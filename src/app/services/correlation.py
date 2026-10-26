from app.adapters.apm_alert_source import APMAlert
from app.services.change_control import ChangeControlStore


class AlertCorrelator:
    """Folds related alerts into one change instead of opening one per alert.

    Two alerts are related when they come from the same service and environment
    and the earlier one's change is still unapproved and was last touched within
    `window_minutes`. A window of 0 disables correlation.
    """

    def __init__(self, change_store: ChangeControlStore, window_minutes: float, actor: str = "scheduler-agent") -> None:
        self.change_store = change_store
        self.window_minutes = window_minutes
        self.actor = actor

    def correlate(self, alert: APMAlert) -> str | None:
        """Attach the alert to an existing open change and return its id, or None if there isn't one."""
        existing = self.change_store.find_open_change(
            service=alert.event.service,
            environment=alert.event.environment,
            within_minutes=self.window_minutes,
        )
        if existing is None:
            return None
        self.change_store.attach_alert(
            existing.change_id,
            source_alert_id=alert.source_alert_id,
            metric=alert.event.metric,
            actor=self.actor,
        )
        return existing.change_id
