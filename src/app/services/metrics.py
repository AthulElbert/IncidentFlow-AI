from collections import Counter
from app.models.schemas import ChangeRecord, MetricsSummary


def _to_rate(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 4)


def _avg(values: list[float]) -> float:
    if not values:
        return 0.0
    return round(sum(values) / len(values), 2)


def build_metrics_summary(
    changes: list[ChangeRecord],
    min_confidence_for_prod: float,
) -> MetricsSummary:
    total = len(changes)
    status_counts = Counter(item.status for item in changes)
    deployment_state_counts = Counter(item.deployment_state for item in changes)
    issue_type_counts = Counter(item.issue_type for item in changes)
    triage_mode_counts = Counter(item.triage_mode_used for item in changes)

    warning_count_total = sum(item.warning_count for item in changes)
    warning_events = sum(1 for item in changes if item.warning_count > 0)
    blocked_count = deployment_state_counts.get("blocked_by_policy", 0)
    low_confidence_count = sum(1 for item in changes if item.confidence < min_confidence_for_prod)
    passed_dev = sum(1 for item in changes if item.dev_execution_status == "passed")
    promoted_ok = sum(1 for item in changes if item.deployment_state == "promoted_to_prod")

    dev_durations: list[float] = []
    prod_durations: list[float] = []
    for item in changes:
        if item.dev_executed_at:
            dev_durations.append((item.dev_executed_at - item.created_at).total_seconds())
        if item.promoted_at:
            prod_durations.append((item.promoted_at - item.created_at).total_seconds())

    return MetricsSummary(
        total_changes=total,
        status_counts=dict(status_counts),
        deployment_state_counts=dict(deployment_state_counts),
        issue_type_counts=dict(issue_type_counts),
        triage_mode_counts=dict(triage_mode_counts),
        warning_count_total=warning_count_total,
        warning_rate=_to_rate(warning_events, total),
        policy_block_rate=_to_rate(blocked_count, total),
        avg_confidence=round(sum(item.confidence for item in changes) / total, 4) if total else 0.0,
        low_confidence_count=low_confidence_count,
        dev_success_rate=_to_rate(passed_dev, total),
        promotion_success_rate=_to_rate(promoted_ok, total),
        avg_time_to_dev_seconds=_avg(dev_durations),
        avg_time_to_prod_seconds=_avg(prod_durations),
    )
