import json
from urllib.error import HTTPError, URLError

import pytest

from app.adapters import apm_client
from app.adapters.apm_client import HttpAPMClient


class _Resp:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_http_apm_collect_success(monkeypatch):
    def fake_urlopen(req, timeout=0, context=None):
        return _Resp(
            {
                "apm_improvement_pct": 9.4,
                "smoke_tests_passed": True,
                "notes": "evidence from apm",
            }
        )

    monkeypatch.setattr(apm_client.request, "urlopen", fake_urlopen)

    client = HttpAPMClient(base_url="http://apm.local")
    ev = client.collect_dev_evidence("payments-service", "CHG-1", "performance_degradation")

    assert ev.apm_improvement_pct == 9.4
    assert ev.smoke_tests_passed is True


def test_http_apm_collect_error(monkeypatch):
    def fake_urlopen(req, timeout=0, context=None):
        raise URLError("down")

    monkeypatch.setattr(apm_client.request, "urlopen", fake_urlopen)

    client = HttpAPMClient(base_url="http://apm.local")
    with pytest.raises(RuntimeError, match="APM connection error"):
        client.collect_dev_evidence("payments-service", "CHG-1", "performance_degradation")


def test_http_apm_collect_bad_payload(monkeypatch):
    def fake_urlopen(req, timeout=0, context=None):
        return _Resp({"smoke_tests_passed": True, "notes": "missing improvement"})

    monkeypatch.setattr(apm_client.request, "urlopen", fake_urlopen)

    client = HttpAPMClient(base_url="http://apm.local")
    ev = client.collect_dev_evidence("payments-service", "CHG-1", "performance_degradation")
    assert ev.apm_improvement_pct == 0.0
