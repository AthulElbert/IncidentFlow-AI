import json
import random
import ssl
from dataclasses import dataclass
from typing import Protocol
from urllib import parse, request
from urllib.error import HTTPError, URLError


@dataclass(frozen=True)
class APMEvidence:
    apm_improvement_pct: float
    smoke_tests_passed: bool
    notes: str


class APMClient(Protocol):
    def collect_dev_evidence(self, service: str, change_id: str, issue_type: str) -> APMEvidence:
        ...


class MockAPMClient:
    def collect_dev_evidence(self, service: str, change_id: str, issue_type: str) -> APMEvidence:
        base = 12.0 if issue_type != "unknown" else 2.0
        jitter = random.uniform(-2.0, 2.0)
        improvement = round(max(0.0, base + jitter), 2)
        smoke_ok = issue_type != "unknown"
        notes = f"mock-apm evidence for {service}/{change_id}"
        return APMEvidence(
            apm_improvement_pct=improvement,
            smoke_tests_passed=smoke_ok,
            notes=notes,
        )


class HttpAPMClient:
    def __init__(self, base_url: str, timeout_seconds: int = 10, verify_ssl: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.verify_ssl = verify_ssl

        if not self.base_url:
            raise ValueError("APM base URL is required for http mode")

    def collect_dev_evidence(self, service: str, change_id: str, issue_type: str) -> APMEvidence:
        query = parse.urlencode({"service": service, "change_id": change_id, "issue_type": issue_type})
        url = f"{self.base_url}/v1/evidence?{query}"
        req = request.Request(url=url, headers={"Accept": "application/json"}, method="GET")

        context = None
        if not self.verify_ssl:
            context = ssl._create_unverified_context()

        try:
            with request.urlopen(req, timeout=self.timeout_seconds, context=context) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            raise RuntimeError(f"APM API error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"APM connection error: {exc.reason}") from exc

        try:
            improvement = float(body.get("apm_improvement_pct", 0.0))
            smoke = bool(body.get("smoke_tests_passed", False))
            notes = str(body.get("notes", "apm evidence collected"))
        except Exception as exc:
            raise RuntimeError(f"Invalid APM evidence payload: {body}") from exc

        return APMEvidence(
            apm_improvement_pct=improvement,
            smoke_tests_passed=smoke,
            notes=notes,
        )
