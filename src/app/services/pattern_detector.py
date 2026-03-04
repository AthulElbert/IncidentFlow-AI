from collections import defaultdict
from datetime import datetime, timedelta

from app.models.schemas import APMEvent


class PatternDetector:
    def __init__(self) -> None:
        self._event_times: dict[str, list[datetime]] = defaultdict(list)

    def detect_recurrence(self, event: APMEvent, window_minutes: int = 180) -> tuple[bool, int]:
        key = f"{event.service}:{event.metric}:{event.environment}"
        now = event.timestamp
        cutoff = now - timedelta(minutes=window_minutes)

        valid_times = [ts for ts in self._event_times[key] if ts >= cutoff]
        valid_times.append(now)
        self._event_times[key] = valid_times

        count = len(valid_times)
        return count >= 3, count
