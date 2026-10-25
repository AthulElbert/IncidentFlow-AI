import importlib

import pytest
from fastapi.testclient import TestClient

VIEWER = {"X-API-Key": "viewer-key"}
APPROVER = {"X-API-Key": "approver-key"}
RELEASE = {"X-API-Key": "release-key"}


@pytest.fixture
def make_app(monkeypatch):
    def _make(**env):
        monkeypatch.setenv("VIEWER_API_KEY", "viewer-key")
        monkeypatch.setenv("APPROVER_API_KEY", "approver-key")
        monkeypatch.setenv("RELEASE_OPERATOR_API_KEY", "release-key")
        for key, value in env.items():
            monkeypatch.setenv(key, str(value))

        import app.main as main_module

        main_module = importlib.reload(main_module)
        return main_module, TestClient(main_module.app)

    return _make


def _count_prod_deploys(monkeypatch, main_module):
    calls: list[dict] = []
    original = main_module.jenkins_client.trigger_prod_deploy

    def spy(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(main_module.jenkins_client, "trigger_prod_deploy", spy)
    return calls


def _new_change(client) -> str:
    response = client.post("/v1/incidents/mock", headers=VIEWER)
    assert response.status_code == 200
    return response.json()["metadata"]["change_id"]


def _change(client, change_id) -> dict:
    return client.get(f"/v1/changes/{change_id}", headers=VIEWER).json()


def _ready_and_approved(client) -> str:
    change_id = _new_change(client)
    executed = client.post(f"/v1/changes/{change_id}/execute-dev", headers=APPROVER, json={})
    assert executed.status_code == 200
    assert executed.json()["deployment_state"] == "ready_for_prod"
    decided = client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve"})
    assert decided.status_code == 200
    assert decided.json()["deployment_state"] == "ready_for_prod"
    return change_id


def test_happy_path_promotes_only_after_jenkins_confirms_success(make_app, monkeypatch):
    main_module, client = make_app()
    deploys = _count_prod_deploys(monkeypatch, main_module)
    change_id = _ready_and_approved(client)

    record = _change(client, change_id)
    assert record["jenkins_status"] == "SUCCESS"
    assert record["dev_execution_status"] == "passed"

    promoted = client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={"comment": "ship"})

    assert promoted.status_code == 200
    body = promoted.json()
    assert body["deployment_state"] == "promoted_to_prod"
    assert body["promotion_result"] == "success"
    assert body["promoted_at"] is not None
    assert len(deploys) == 1


def test_unapproved_change_never_triggers_a_prod_deploy(make_app, monkeypatch):
    main_module, client = make_app()
    deploys = _count_prod_deploys(monkeypatch, main_module)
    change_id = _new_change(client)
    client.post(f"/v1/changes/{change_id}/execute-dev", headers=APPROVER, json={})

    response = client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={})

    assert response.status_code == 400
    assert "must be approved" in response.json()["detail"]
    assert deploys == []
    assert _change(client, change_id)["deployment_state"] == "ready_for_prod"


def test_rejected_change_never_triggers_a_prod_deploy(make_app, monkeypatch):
    main_module, client = make_app()
    deploys = _count_prod_deploys(monkeypatch, main_module)
    change_id = _new_change(client)
    client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "reject", "comment": "no"})

    response = client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={})

    assert response.status_code == 400
    assert deploys == []


def test_second_promote_does_not_deploy_twice(make_app, monkeypatch):
    main_module, client = make_app()
    deploys = _count_prod_deploys(monkeypatch, main_module)
    change_id = _ready_and_approved(client)

    assert client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={}).status_code == 200
    again = client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={})

    assert again.status_code == 400
    assert "already promoted" in again.json()["detail"]
    assert len(deploys) == 1


