import io
import json
from urllib.error import HTTPError, URLError

import pytest

from app.adapters import jira_client
from app.adapters.jira_client import RealJiraClient


class _Response:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_real_jira_create_ticket_success(monkeypatch):
    def fake_urlopen(req, timeout=0):
        return _Response({"key": "SUP-123"})

    monkeypatch.setattr(jira_client.request, "urlopen", fake_urlopen)

    client = RealJiraClient(
        base_url="https://example.atlassian.net",
        project_key="SUP",
        email="user@example.com",
        api_token="token",
    )
    result = client.create_ticket("summary", "description", ["ops"])

    assert result.key == "SUP-123"
    assert result.summary == "summary"


def test_real_jira_create_ticket_http_error(monkeypatch):
    def fake_urlopen(req, timeout=0):
        raise HTTPError(
            url=req.full_url,
            code=401,
            msg="Unauthorized",
            hdrs=None,
            fp=io.BytesIO(b"invalid creds"),
        )

    monkeypatch.setattr(jira_client.request, "urlopen", fake_urlopen)

    client = RealJiraClient(
        base_url="https://example.atlassian.net",
        project_key="SUP",
        email="user@example.com",
        api_token="token",
    )

    with pytest.raises(RuntimeError, match="Jira API error 401"):
        client.create_ticket("summary", "description", ["ops"])


def test_real_jira_create_ticket_connection_error(monkeypatch):
    def fake_urlopen(req, timeout=0):
        raise URLError("network down")

    monkeypatch.setattr(jira_client.request, "urlopen", fake_urlopen)

    client = RealJiraClient(
        base_url="https://example.atlassian.net",
        project_key="SUP",
        email="user@example.com",
        api_token="token",
    )

    with pytest.raises(RuntimeError, match="Jira connection error"):
        client.create_ticket("summary", "description", ["ops"])
