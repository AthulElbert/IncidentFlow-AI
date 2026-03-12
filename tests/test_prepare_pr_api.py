import importlib

from fastapi.testclient import TestClient


def test_prepare_pr_endpoint_flow(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("AUTH_MODE", "api_key")
    monkeypatch.setenv("VIEWER_API_KEY", "viewer-key")
    monkeypatch.setenv("APPROVER_API_KEY", "approver-key")
    monkeypatch.setenv("RELEASE_OPERATOR_API_KEY", "release-key")
    monkeypatch.setenv("PR_MODE", "mock")
    monkeypatch.setenv("TEST_EVIDENCE_MODE", "mock")

    import app.main as main_module

    main_module = importlib.reload(main_module)
    client = TestClient(main_module.app)

    incident = client.post("/v1/incidents/mock", headers={"X-API-Key": "viewer-key"})
    assert incident.status_code == 200
    change_id = incident.json()["metadata"]["change_id"]

    prepared = client.post(
        f"/v1/changes/{change_id}/prepare-pr",
        headers={"X-API-Key": "approver-key"},
        json={"comment": "auto draft for review"},
    )
    assert prepared.status_code == 200
    body = prepared.json()
    assert body["change_id"] == change_id
    assert body["pr_status"] == "draft_created"
    assert body["pr_branch"].startswith("agent/")
    assert body["patch_artifact_path"].endswith(".patch")
    assert body["local_branch_created"] is False
    assert body["code_change_status"] == "not_started"
    assert body["test_evidence_status"] == "passed"
