import io
from urllib.error import HTTPError, URLError

import pytest

from app.adapters import jenkins_client
from app.adapters.jenkins_client import RealJenkinsClient


class _Response:
    def __init__(self, status=201, location="https://jenkins/queue/item/1"):
        self.status = status
        self.headers = {"Location": location}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_real_jenkins_trigger_dev_success(monkeypatch):
    def fake_urlopen(req, timeout=0, context=None):
        return _Response(location="https://jenkins/queue/item/11")

    monkeypatch.setattr(jenkins_client.request, "urlopen", fake_urlopen)

    client = RealJenkinsClient(
        base_url="https://jenkins.example.com",
        user="user",
        api_token="token",
    )
    result = client.trigger_dev_validation("payments-service", "performance_degradation")

    assert result.status == "QUEUED"
    assert result.url == "https://jenkins/queue/item/11"


def test_real_jenkins_trigger_prod_success(monkeypatch):
    def fake_urlopen(req, timeout=0, context=None):
        return _Response(location="https://jenkins/queue/item/99")

    monkeypatch.setattr(jenkins_client.request, "urlopen", fake_urlopen)

    client = RealJenkinsClient(
        base_url="https://jenkins.example.com",
        user="user",
        api_token="token",
    )
    result = client.trigger_prod_deploy("payments-service", "CHG-123")

    assert result.status == "QUEUED"
    assert result.url == "https://jenkins/queue/item/99"


def test_real_jenkins_trigger_http_error(monkeypatch):
    def fake_urlopen(req, timeout=0, context=None):
        raise HTTPError(
            url=req.full_url,
            code=403,
            msg="Forbidden",
            hdrs=None,
            fp=io.BytesIO(b"no permission"),
        )

    monkeypatch.setattr(jenkins_client.request, "urlopen", fake_urlopen)

    client = RealJenkinsClient(
        base_url="https://jenkins.example.com",
        user="user",
        api_token="token",
    )

    with pytest.raises(RuntimeError, match="Jenkins API error 403"):
        client.trigger_dev_validation("payments-service", "performance_degradation")


def test_real_jenkins_trigger_connection_error(monkeypatch):
    def fake_urlopen(req, timeout=0, context=None):
        raise URLError("unreachable")

    monkeypatch.setattr(jenkins_client.request, "urlopen", fake_urlopen)

    client = RealJenkinsClient(
        base_url="https://jenkins.example.com",
        user="user",
        api_token="token",
    )

    with pytest.raises(RuntimeError, match="Jenkins connection error"):
        client.trigger_dev_validation("payments-service", "performance_degradation")
