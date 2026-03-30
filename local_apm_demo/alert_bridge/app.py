from datetime import datetime, timezone
from threading import Lock

from fastapi import FastAPI, Request

app = FastAPI(title="Local APM Alert Bridge")
_lock = Lock()
_alerts: dict[str, dict] = {}


def _to_event(alert: dict) -> dict:
    labels = alert.get("labels", {}) if isinstance(alert, dict) else {}
    annotations = alert.get("annotations", {}) if isinstance(alert, dict) else {}
    status = str(alert.get("status", "firing")).lower()
    starts_at = str(alert.get("startsAt", "")).strip()
    source_alert_id = (
        str(labels.get("alertname", "alert")).strip()
        + "-"
        + str(labels.get("service", "sample-app")).strip()
        + "-"
        + starts_at
    )
    metric = str(labels.get("metric", "generic_alert_score")).strip()
    threshold_raw = str(labels.get("threshold", "1.0")).strip()
    try:
        threshold = float(threshold_raw)
    except ValueError:
        threshold = 1.0
    value = threshold * (1.5 if status == "firing" else 0.5)

    summary = str(annotations.get("summary", labels.get("alertname", "alert"))).strip()
    description = str(annotations.get("description", "")).strip()
    message = summary if not description else f"{summary} - {description}"
    return {
        "source_alert_id": source_alert_id,
        "service": str(labels.get("service", "sample-app")).strip(),
        "metric": metric,
        "value": value,
        "threshold": threshold,
        "environment": "local",
        "timestamp": starts_at or datetime.now(timezone.utc).isoformat(),
        "message": message,
        "status": status,
    }


@app.get("/health")
def health() -> dict:
    with _lock:
        count = len(_alerts)
    return {"status": "ok", "active_alerts": count}


@app.post("/webhook")
async def ingest_alertmanager(payload: Request) -> dict:
    body = await payload.json()
    raw_alerts = body.get("alerts", []) if isinstance(body, dict) else []
    accepted = 0
    with _lock:
        for item in raw_alerts:
            alert = _to_event(item)
            if alert["status"] != "firing":
                _alerts.pop(alert["source_alert_id"], None)
                continue
            _alerts[alert["source_alert_id"]] = alert
            accepted += 1
    return {"status": "received", "alerts": accepted}


@app.get("/v1/alerts")
def list_alerts(limit: int = 20) -> list[dict]:
    with _lock:
        values = list(_alerts.values())[: max(0, limit)]
    return values
