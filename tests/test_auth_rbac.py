import importlib

from fastapi.testclient import TestClient



def test_auth_missing_and_invalid_api_key(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("VIEWER_API_KEY", "viewer-key")
    monkeypatch.setenv("APPROVER_API_KEY", "approver-key")
    monkeypatch.setenv("RELEASE_OPERATOR_API_KEY", "release-key")

    import app.main as main_module

    main_module = importlib.reload(main_module)
    client = TestClient(main_module.app)

    r1 = client.get("/health")
    assert r1.status_code == 401

    r2 = client.get("/health", headers={"X-API-Key": "wrong"})
    assert r2.status_code == 401


def test_auth_forbidden_by_role(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("VIEWER_API_KEY", "viewer-key")
    monkeypatch.setenv("APPROVER_API_KEY", "approver-key")
    monkeypatch.setenv("RELEASE_OPERATOR_API_KEY", "release-key")

    import app.main as main_module

    main_module = importlib.reload(main_module)
    client = TestClient(main_module.app)

    # Viewer can access read endpoint.
    r_read = client.get("/v1/changes", headers={"X-API-Key": "viewer-key"})
    assert r_read.status_code == 200

    # Viewer cannot approve.
    r_decide = client.post(
        "/v1/changes/CHG-DOES-NOT-EXIST/decision",
        headers={"X-API-Key": "viewer-key"},
        json={"decision": "approve", "comment": "x"},
    )
    assert r_decide.status_code == 403

    # Viewer cannot execute dev fix.
    r_execute = client.post(
        "/v1/changes/CHG-DOES-NOT-EXIST/execute-dev",
        headers={"X-API-Key": "viewer-key"},
        json={"comment": "x"},
    )
    assert r_execute.status_code == 403

    # Approver cannot promote.
    r_promote = client.post(
        "/v1/changes/CHG-DOES-NOT-EXIST/promote",
        headers={"X-API-Key": "approver-key"},
        json={"comment": "x"},
    )
    assert r_promote.status_code == 403

    # Approver can execute dev fix endpoint (will fail only because change not found).
    r_execute_approver = client.post(
        "/v1/changes/CHG-DOES-NOT-EXIST/execute-dev",
        headers={"X-API-Key": "approver-key"},
        json={"comment": "x"},
    )
    assert r_execute_approver.status_code == 404
