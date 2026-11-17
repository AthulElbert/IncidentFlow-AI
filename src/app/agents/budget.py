"""Limits on one agent run as a whole: time, tool calls and model calls.

Each agent already has its own tool-call cap and the Fixer its own step cap. This adds the totals across a run,
which is what a retry loop can otherwise spend without limit: attempts x agents x steps. Exceeding a limit
escalates the change with the reason; it never retries.

Model calls are counted per logical call an agent makes; tokens and cost are counted per physical call the router
reports (retries and fallbacks included), when the provider reports usage. A limit of 0 for tokens or cost means
"not limited".
"""
import threading
import time
from dataclasses import dataclass
from typing import Callable

from app.adapters.llm_usage import LLMCall
from app.tools.instrumentation import ToolBudget


class RunBudgetExceeded(RuntimeError):
    """Raised inside an agent when the run's model-call budget is spent."""


@dataclass(frozen=True)
class BudgetLimits:
    max_seconds: float = 900.0
    max_tool_calls: int = 150
    max_llm_calls: int = 60
    max_tokens: int = 0  # 0 = no limit
    max_cost: float = 0.0  # 0 = no limit; in the currency the endpoint prices are written in


class RunBudget:
    def __init__(self, limits: BudgetLimits, clock: Callable[[], float] = time.monotonic) -> None:
        self.limits = limits
        self._clock = clock
        self._started = clock()
        self.tool_calls = ToolBudget(limits.max_tool_calls)
        self._llm_calls = 0
        self._tokens = 0
        self._cost = 0.0
        self._lock = threading.Lock()

    @property
    def elapsed(self) -> float:
        return self._clock() - self._started

    @property
    def llm_calls(self) -> int:
        return self._llm_calls

    @property
    def tokens(self) -> int:
        return self._tokens

    @property
    def cost(self) -> float:
        return round(self._cost, 6)

    def spend_llm(self) -> None:
        """Count one model call, or raise if the run has used its share of calls, tokens or money."""
        with self._lock:
            spent = self._llm_limit_reached()
            if spent:
                raise RunBudgetExceeded(spent)
            self._llm_calls += 1

    def record_llm_usage(self, call: LLMCall) -> None:
        """Add what one physical model call used. Called by the router's usage sink, after the call."""
        with self._lock:
            self._tokens += call.tokens
            self._cost += call.cost

    def _llm_limit_reached(self) -> str | None:
        if self._llm_calls >= self.limits.max_llm_calls:
            return f"the run used its {self.limits.max_llm_calls} model calls"
        if self.limits.max_tokens and self._tokens >= self.limits.max_tokens:
            return f"the run used its {self.limits.max_tokens} model tokens"
        if self.limits.max_cost and self._cost >= self.limits.max_cost:
            return f"the run used its model spending limit of {self.limits.max_cost:g}"
        return None

    def exceeded(self) -> str | None:
        """Why no further stage can run, in words, or None.

        Only time and tool calls stop a stage from starting. The model limits are enforced where a model call is
        attempted (`spend_llm`), because the stages after a fix (verification, a rules-only review, packaging)
        need no model: a fix that used the last allowed model call must still be verified, not thrown away.
        """
        if self.elapsed >= self.limits.max_seconds:
            return f"the run used its {self.limits.max_seconds:.0f}s time limit"
        if self.tool_calls.used >= self.limits.max_tool_calls:
            return f"the run used its {self.limits.max_tool_calls} tool calls"
        return None

    def llm_limit_reached(self) -> str | None:
        """Why no further model call may be made, in words, or None."""
        with self._lock:
            return self._llm_limit_reached()

    def usage(self) -> dict:
        return {
            "seconds": round(self.elapsed, 1),
            "tool_calls": self.tool_calls.used,
            "llm_calls": self._llm_calls,
            "tokens": self._tokens,
            "cost": self.cost,
            "limits": {
                "seconds": self.limits.max_seconds,
                "tool_calls": self.limits.max_tool_calls,
                "llm_calls": self.limits.max_llm_calls,
                "tokens": self.limits.max_tokens,
                "cost": self.limits.max_cost,
            },
        }
