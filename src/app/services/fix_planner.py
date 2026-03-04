from app.models.schemas import APMEvent


def suggest_runbook_actions(issue_type: str, event: APMEvent) -> list[str]:
    if issue_type == "performance_degradation":
        return [
            "Check p95/p99 latency breakdown by endpoint in APM.",
            "Scale service replicas by +1 in dev and verify latency trend.",
            "Temporarily increase upstream timeout by 10-15% in dev config.",
        ]
    if issue_type == "application_error":
        return [
            "Inspect recent deployment diff and rollback candidate changes in dev.",
            "Enable safe-mode feature flags for failing module.",
            "Validate error-rate drop after config toggle in dev.",
        ]
    if issue_type == "resource_saturation":
        return [
            "Increase memory/cpu limits in dev deployment config.",
            "Check garbage collection or memory leak signals from runtime metrics.",
            "Tune worker/thread pool size in app config and retest.",
        ]
    if issue_type == "dependency_failure":
        return [
            "Run dependency health check and connection pool diagnostics.",
            "Update retry/backoff config in dev environment.",
            "Introduce circuit-breaker threshold adjustments in config.",
        ]

    return [
        "Collect expanded logs and trace ids for the failing request path.",
        "Create manual triage task for SRE/on-call with service owner.",
    ]


def build_ticket_text(event: APMEvent, issue_type: str, probable_cause: str, is_recurring: bool) -> tuple[str, str]:
    recurrence = "Recurring" if is_recurring else "New"
    summary = f"[{recurrence}] {event.service} - {issue_type} in {event.environment}"
    description = (
        f"Service: {event.service}\n"
        f"Metric: {event.metric}\n"
        f"Observed value: {event.value}\n"
        f"Threshold: {event.threshold}\n"
        f"Alert message: {event.message}\n"
        f"Probable cause: {probable_cause}\n"
    )
    return summary, description
