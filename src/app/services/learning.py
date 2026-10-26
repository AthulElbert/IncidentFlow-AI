"""Learning from outcomes: what happened to a change becomes what incident memory knows.

The pipeline files every new incident as `open`, with no real resolution. When a person releases the fix, the
incident is marked `resolved` and its resolution is written from facts on the change (what was changed, how it
was verified, who approved and released it). When a person rejects the proposed fix the incident is marked
`rejected`, which keeps the note but stops it from being offered as evidence. Nothing else becomes memory:
an escalation, a failed deploy or a fix still waiting for a decision is not an outcome.

Delivery from the event bus is at-least-once and the startup sweep re-reads the change store, so learning the
same outcome twice must change nothing.
"""
import logging
from datetime import datetime, timezone

from app.events.bus import INCIDENT_EVENTS_TOPIC, Event, EventBus, drain
from app.models.schemas import ChangeRecord, IncidentRecord
from app.services.change_control import ChangeControlStore
from app.services.knowledge_base import IncidentKnowledgeBase

logger = logging.getLogger("app.learning")

LEARNING_GROUP = "learning"
MAX_RESOLUTION_CHARS = 700
_OUTCOME_STATES = {"promoted_to_prod", "closed_rejected"}


def resolution_text(record: ChangeRecord) -> str:
    """What was done, from the change's own facts. No model writes this."""
    run = record.agent_run or {}
    attempts = run.get("attempts") or []
    fix = (attempts[-1].get("fix") or {}) if attempts else {}
    parts: list[str] = []

    if fix.get("status") == "proposed":
        files = run.get("changed_files") or fix.get("files") or []
        what = f"Fixed by an agent-proposed change to {', '.join(files)}" if files else "Fixed by an agent-proposed change"
        if fix.get("summary"):
            what += f": {fix['summary']}"
        parts.append(what.rstrip(".") + ".")
        metric = (run.get("verification") or {}).get("alert_metric") or {}
        if "before" in metric and "after" in metric:
            parts.append(f"In dev the alert metric went from {metric['before']} to {metric['after']} (threshold {metric.get('threshold')}).")
    else:
        actions = "; ".join(record.proposed_actions[:3])
        parts.append("Resolved by releasing the validated change" + (f" ({actions})." if actions else "."))

    who = [f"approved by {record.decided_by}" if record.decided_by else "", f"released by {record.promoted_by}" if record.promoted_by else ""]
    who_text = ", ".join(item for item in who if item)
    if who_text:
        parts.append(who_text[0].upper() + who_text[1:] + ".")
    return " ".join(parts)[:MAX_RESOLUTION_CHARS]


class OutcomeLearner:
    def __init__(self, change_store: ChangeControlStore, kb: IncidentKnowledgeBase, bus: EventBus | None = None) -> None:
        self.change_store = change_store
        self.kb = kb
        self.bus = bus

    def learn(self, change_id: str) -> str | None:
        """Record this change's outcome on its incident. Returns the outcome written, or None if there was nothing to do."""
        record = self.change_store.get_change(change_id)
        if record is None or record.deployment_state not in _OUTCOME_STATES:
            return None
        incident = self.kb.get(record.incident_id)
        if incident is None or incident.outcome != "open":
            return None  # unknown, a seeded incident, or already learned

        now = datetime.now(timezone.utc)
        if record.deployment_state == "promoted_to_prod":
            updated = incident.model_copy(
                update={"outcome": "resolved", "outcome_at": now, "resolution": resolution_text(record),
                        "outcome_note": f"released as {record.change_id}"}
            )
        else:
            note = f"proposed fix rejected by {record.decided_by or 'a person'}" + (f": {record.decision_comment}" if record.decision_comment else "")
            updated = incident.model_copy(update={"outcome": "rejected", "outcome_at": now, "outcome_note": note[:400]})
        self.kb.update_record(updated)
        logger.info("outcome-learned", extra={"change_id": change_id, "incident_id": incident.incident_id, "outcome": updated.outcome})
        return updated.outcome

    def reconcile(self) -> list[str]:
        """Learn every outcome the change store already holds. Run at startup, since the bus may not have survived."""
        learned: list[str] = []
        for record in self.change_store.list_changes():
            if self.learn(record.change_id):
                learned.append(record.change_id)
        return learned

    def handle(self, event: Event) -> None:
        change_id = event.payload.get("change_id")
        if change_id and event.payload.get("to_state") in _OUTCOME_STATES:
            self.learn(change_id)

    def run_once(self) -> int:
        """Handle the events published since the last run. Returns how many were handled."""
        if self.bus is None:
            return 0
        return drain(self.bus, INCIDENT_EVENTS_TOPIC, LEARNING_GROUP, self.handle)
