import json
import logging
from pathlib import Path
from typing import Protocol

from app.tools.base import TimeWindow
from app.tools.models import LogLine
from app.tools.scenarios import DEFAULT_SCENARIOS, Scenario, scenario_for

logger = logging.getLogger("app.tools.logs")

_MAX_LIMIT = 500


class LogsTool(Protocol):
    def search(self, service: str, query: str, window: TimeWindow, limit: int = 100) -> list[LogLine]:
        """Log lines for a service in the window containing every word of `query`, oldest first."""
        ...


def _matches(line: LogLine, service: str, terms: list[str], window: TimeWindow) -> bool:
    if line.service != service or not window.contains(line.timestamp):
        return False
    haystack = f"{line.level} {line.message}".lower()
    return all(term in haystack for term in terms)


def _search(lines: list[LogLine], service: str, query: str, window: TimeWindow, limit: int) -> list[LogLine]:
    terms = [term.lower() for term in query.split() if term.strip()]
    found = sorted((line for line in lines if _matches(line, service, terms, window)), key=lambda l: l.timestamp)
    return found[-max(1, min(limit, _MAX_LIMIT)):]


class MockLogsTool:
    def __init__(self, scenarios: tuple[Scenario, ...] | list[Scenario] = DEFAULT_SCENARIOS) -> None:
        self.scenarios = tuple(scenarios)

    def search(self, service: str, query: str, window: TimeWindow, limit: int = 100) -> list[LogLine]:
        scenario = scenario_for(service, self.scenarios)
        return _search(scenario.logs, service, query, window, limit) if scenario else []


class FileLogsTool:
    """Searches a JSONL log file: one {"timestamp","service","level","message"} object per line."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def _read(self) -> list[LogLine]:
        if not self.path.exists():
            return []
        lines: list[LogLine] = []
        for number, raw in enumerate(self.path.read_text(encoding="utf-8-sig").splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                lines.append(LogLine(**json.loads(raw)))
            except Exception:
                logger.warning("log-line-unreadable", extra={"path": str(self.path), "line": number})
        return lines

    def search(self, service: str, query: str, window: TimeWindow, limit: int = 100) -> list[LogLine]:
        return _search(self._read(), service, query, window, limit)
