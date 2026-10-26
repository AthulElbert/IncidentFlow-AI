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


def test_every_element_the_dashboard_wires_up_at_load_exists_in_the_page():
    """A missing id stops the whole script at load, which leaves a blank dashboard (nothing else would notice)."""
    import re
    from pathlib import Path

    html = (Path(__file__).resolve().parents[1] / "frontend" / "dashboard.html").read_text(encoding="utf-8")
    page, script = html.split("<script>", 1)
    wired = set(re.findall(r'^\s*el\("([A-Za-z0-9_]+)"\)\.addEventListener', script, re.M))
    present = set(re.findall(r'\bid="([A-Za-z0-9_]+)"', page))

    assert wired, "expected the dashboard to wire up some elements"
    assert wired - present == set()
