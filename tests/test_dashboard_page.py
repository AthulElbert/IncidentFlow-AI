import importlib

from fastapi.testclient import TestClient


def test_dashboard_page_served(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")

    import app.main as main_module

    main_module = importlib.reload(main_module)
    client = TestClient(main_module.app)
    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "Agentic Production Support Dashboard" in response.text
