import json
import ssl
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol
from urllib import parse, request
from urllib.error import HTTPError, URLError

from app.tools.base import TimeWindow, ToolError
from app.tools.models import MetricPoint, MetricSeries
from app.tools.scenarios import DEFAULT_SCENARIOS, Scenario, scenario_for


class MetricsTool(Protocol):
    def available_metrics(self, service: str) -> list[str]:
        """Metric names that can be queried for this service."""
        ...

    def query(self, service: str, metric: str, window: TimeWindow, step_seconds: int = 60) -> MetricSeries:
        ...


class MockMetricsTool:
    """Serves scenario metrics. Services with no scenario get a flat, healthy series."""

    def __init__(self, scenarios: tuple[Scenario, ...] | list[Scenario] = DEFAULT_SCENARIOS) -> None:
        self.scenarios = tuple(scenarios)

    def available_metrics(self, service: str) -> list[str]:
        scenario = scenario_for(service, self.scenarios)
        return sorted(scenario.metrics) if scenario else []

    def query(self, service: str, metric: str, window: TimeWindow, step_seconds: int = 60) -> MetricSeries:
        scenario = scenario_for(service, self.scenarios)
        series = scenario.metric_series(metric, window, step_seconds) if scenario else None
        if series is not None:
            return series
        if scenario is not None:
            raise ToolError(f"unknown metric '{metric}' for {service}; available: {sorted(scenario.metrics)}")
        # No scenario for this service: report it as healthy rather than inventing a problem.
        step = max(1, step_seconds)
        points = []
        moment = window.start
        while moment <= window.end:
            points.append(MetricPoint(timestamp=moment, value=1.0))
            moment += timedelta(seconds=step)
        return MetricSeries(service=service, metric=metric, points=points)


@dataclass(frozen=True)
class PromQuery:
    expr: str
    unit: str = ""


# Queries for the local stack in local_apm_demo/ (faulty_project and sample_app).
DEFAULT_PROMQL_CATALOG: dict[tuple[str, str], PromQuery] = {
    ("faulty-project", "latency_p95_seconds"): PromQuery(
        "histogram_quantile(0.95, sum(rate(faulty_request_duration_seconds_bucket[1m])) by (le))", "seconds"
    ),
    ("faulty-project", "error_rate_per_sec"): PromQuery('sum(rate(faulty_requests_total{status="error"}[1m]))', "req/s"),
    ("sample-app", "latency_p95_seconds"): PromQuery(
        "histogram_quantile(0.95, sum(rate(app_request_duration_seconds_bucket[1m])) by (le))", "seconds"
    ),
    ("sample-app", "error_rate_per_sec"): PromQuery('sum(rate(app_requests_total{status="error"}[1m]))', "req/s"),
}


class PrometheusMetricsTool:
    """Queries a Prometheus server through a fixed catalog of service/metric -> PromQL.

    Agents choose from the catalog; they never supply PromQL, so a prompt-injected
    alert cannot make this tool run an arbitrary query.
    """

    def __init__(
        self,
        base_url: str,
        timeout_seconds: int = 10,
        verify_ssl: bool = True,
        catalog: dict[tuple[str, str], PromQuery] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.verify_ssl = verify_ssl
        self.catalog = dict(DEFAULT_PROMQL_CATALOG if catalog is None else catalog)
        if not self.base_url:
            raise ValueError("Prometheus base URL is required for metrics_mode=prometheus")

    def available_metrics(self, service: str) -> list[str]:
        return sorted(metric for (svc, metric) in self.catalog if svc == service)

    def query(self, service: str, metric: str, window: TimeWindow, step_seconds: int = 60) -> MetricSeries:
        entry = self.catalog.get((service, metric))
        if entry is None:
            raise ToolError(
                f"no query defined for {service}/{metric}; available for {service}: {self.available_metrics(service)}"
            )

        params = parse.urlencode(
            {
                "query": entry.expr,
                "start": f"{window.start.timestamp():.3f}",
                "end": f"{window.end.timestamp():.3f}",
                "step": max(1, step_seconds),
            }
        )
        req = request.Request(
            url=f"{self.base_url}/api/v1/query_range?{params}",
            headers={"Accept": "application/json"},
            method="GET",
        )
        context = None if self.verify_ssl else ssl._create_unverified_context()

        try:
            with request.urlopen(req, timeout=self.timeout_seconds, context=context) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore") if exc.fp else ""
            raise ToolError(f"Prometheus error {exc.code}: {detail[:300]}") from exc
        except URLError as exc:
            raise ToolError(f"Prometheus connection error: {exc.reason}") from exc
        except ValueError as exc:
            raise ToolError("Prometheus returned invalid JSON") from exc

        if body.get("status") != "success":
            raise ToolError(f"Prometheus query failed: {body.get('error', 'unknown error')}")

        results = body.get("data", {}).get("result", [])
        points: list[MetricPoint] = []
        if results:
            for raw_ts, raw_value in results[0].get("values", []):
                try:
                    value = float(raw_value)
                except (TypeError, ValueError):
                    continue
                if value != value:  # NaN: no data for that step
                    continue
                points.append(
                    MetricPoint(timestamp=datetime.fromtimestamp(float(raw_ts), tz=timezone.utc), value=value)
                )
        return MetricSeries(service=service, metric=metric, unit=entry.unit, points=points)
