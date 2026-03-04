from app.models.schemas import APMEvent


def classify_issue(event: APMEvent) -> tuple[str, float, str]:
    text = f"{event.metric} {event.message}".lower()

    if "latency" in text or "response time" in text:
        return "performance_degradation", 0.91, "High latency detected against threshold"
    if "error" in text or "5xx" in text or "exception" in text:
        return "application_error", 0.89, "Error-rate spike suggests app failure"
    if "cpu" in text or "memory" in text:
        return "resource_saturation", 0.86, "Resource pressure likely causing degradation"
    if "db" in text or "connection" in text or "timeout" in text:
        return "dependency_failure", 0.84, "Downstream dependency instability suspected"

    return "unknown", 0.55, "Insufficient signal; manual triage recommended"
