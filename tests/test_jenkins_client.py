import io
import json
from urllib.error import HTTPError, URLError

import pytest

from app.adapters import jenkins_client
from app.adapters.jenkins_client import MockJenkinsClient, RealJenkinsClient, wait_for_build


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


class _JsonResponse:
    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _client() -> RealJenkinsClient:
    return RealJenkinsClient(base_url="https://jenkins.example.com", user="user", api_token="token")


def _serve(monkeypatch, routes: dict[str, dict]) -> list[str]:
    requested: list[str] = []

    def fake_urlopen(req, timeout=0, context=None):
        requested.append(req.full_url)
        return _JsonResponse(routes[req.full_url])

    monkeypatch.setattr(jenkins_client.request, "urlopen", fake_urlopen)
    return requested


def test_real_jenkins_status_stays_queued_until_the_queue_item_has_a_build(monkeypatch):
    _serve(monkeypatch, {"https://jenkins.example.com/queue/item/7/api/json": {"why": "waiting"}})

    result = _client().get_build_status("https://jenkins.example.com/queue/item/7/")

    assert result.status == "QUEUED"
    assert result.url == "https://jenkins.example.com/queue/item/7/"


def test_real_jenkins_status_resolves_queue_item_to_build_and_reports_running(monkeypatch):
    requested = _serve(
        monkeypatch,
        {
            "https://jenkins.example.com/queue/item/7/api/json": {
                "executable": {"number": 42, "url": "https://jenkins.example.com/job/pay-prod-deploy/42/"}
            },
            "https://jenkins.example.com/job/pay-prod-deploy/42/api/json": {
                "building": True,
                "result": None,
                "number": 42,
                "url": "https://jenkins.example.com/job/pay-prod-deploy/42/",
                "fullDisplayName": "pay-prod-deploy #42",
            },
        },
    )

    result = _client().get_build_status("https://jenkins.example.com/queue/item/7/")

    assert result.status == "RUNNING"
    assert result.build_number == 42
    # Callers persist this url: queue items expire, the build url does not.
    assert result.url == "https://jenkins.example.com/job/pay-prod-deploy/42/"
    assert len(requested) == 2


@pytest.mark.parametrize(
    "jenkins_result,expected",
    [("SUCCESS", "SUCCESS"), ("FAILURE", "FAILURE"), ("ABORTED", "ABORTED"), ("UNSTABLE", "UNSTABLE"), ("WEIRD", "UNKNOWN")],
)
def test_real_jenkins_status_maps_final_build_results(monkeypatch, jenkins_result, expected):
    build_url = "https://jenkins.example.com/job/pay-prod-deploy/42/"
    _serve(
        monkeypatch,
        {build_url + "api/json": {"building": False, "result": jenkins_result, "number": 42, "url": build_url}},
    )

    assert _client().get_build_status(build_url).status == expected


def test_real_jenkins_status_reports_cancelled_queue_item_as_aborted(monkeypatch):
    _serve(monkeypatch, {"https://jenkins.example.com/queue/item/7/api/json": {"cancelled": True}})

    assert _client().get_build_status("https://jenkins.example.com/queue/item/7/").status == "ABORTED"


def test_real_jenkins_status_http_error(monkeypatch):
    def fake_urlopen(req, timeout=0, context=None):
        raise HTTPError(url=req.full_url, code=404, msg="Not Found", hdrs=None, fp=io.BytesIO(b"gone"))

    monkeypatch.setattr(jenkins_client.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="Jenkins API error 404"):
        _client().get_build_status("https://jenkins.example.com/queue/item/7/")


def test_mock_jenkins_runs_then_reports_the_configured_outcome():
    mock = MockJenkinsClient(outcome="success", polls_until_done=2)
    build = mock.trigger_prod_deploy("payments-service", "CHG-1")
    assert build.status == "QUEUED"

    assert mock.get_build_status(build.url).status == "RUNNING"
    assert mock.get_build_status(build.url).status == "RUNNING"
    assert mock.get_build_status(build.url).status == "SUCCESS"

    failing = MockJenkinsClient(outcome="failure", polls_until_done=0)
    assert failing.get_build_status(failing.trigger_dev_validation("s", "t").url).status == "FAILURE"


def test_wait_for_build_stops_at_terminal_state_and_respects_the_wait_budget():
    mock = MockJenkinsClient(outcome="success", polls_until_done=2)
    build = mock.trigger_dev_validation("payments-service", "performance_degradation")
    assert wait_for_build(mock, build, wait_seconds=30, poll_interval_seconds=0).status == "SUCCESS"

    slow = MockJenkinsClient(outcome="success", polls_until_done=10_000)
    slow_build = slow.trigger_dev_validation("payments-service", "performance_degradation")
    # A zero budget still polls once, then gives up while the build is running.
    assert wait_for_build(slow, slow_build, wait_seconds=0, poll_interval_seconds=0).status == "RUNNING"


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
