import threading
from collections import defaultdict

from app.events.bus import Event


class InProcessBus:
    """In-memory bus for tests and single-process demos. Events are lost on restart."""

    def __init__(self, max_events_per_topic: int = 10_000) -> None:
        self.max_events_per_topic = max(1, max_events_per_topic)
        self._events: dict[str, list[Event]] = defaultdict(list)
        self._next_seq: dict[str, int] = defaultdict(lambda: 1)
        self._offsets: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def publish(self, event: Event) -> Event:
        with self._lock:
            seq = self._next_seq[event.topic]
            self._next_seq[event.topic] = seq + 1
            stored = event.model_copy(update={"seq": seq})
            events = self._events[event.topic]
            events.append(stored)
            if len(events) > self.max_events_per_topic:
                del events[: len(events) - self.max_events_per_topic]
            return stored

    def poll(self, topic: str, group: str, limit: int = 100) -> list[Event]:
        with self._lock:
            offset = self._offsets.get((topic, group), 0)
            pending = [event for event in self._events.get(topic, []) if (event.seq or 0) > offset]
            return pending[: max(1, limit)]

    def commit(self, group: str, event: Event) -> None:
        with self._lock:
            key = (event.topic, group)
            self._offsets[key] = max(self._offsets.get(key, 0), event.seq or 0)
