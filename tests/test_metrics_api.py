import importlib

from fastapi.testclient import TestClient


def test_metrics_summary_endpoint_available(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("STORAGE_BACKEND", "json")

    import app.main as main_module

    main_module = importlib.reload(main_module)
    client = TestClient(main_module.app)
    response = client.get("/v1/metrics/summary")

    assert response.status_code == 200
    body = response.json()
    assert "total_changes" in body
    assert "policy_block_rate" in body
    assert "dev_success_rate" in body
