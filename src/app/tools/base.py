from datetime import datetime, timedelta, timezone

from pydantic import BaseModel, model_validator


class ToolError(RuntimeError):
    """A tool call failed in a way an agent can read and react to (bad query, backend error)."""


class ToolUnavailableError(ToolError):
    """The configured backend does not support this operation."""


class ToolNotPermitted(RuntimeError):
    """The calling agent is not allowed to use this tool or method."""


class ToolBudgetExceeded(RuntimeError):
    """The run has used up its tool-call budget."""


class TimeWindow(BaseModel):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def _check(self) -> "TimeWindow":
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("TimeWindow datetimes must be timezone-aware")
        if self.start >= self.end:
            raise ValueError("TimeWindow start must be before end")
        return self

    @classmethod
    def last(cls, minutes: float, end: datetime | None = None) -> "TimeWindow":
        stop = end or datetime.now(timezone.utc)
        return cls(start=stop - timedelta(minutes=minutes), end=stop)

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment <= self.end
