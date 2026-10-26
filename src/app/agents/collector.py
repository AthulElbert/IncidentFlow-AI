"""The fixed evidence plan every investigation runs, through the audited tool proxies."""
from datetime import timedelta
from typing import Any, Callable

from app.agents.evidence import EvidenceLedger
from app.agents.models import IncidentContext
from app.tools.base import TimeWindow, ToolBudgetExceeded, ToolError
from app.tools.instrumentation import AgentTools

METRICS_WINDOW_MINUTES = 60
LOGS_WINDOW_MINUTES = 60
DEPLOYS_WINDOW_MINUTES = 180
MAX_EXTRA_METRICS = 5
LOG_LIMIT = 200


def window_for(ctx: IncidentContext, minutes: float) -> TimeWindow:
    return TimeWindow(start=ctx.occurred_at - timedelta(minutes=minutes), end=ctx.occurred_at)


class _OutOfBudget(Exception):
    pass


def _call(ledger: EvidenceLedger, source: str, fn: Callable[[], Any]) -> Any | None:
    """Run one tool call. A tool error becomes evidence of the failure; a spent budget stops collection."""
    try:
        return fn()
    except ToolError as exc:
        ledger.add_error(source, str(exc))
        return None
    except ToolBudgetExceeded as exc:
        ledger.warnings.append(f"tool budget exhausted during evidence collection: {exc}")
        raise _OutOfBudget() from exc


def collect_evidence(ctx: IncidentContext, tools: AgentTools) -> EvidenceLedger:
    """Metrics first (they establish the onset), then logs, deploys and similar incidents."""
    ledger = EvidenceLedger(ctx.service)
    try:
        _collect(ctx, tools, ledger)
    except _OutOfBudget:
        pass  # keep whatever was gathered; the report carries the warning
    return ledger


def _collect(ctx: IncidentContext, tools: AgentTools, ledger: EvidenceLedger) -> None:
    metric_window = window_for(ctx, METRICS_WINDOW_MINUTES)
    available = _call(ledger, "metrics.available_metrics", lambda: tools.metrics.available_metrics(ctx.service)) or []

    names = [ctx.alert_metric] + [name for name in available if name != ctx.alert_metric][:MAX_EXTRA_METRICS]
    for name in names:
        series = _call(ledger, f"metrics.query({name})", lambda n=name: tools.metrics.query(ctx.service, n, metric_window))
        if series is not None:
            ledger.add_metric(series, source=f"metrics.query({ctx.service}, {name})")

    lines = _call(
        ledger,
        "logs.search",
        lambda: tools.logs.search(ctx.service, "", window_for(ctx, LOGS_WINDOW_MINUTES), limit=LOG_LIMIT),
    )
    if lines is not None:
        ledger.add_logs(lines, source=f"logs.search({ctx.service})")

    deploys = _call(
        ledger,
        "deploys.recent",
        lambda: tools.deploys.recent(ctx.service, window_for(ctx, DEPLOYS_WINDOW_MINUTES)),
    )
    if deploys is not None:
        ledger.add_deploys(deploys, DEPLOYS_WINDOW_MINUTES, source=f"deploys.recent({ctx.service})")

    error_samples = " ".join(
        sample
        for item in ledger.items
        if item.kind == "log_pattern"
        for sample in item.data.get("samples", [])[:1]
    )
    query = f"{ctx.alert_metric} {ctx.alert_message} {error_samples}"
    similar = _call(ledger, "memory.search", lambda: tools.memory.search(query, service=ctx.service, k=3))
    if similar is not None:
        ledger.add_similar(similar, source="memory.search")