def test_running_prod_deploy_stays_promoting_until_refresh_confirms_success(make_app, monkeypatch):
    main_module, client = make_app(JENKINS_BUILD_WAIT_SECONDS=0, MOCK_JENKINS_POLLS=0)
    change_id = _ready_and_approved(client)
    # From here on every build reports RUNNING for two polls before it succeeds.
    main_module.jenkins_client.polls_until_done = 2

    promoted = client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={})

    assert promoted.status_code == 200
    assert promoted.json()["deployment_state"] == "promoting"
    assert promoted.json()["promotion_result"] == "queued"
    assert promoted.json()["promoted_at"] is None

    first = client.post(f"/v1/changes/{change_id}/refresh-status", headers=APPROVER)
    assert first.status_code == 200
    assert first.json()["deployment_state"] == "promoting"

    second = client.post(f"/v1/changes/{change_id}/refresh-status", headers=APPROVER)
    assert second.json()["deployment_state"] == "promoted_to_prod"
    assert second.json()["promotion_result"] == "success"
    assert second.json()["promoted_at"] is not None


def test_failed_prod_build_is_recorded_as_failed(make_app):
    main_module, client = make_app(MOCK_JENKINS_POLLS=0)
    change_id = _ready_and_approved(client)
    main_module.jenkins_client.outcome = "FAILURE"

    response = client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={})

    assert response.status_code == 200
    assert response.json()["deployment_state"] == "prod_promotion_failed"
    assert response.json()["promotion_result"] == "failed"


def test_deploy_trigger_error_is_recorded_and_not_left_promoting(make_app, monkeypatch):
    main_module, client = make_app()
    change_id = _ready_and_approved(client)

    def boom(**kwargs):
        raise RuntimeError("jenkins unreachable")

    monkeypatch.setattr(main_module.jenkins_client, "trigger_prod_deploy", boom)

    response = client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={})

    assert response.status_code == 502
    record = _change(client, change_id)
    assert record["deployment_state"] == "prod_promotion_failed"
    assert "jenkins unreachable" in record["promotion_comment"]


def test_dev_build_still_running_blocks_approval_until_refreshed(make_app, monkeypatch):
    main_module, client = make_app(JENKINS_BUILD_WAIT_SECONDS=0, MOCK_JENKINS_POLLS=2)
    change_id = _new_change(client)

    started = client.post(f"/v1/changes/{change_id}/execute-dev", headers=APPROVER, json={})
    assert started.status_code == 200
    assert started.json()["deployment_state"] == "dev_fix_in_progress"
    assert started.json()["validation_passed"] is False

    early = client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve"})
    assert early.status_code == 400

    duplicate = client.post(f"/v1/changes/{change_id}/execute-dev", headers=APPROVER, json={})
    assert duplicate.status_code == 400
    assert "already in progress" in duplicate.json()["detail"]

    assert client.post(f"/v1/changes/{change_id}/refresh-status", headers=APPROVER).json()["deployment_state"] == (
        "dev_fix_in_progress"
    )
    finished = client.post(f"/v1/changes/{change_id}/refresh-status", headers=APPROVER)
    assert finished.json()["deployment_state"] == "ready_for_prod"
    assert finished.json()["jenkins_status"] == "SUCCESS"
    assert finished.json()["dev_execution_status"] == "passed"


def test_failed_dev_build_blocks_the_change(make_app):
    _, client = make_app(MOCK_JENKINS_OUTCOME="failure", MOCK_JENKINS_POLLS=0)
    change_id = _new_change(client)

    executed = client.post(f"/v1/changes/{change_id}/execute-dev", headers=APPROVER, json={})

    assert executed.json()["deployment_state"] == "dev_fix_failed"
    approve = client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve"})
    assert approve.status_code == 400


def test_refresh_status_rules(make_app):
    _, client = make_app()
    change_id = _new_change(client)

    assert client.post(f"/v1/changes/{change_id}/refresh-status", headers=VIEWER).status_code == 403
    assert client.post("/v1/changes/CHG-NOPE/refresh-status", headers=APPROVER).status_code == 404
    nothing = client.post(f"/v1/changes/{change_id}/refresh-status", headers=APPROVER)
    assert nothing.status_code == 400
    assert "No build in progress" in nothing.json()["detail"]


def test_startup_refuses_non_terminal_jenkins_state_for_prod(make_app):
    with pytest.raises(Exception, match="ALLOWED_JENKINS_STATES_FOR_PROD"):
        make_app(ALLOWED_JENKINS_STATES_FOR_PROD="QUEUED,SUCCESS")
