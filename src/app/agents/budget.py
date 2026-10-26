"""Limits on one agent run as a whole: time, tool calls and model calls.

Each agent already has its own tool-call cap and the Fixer its own step cap. This adds the totals across a run,
which is what a retry loop can otherwise spend without limit: attempts x agents x steps. Exceeding a limit
escalates the change with the reason; it never retries.

Model calls are counted for the Fixer, the loop that can run long. The Investigator's and the Critic's model use
is bounded by their own step limits. Tokens are not counted because the model client does not report usage.
"""
import threading
import time
from dataclasses import dataclass
from typing import Callable

from app.tools.instrumentation import ToolBudget


class RunBudgetExceeded(RuntimeError):
    """Raised inside an agent when the run's model-call budget is spent."""


@dataclass(frozen=True)
class BudgetLimits:
    max_seconds: float = 900.0
    max_tool_calls: int = 150
    max_llm_calls: int = 60


class RunBudget:
    def __init__(self, limits: BudgetLimits, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self._clock = clock
        self._started = clock()
        self.tool_calls = ToolBudget(limits.max_tool_calls)
        self._llm_calls = 0
        self._lock = threading.Lock()

    @property
    def elapsed(self) -> float:
        return self._clock() - self._started

    @property
    def llm_calls(self) -> int:
        return self._llm_calls

    def spend_llm(self) -> None:
        """Count one model call, or raise if the run has used its share."""
        with self._lock:
            if self._llm_calls >= self.limits.max_llm_calls:
                raise RunBudgetExceeded(f"the run used its {self.limits.max_llm_calls} model calls")
            self._llm_calls += 1

    def exceeded(self) -> str | None:
        """Which limit the run has hit, in words, or None."""
        if self.elapsed >= self.limits.max_seconds:
            return f"the run used its {self.limits.max_seconds:.0f}s time limit"
        if self.tool_calls.used >= self.limits.max_tool_calls:
            return f"the run used its {self.limits.max_tool_calls} tool calls"
        if self._llm_calls >= self.limits.max_llm_calls:
            return f"the run used its {self.limits.max_llm_calls} model calls"
        return None

    def usage(self) -> dict:
        return {
            "seconds": round(self.elapsed, 1),
            "tool_calls": self.tool_calls.used,
            "llm_calls": self._llm_calls,
            "limits": {
                "seconds": self.limits.max_seconds,
                "tool_calls": self.limits.max_tool_calls,
                "llm_calls": self.limits.max_llm_calls,
            },
        }
