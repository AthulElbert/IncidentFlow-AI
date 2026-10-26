"""Event bus interface.

Pull-based with explicit offset commits, which maps directly onto Kafka consumer
groups: `poll` returns events after the group's committed offset and `commit`
advances it. Delivery is at-least-once: an event that is polled but not
committed is delivered again, so consumers must be idempotent.
"""
from datetime import datetime, timezone
from typing import Any, Callable, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

INCIDENT_EVENTS_TOPIC = "incident.events"


class Event(BaseModel):
    event_id: str
    topic: str
    key: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    seq: int | None = None


def make_event(topic: str, key: str, payload: dict[str, Any] | None = None) -> Event:
    return Event(
        event_id=f"EVT-{uuid4().hex[:16].upper()}",
        topic=topic,
        key=key,
        payload=payload or {},
        created_at=datetime.now(timezone.utc),
    )


class EventBus(Protocol):
    def publish(self, event: Event) -> Event:
        ...

    def poll(self, topic: str, group: str, limit: int = 100) -> list[Event]:
        ...

    def commit(self, group: str, event: Event) -> None:
        ...


def drain(bus: EventBus, topic: str, group: str, handler: Callable[[Event], None], limit: int = 100) -> int:
    """Hand pending events to `handler`, committing each only after it returns.

    If the handler raises, that event stays uncommitted (it will be redelivered)
    and the exception propagates; earlier events in the batch stay committed.
    """
    handled = 0
    for event in bus.poll(topic, group, limit):
        handler(event)
        bus.commit(group, event)
        handled += 1
    return handled
