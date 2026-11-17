"""Service-scoped authorisation (AUTHZ_MODE=scoped): a role is not enough, the person's team must own the service."""
import json
import os

import pytest
from agent_harness import APPROVER, RELEASE, VIEWER, scenario_incident

from app.config import ConfigError, load_settings, validate_settings

SERVICE = "faulty-project"  # the service of the bad_config_latency scenario


def _owners(tmp_path, services=None):
    path = tmp_path / "owners.json"
    path.write_text(json.dumps({"admin_team": "platform", "services": services if services is not None else {SERVICE: "team-checkout"}}), encoding="utf-8")
    return str(path)


def _scoped(make_app, tmp_path, approver_teams="team-checkout", release_teams="team-checkout", services=None, **env):
    return make_app(
        AUTHZ_MODE="scoped", SERVICE_OWNERSHIP_FILE=_owners(tmp_path, services),
        APPROVER_TEAMS=approver_teams, RELEASE_OPERATOR_TEAMS=release_teams, **env,
    )


def _audit(client, change_id):
    return client.get(f"/v1/audit?entity_id={change_id}&limit=200", headers=APPROVER).json()


def test_a_person_outside_the_owning_team_is_refused_and_the_refusal_is_audited(make_app, tmp_path):
    _, client = _scoped(make_app, tmp_path, approver_teams="team-orders")
    change_id = scenario_incident(client)

    refused = client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "reject"})
    assert refused.status_code == 403
    assert refused.json()["detail"] == f"approver-user is not in team-checkout, the team that owns {SERVICE}"
    assert client.get(f"/v1/changes/{change_id}", headers=VIEWER).json()["status"] == "pending_approval"

    denied = [e for e in _audit(client, change_id) if e["action"] == "authz.denied"]
    assert len(denied) == 1 and denied[0]["actor"] == "approver-user"
    assert denied[0]["details"]["action"] == "decide" and denied[0]["details"]["policy"]["engine"] == "python"
    labels = [item["label"] for item in client.get(f"/v1/changes/{change_id}/review", headers=VIEWER).json()["timeline"]]
    assert "Refused: not permitted on this service" in labels


def test_the_owning_team_may_act_and_the_allowing_decision_is_kept(make_app, tmp_path):
    _, client = _scoped(make_app, tmp_path)
    change_id = scenario_incident(client)
    assert client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "reject"}).status_code == 200

    actions = [e["action"] for e in _audit(client, change_id)]
    assert actions.index("authz.allowed") < actions.index("decision.rejected")
    labels = [item["label"] for item in client.get(f"/v1/changes/{change_id}/review", headers=VIEWER).json()["timeline"]]
    assert not any("authz" in label for label in labels)  # an allowed check is not a timeline event of its own


def test_the_admin_team_may_act_on_any_service_and_nobody_else_on_an_unowned_one(make_app, tmp_path):
    _, client = _scoped(make_app, tmp_path, approver_teams="team-checkout", release_teams="platform", services={})
    change_id = scenario_incident(client)

    refused = client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "reject"})
    assert refused.status_code == 403 and "has no owning team" in refused.json()["detail"]
    # The release operator is in the admin team; its role also covers what an approver may do.
    assert client.post(f"/v1/changes/{change_id}/decision", headers=RELEASE, json={"decision": "reject"}).status_code == 200


def test_every_action_on_a_change_is_scoped(make_app, tmp_path):
    _, client = _scoped(make_app, tmp_path, approver_teams="team-orders", release_teams="team-orders", AGENT_RUN_MODE="manual")
    change_id = scenario_incident(client)
    for method, path, headers, body in [
        ("post", "investigate", APPROVER, None),
        ("post", "execute-dev", APPROVER, {}),
        ("post", "refresh-status", APPROVER, None),
        ("post", "prepare-pr", APPROVER, {}),
        ("post", "decision", APPROVER, {"decision": "approve"}),
        ("post", "promote", RELEASE, {}),
        ("put", "postmortem", APPROVER, {"markdown": "## Summary\nsomething happened here"}),
    ]:
        response = getattr(client, method)(f"/v1/changes/{change_id}/{path}", headers=headers, json=body)
        assert response.status_code == 403, (path, response.status_code, response.text)
    # Reading is not scoped, and roles still come first
    assert client.get(f"/v1/changes/{change_id}/review", headers=VIEWER).status_code == 200
    assert client.post(f"/v1/changes/{change_id}/decision", headers=VIEWER, json={"decision": "reject"}).status_code == 403
    assert client.post("/v1/changes/CHG-NOPE/decision", headers=APPROVER, json={"decision": "reject"}).status_code == 404


