import json
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from urllib import parse, request
from urllib.error import HTTPError, URLError

from app.models.schemas import APMEvent


@dataclass(frozen=True)
class APMAlert:
    source_alert_id: str
    event: APMEvent


class APMAlertSource(Protocol):
    def fetch_open_alerts(self, limit: int = 20) -> list[APMAlert]:
        ...


class MockAPMAlertSource:
    def __init__(self, queue_file: str) -> None:
        self.queue_path = Path(queue_file)

    def fetch_open_alerts(self, limit: int = 20) -> list[APMAlert]:
        if not self.queue_path.exists():
            return []

        raw = json.loads(self.queue_path.read_text(encoding="utf-8-sig"))
        items = raw[: max(0, limit)]
        alerts: list[APMAlert] = []
        for idx, item in enumerate(items):
            source_id = str(item.get("source_alert_id", f"mock-{idx+1}")).strip()
            event = APMEvent(
                service=item["service"],
                metric=item["metric"],
                value=float(item["value"]),
                threshold=float(item["threshold"]),
                environment=item.get("environment", "prod"),
                timestamp=item["timestamp"],
                message=item["message"],
            )
            alerts.append(APMAlert(source_alert_id=source_id, event=event))
        return alerts


class HttpAPMAlertSource:
    def __init__(self, base_url: str, timeout_seconds: int = 10, verify_ssl: bool = True) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.verify_ssl = verify_ssl
        if not self.base_url:
            raise ValueError("APM alerts base URL is required for http mode")

    def fetch_open_alerts(self, limit: int = 20) -> list[APMAlert]:
        query = parse.urlencode({"limit": limit})
        url = f"{self.base_url}/v1/alerts?{query}"
        req = request.Request(url=url, headers={"Accept": "application/json"}, method="GET")

        context = None
        if not self.verify_ssl:
            context = ssl._create_unverified_context()

        try:
            with request.urlopen(req, timeout=self.timeout_seconds, context=context) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            raise RuntimeError(f"APM alerts API error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"APM alerts connection error: {exc.reason}") from exc

        if not isinstance(body, list):
            raise RuntimeError("APM alerts API payload must be a list")

        alerts: list[APMAlert] = []
        for idx, item in enumerate(body[: max(0, limit)]):
            source_id = str(item.get("source_alert_id", f"http-{idx+1}")).strip()
            event = APMEvent(
                service=item["service"],
                metric=item["metric"],
                value=float(item["value"]),
                threshold=float(item["threshold"]),
                environment=item.get("environment", "prod"),
                timestamp=item["timestamp"],
                message=item["message"],
            )
            alerts.append(APMAlert(source_alert_id=source_id, event=event))
        return alerts


class DynatraceAPMAlertSource:
    def __init__(
        self,
        base_url: str,
        api_token: str,
        timeout_seconds: int = 10,
        verify_ssl: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token.strip()
        self.timeout_seconds = timeout_seconds
        self.verify_ssl = verify_ssl
        if not self.base_url or not self.api_token:
            raise ValueError("Dynatrace alerts config requires base_url and api_token")

    def fetch_open_alerts(self, limit: int = 20) -> list[APMAlert]:
        problems = self._fetch_problems(limit=limit)
        alerts: list[APMAlert] = []
        for idx, item in enumerate(problems):
            source_id = str(item.get("problemId") or item.get("id") or f"dynatrace-{idx+1}").strip()
            title = str(item.get("title", "Dynatrace problem")).strip()
            severity = str(item.get("severityLevel", "UNKNOWN")).upper()
            status = str(item.get("status", "OPEN")).upper()
            if status not in {"OPEN", "ACTIVE"}:
                continue

            impacted = item.get("impactedEntities") or []
            service = "unknown-service"
            if isinstance(impacted, list) and impacted:
                first = impacted[0]
                if isinstance(first, dict):
                    service = str(first.get("name") or first.get("entityId") or service)

            metric, value, threshold = self._infer_metric_and_values(title=title, severity=severity)
            event = APMEvent(
                service=service,
                metric=metric,
                value=value,
                threshold=threshold,
                environment="prod",
                timestamp=self._extract_timestamp(item),
                message=title,
            )
            alerts.append(APMAlert(source_alert_id=source_id, event=event))
        return alerts[: max(0, limit)]

    def _fetch_problems(self, limit: int) -> list[dict]:
        selector = 'status("OPEN")'
        query = parse.urlencode({"problemSelector": selector, "pageSize": max(1, limit)})
        url = f"{self.base_url}/api/v2/problems?{query}"
        req = request.Request(
            url=url,
            headers={
                "Authorization": f"Api-Token {self.api_token}",
                "Accept": "application/json",
            },
            method="GET",
        )

        context = None
        if not self.verify_ssl:
            context = ssl._create_unverified_context()

        try:
            with request.urlopen(req, timeout=self.timeout_seconds, context=context) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            raise RuntimeError(f"Dynatrace problems API error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"Dynatrace problems connection error: {exc.reason}") from exc

        if not isinstance(body, dict):
            raise RuntimeError("Dynatrace problems payload must be an object")
        problems = body.get("problems", [])
        if not isinstance(problems, list):
            raise RuntimeError("Dynatrace problems payload missing list 'problems'")
        return [item for item in problems if isinstance(item, dict)]

    def _extract_timestamp(self, item: dict) -> datetime:
        value = item.get("startTime") or item.get("startTimeUTC")
        if isinstance(value, int):
            return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(text)
            except ValueError:
                pass
        return datetime.now(timezone.utc)

    def _infer_metric_and_values(self, title: str, severity: str) -> tuple[str, float, float]:
        text = title.lower()
        if "latency" in text or "response time" in text:
            return "latency_p99_ms", 1200.0, 800.0
        if "error" in text or "exception" in text or "failure" in text:
            return "error_rate_pct", 7.0, 2.0
        if "cpu" in text:
            return "cpu_usage_pct", 95.0, 80.0
        if "memory" in text:
            return "memory_usage_pct", 92.0, 85.0
        if severity in {"AVAILABILITY", "ERROR"}:
            return "error_rate_pct", 6.0, 2.0
        if severity in {"PERFORMANCE", "RESOURCE"}:
            return "latency_p99_ms", 1100.0, 800.0
        return "generic_alert_score", 1.0, 0.5
