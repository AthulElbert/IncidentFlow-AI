import json
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.adapters.apm_alert_source import APMAlertSource
from app.services.correlation import AlertCorrelator
from app.services.pipeline import SupportAgentPipeline
from app.storage import atomic_write_text

logger = logging.getLogger("app.scheduler")


class AlertDedupStore:
    def __init__(self, file_path: str) -> None:
        self.path = Path(file_path)
        self._lock = threading.Lock()
        self._seen = self._load()

    def _load(self) -> set[str]:
        if not self.path.exists():
            return set()
        raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, list):
            return set()
        return {str(item).strip() for item in raw if str(item).strip()}

    def _persist(self) -> None:
        atomic_write_text(self.path, json.dumps(sorted(self._seen), indent=2))

    def contains(self, alert_id: str) -> bool:
        with self._lock:
            return alert_id in self._seen

    def add(self, alert_id: str) -> None:
        with self._lock:
            self._seen.add(alert_id)
            self._persist()


@dataclass(frozen=True)
class SchedulerStats:
    runs: int = 0
    alerts_seen: int = 0
    alerts_processed: int = 0
    alerts_skipped_dedup: int = 0
    alerts_correlated: int = 0
    alerts_failed: int = 0
    last_run_at: str = ""
    last_error: str = ""


class IncidentScheduler:
    """Polls the alert source and turns new alerts into incidents and change records.

    It never approves, deploys or promotes anything. Everything after intake is
    driven by people through the API.
    """

    def __init__(
        self,
        alert_source: APMAlertSource,
        pipeline: SupportAgentPipeline,
        dedup_store: AlertDedupStore,
        poll_interval_seconds: int = 30,
        correlator: AlertCorrelator | None = None,
    ) -> None:
        self.alert_source = alert_source
        self.pipeline = pipeline
        self.dedup_store = dedup_store
        self.correlator = correlator
        self.poll_interval_seconds = max(5, poll_interval_seconds)

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats = SchedulerStats()
        # One cycle at a time: the poll loop and POST /scheduler/run-once must not
        # interleave, or the same alert could be processed twice and stats lost.
        self._cycle_lock = threading.Lock()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="incident-scheduler", daemon=True)
        self._thread.start()
        logger.info("scheduler-started", extra={"poll_interval_seconds": self.poll_interval_seconds})

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("scheduler-stopped")

    def status(self) -> dict:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "poll_interval_seconds": self.poll_interval_seconds,
            "stats": self._stats.__dict__,
        }

    def run_once(self) -> dict:
        self._process_cycle()
        return self.status()

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            self._process_cycle()
            self._stop_event.wait(self.poll_interval_seconds)

    def _process_cycle(self) -> None:
        with self._cycle_lock:
            stats = self._stats.__dict__.copy()
            stats["runs"] = int(stats.get("runs", 0)) + 1
            stats["last_run_at"] = datetime.now(timezone.utc).isoformat()
            try:
                alerts = self.alert_source.fetch_open_alerts(limit=20)
                stats["alerts_seen"] = int(stats.get("alerts_seen", 0)) + len(alerts)
                for alert in alerts:
                    if self.dedup_store.contains(alert.source_alert_id):
                        stats["alerts_skipped_dedup"] = int(stats.get("alerts_skipped_dedup", 0)) + 1
                        continue
                    try:
                        if self.correlator is not None:
                            related_change = self.correlator.correlate(alert)
                            if related_change:
                                self.dedup_store.add(alert.source_alert_id)
                                stats["alerts_correlated"] = int(stats.get("alerts_correlated", 0)) + 1
                                continue
                        response = self.pipeline.process_event(alert.event)
                        if not response.metadata.get("change_id"):
                            raise ValueError("pipeline response missing change_id")
                        self.dedup_store.add(alert.source_alert_id)
                        stats["alerts_processed"] = int(stats.get("alerts_processed", 0)) + 1
                    except Exception as exc:
                        stats["alerts_failed"] = int(stats.get("alerts_failed", 0)) + 1
                        stats["last_error"] = str(exc)
                        logger.exception("scheduler-alert-processing-failed", extra={"alert_id": alert.source_alert_id})
            except Exception as exc:
                stats["last_error"] = str(exc)
                logger.exception("scheduler-cycle-failed")
            self._stats = SchedulerStats(**stats)
