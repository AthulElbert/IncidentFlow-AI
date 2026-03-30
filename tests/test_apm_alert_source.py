from datetime import datetime

from app.adapters.apm_alert_source import DynatraceAPMAlertSource


def test_dynatrace_alert_source_parses_open_problems(monkeypatch):
    source = DynatraceAPMAlertSource(
        base_url="https://dynatrace.example",
        api_token="token",
        timeout_seconds=5,
        verify_ssl=True,
    )

    sample = [
        {
            "problemId": "PROB-1",
            "title": "High latency detected on checkout service",
            "severityLevel": "PERFORMANCE",
            "status": "OPEN",
            "startTime": 1735732800000,
            "impactedEntities": [{"name": "payments-service"}],
        },
        {
            "problemId": "PROB-2",
            "title": "Resolved issue should be ignored",
            "severityLevel": "ERROR",
            "status": "RESOLVED",
            "startTime": 1735732800000,
            "impactedEntities": [{"name": "orders-service"}],
        },
    ]

    monkeypatch.setattr(source, "_fetch_problems", lambda limit: sample)
    alerts = source.fetch_open_alerts(limit=10)

    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.source_alert_id == "PROB-1"
    assert alert.event.service == "payments-service"
    assert alert.event.metric == "latency_p99_ms"
    assert isinstance(alert.event.timestamp, datetime)
