"""Deterministic incident scenarios.

Each scenario is a self-consistent story (metrics, logs and recent deploys that
agree with each other) with a known answer. The mock tools serve them, and the
Phase 3 replay/eval harness uses `expected_*` to score the Investigator.
Everything is a pure function of the scenario, so runs are reproducible.
"""
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.tools.base import TimeWindow
from app.tools.models import DeployRecord, LogLine, MetricPoint, MetricSeries

ANCHOR = datetime(2026, 7, 21, 14, 30, tzinfo=timezone.utc)


@dataclass(frozen=True)
class MetricShape:
    """A flat baseline that ramps to `peak` starting `onset_minutes` before the anchor."""

    baseline: float
    peak: float
    onset_minutes: float
    unit: str = ""
    noise: float = 0.03
    ramp_minutes: float = 3.0


@dataclass(frozen=True)
class Scenario:
    name: str
    service: str
    environment: str
    alert_metric: str
    alert_message: str
    metrics: dict[str, MetricShape]
    logs: list[LogLine]
    deploys: list[DeployRecord]
    expected_issue_type: str
    expected_root_cause: str
    anchor: datetime = ANCHOR
    tags: tuple[str, ...] = field(default_factory=tuple)

    def metric_series(self, metric: str, window: TimeWindow, step_seconds: int = 60) -> MetricSeries | None:
        shape = self.metrics.get(metric)
        if shape is None:
            return None
        onset = self.anchor - timedelta(minutes=shape.onset_minutes)
        points: list[MetricPoint] = []
        step = max(1, step_seconds)
        moment = window.start
        while moment <= window.end:
            progress = (moment - onset).total_seconds() / (shape.ramp_minutes * 60)
            level = shape.baseline + (shape.peak - shape.baseline) * min(1.0, max(0.0, progress))
            jitter = random.Random(f"{self.name}:{metric}:{int(moment.timestamp()) // step}").uniform(-1, 1)
            points.append(MetricPoint(timestamp=moment, value=round(max(0.0, level * (1 + shape.noise * jitter)), 4)))
            moment += timedelta(seconds=step)
        return MetricSeries(service=self.service, metric=metric, unit=shape.unit, points=points)


def _log(minutes_ago: float, service: str, level: str, message: str) -> LogLine:
    return LogLine(timestamp=ANCHOR - timedelta(minutes=minutes_ago), service=service, level=level, message=message)


BAD_DEPLOY_ERROR_SPIKE = Scenario(
    name="bad_deploy_error_spike",
    service="orders-service",
    environment="prod",
    alert_metric="error_rate_pct",
    alert_message="5xx rate above 2% on orders-service",
    metrics={
        "error_rate_pct": MetricShape(baseline=0.4, peak=7.5, onset_minutes=29, unit="percent"),
        "latency_p95_ms": MetricShape(baseline=180, peak=210, onset_minutes=29, unit="ms"),
        "cpu_pct": MetricShape(baseline=41, peak=44, onset_minutes=29, unit="percent"),
    },
    logs=[
        _log(40, "orders-service", "INFO", "order submitted ok order_id=88121"),
        _log(31, "orders-service", "INFO", "deploy complete version=2026.07.21-3"),
        _log(28, "orders-service", "ERROR", "NullPointerException in CheckoutSubmitHandler.applyPromo at /checkout/submit"),
        _log(27, "orders-service", "ERROR", "NullPointerException in CheckoutSubmitHandler.applyPromo at /checkout/submit"),
        _log(25, "orders-service", "ERROR", "HTTP 500 POST /checkout/submit promo_code=None"),
        _log(12, "orders-service", "ERROR", "NullPointerException in CheckoutSubmitHandler.applyPromo at /checkout/submit"),
        _log(3, "orders-service", "WARN", "error budget burn rate 14x for orders-service"),
    ],
    deploys=[
        DeployRecord(
            service="orders-service",
            ref="2026.07.21-3",
            deployed_at=ANCHOR - timedelta(minutes=31),
            deployed_by="ci-bot",
            kind="code",
            summary="Add promo code handling to checkout submit",
        )
    ],
    expected_issue_type="application_error",
    expected_root_cause="regression in the 2026.07.21-3 deploy: NullPointerException in checkout promo handling",
    tags=("deploy-correlated",),
)

DEPENDENCY_TIMEOUTS = Scenario(
    name="dependency_timeouts",
    service="checkout-service",
    environment="prod",
    alert_metric="dependency_timeout_ms",
    alert_message="inventory-db call latency above 1500ms",
    metrics={
        "dependency_timeout_ms": MetricShape(baseline=220, peak=3200, onset_minutes=17, unit="ms"),
        "error_rate_pct": MetricShape(baseline=0.3, peak=4.1, onset_minutes=15, unit="percent"),
        "cpu_pct": MetricShape(baseline=38, peak=40, onset_minutes=17, unit="percent"),
    },
    logs=[
        _log(18, "checkout-service", "WARN", "inventory-db query slow duration_ms=1840"),
        _log(16, "checkout-service", "ERROR", "timeout contacting inventory-db after 3000ms"),
        _log(15, "checkout-service", "ERROR", "connection pool exhausted for inventory-db (50/50 in use)"),
        _log(9, "checkout-service", "ERROR", "timeout contacting inventory-db after 3000ms"),
        _log(2, "checkout-service", "ERROR", "circuit breaker open for inventory-db"),
    ],
    deploys=[],
    expected_issue_type="dependency_failure",
    expected_root_cause="inventory-db is slow and the connection pool is exhausted; no recent deploy",
    tags=("no-deploy",),
)

BAD_CONFIG_LATENCY = Scenario(
    name="bad_config_latency",
    service="faulty-project",
    environment="local",
    alert_metric="latency_p95_seconds",
    alert_message="Faulty project checkout latency p95 is high",
    metrics={
        "latency_p95_seconds": MetricShape(baseline=0.25, peak=2.2, onset_minutes=12, unit="seconds"),
        "error_rate_per_sec": MetricShape(baseline=0.01, peak=0.31, onset_minutes=12, unit="req/s"),
    },
    logs=[
        _log(14, "faulty-project", "INFO", "config reloaded path=/app/app_config.json latency_ms=1800 error_probability=0.25"),
        _log(11, "faulty-project", "WARN", "slow checkout response elapsed_ms=1790 (latency_ms=1800)"),
        _log(8, "faulty-project", "ERROR", "intentional faulty checkout (error_probability=0.25)"),
        _log(4, "faulty-project", "WARN", "slow checkout response elapsed_ms=1822 (latency_ms=1800)"),
    ],
    deploys=[
        DeployRecord(
            service="faulty-project",
            ref="config:app_config.json@a41c9e2",
            deployed_at=ANCHOR - timedelta(minutes=14),
            deployed_by="ops-user",
            kind="config",
            summary="app_config.json: latency_ms 120 -> 1800, error_probability 0.01 -> 0.25",
        )
    ],
    expected_issue_type="performance_degradation",
    expected_root_cause="config change set latency_ms=1800 and error_probability=0.25 in app_config.json",
    tags=("config-change", "deploy-correlated"),
)

DEFAULT_SCENARIOS: tuple[Scenario, ...] = (BAD_DEPLOY_ERROR_SPIKE, DEPENDENCY_TIMEOUTS, BAD_CONFIG_LATENCY)


def scenario_for(service: str, scenarios: tuple[Scenario, ...] | list[Scenario] = DEFAULT_SCENARIOS) -> Scenario | None:
    return next((item for item in scenarios if item.service == service), None)