def test_the_screen_can_ask_what_is_open_to_each_key(make_app, tmp_path):
    _, client = _scoped(make_app, tmp_path, approver_teams="team-orders")
    change_id = scenario_incident(client)

    mine = client.get(f"/v1/changes/{change_id}/access", headers=APPROVER).json()
    assert mine["scoped"] is True and mine["teams"] == ["team-orders"]
    assert mine["actions"]["approve"] == f"approver-user is not in team-checkout, the team that owns {SERVICE}"
    assert mine["actions"]["promote"] == "needs the release_operator role"

    release = client.get(f"/v1/changes/{change_id}/access", headers=RELEASE).json()
    assert release["actions"]["promote"] == "" and release["actions"]["approve"] == ""
    assert client.get(f"/v1/whoami", headers=RELEASE).json()["teams"] == ["team-checkout"]
    assert not any(e["action"].startswith("authz.") for e in _audit(client, change_id))  # asking decides nothing


def test_without_scoping_roles_alone_decide_as_before(make_app):
    _, client = make_app()
    change_id = scenario_incident(client)
    assert client.get(f"/v1/changes/{change_id}/access", headers=APPROVER).json() == {
        "actor": "approver-user", "teams": [], "scoped": False,
        "actions": {"run_agents": "", "approve": "", "reject": "", "promote": "needs the release_operator role", "refresh_status": ""},
    }
    assert client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "reject"}).status_code == 200
    assert not any(e["action"].startswith("authz.") for e in _audit(client, change_id))


def test_scoping_needs_identities_and_a_valid_ownership_file_out_of_the_agents_reach(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTHZ_MODE", "scoped")
    monkeypatch.delenv("SERVICE_OWNERSHIP_FILE", raising=False)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    with pytest.raises(ConfigError) as exc:
        validate_settings(load_settings())
    assert "needs AUTH_ENABLED=true" in str(exc.value) and "requires SERVICE_OWNERSHIP_FILE" in str(exc.value)

    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("SERVICE_OWNERSHIP_FILE", "config/owners.json")
    monkeypatch.setenv("CODE_CHANGE_ALLOWED_PATHS", "config/")
    with pytest.raises(ConfigError, match="which agents may edit"):
        validate_settings(load_settings())

    bad = tmp_path / "owners.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("SERVICE_OWNERSHIP_FILE", str(bad))
    with pytest.raises(ConfigError, match="not valid JSON"):
        validate_settings(load_settings())


# --- with the Rego rules on a real OPA server (runs when TEST_OPA_URL is set) --------------------------------------


@pytest.mark.skipif(not os.getenv("TEST_OPA_URL"), reason="TEST_OPA_URL not set")
def test_the_app_decides_through_opa_and_records_which_rules_it_used(make_app, tmp_path):
    main_module, client = _scoped(make_app, tmp_path, approver_teams="team-orders", POLICY_ENGINE="opa", OPA_URL=os.environ["TEST_OPA_URL"])
    assert main_module.policy_engine.name == "opa"
    change_id = scenario_incident(client)

    refused = client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve"})
    assert refused.status_code == 403 and "the team that owns" in refused.json()["detail"]
    denied = next(e for e in _audit(client, change_id) if e["action"] == "authz.denied")
    assert denied["details"]["policy"]["engine"] == "opa" and denied["details"]["policy"]["bundle"] == main_module.policy_engine.bundle

    # The promotion gate also comes from OPA: a fresh scenario change has no dev evidence yet
    review = client.get(f"/v1/changes/{change_id}/review", headers=VIEWER).json()
    assert "dev execution evidence is not in passed state" in json.dumps(review)
