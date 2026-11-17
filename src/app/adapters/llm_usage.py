"""What each model call cost, and a way for the code running an agent to collect it.

The router reports every physical call (retries and fallbacks included) to whatever sink the current task has
set. An agent run sets one so tokens and cost count against the run's budget and reach the audit log; outside a
run nothing is set and reports are dropped. A context variable carries it, so concurrent runs never mix.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Callable, Iterator


@dataclass(frozen=True)
class LLMCall:
    profile: str
    endpoint: str
    model: str
    ok: bool
    latency_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    usage_reported: bool = False  # False when the provider returned no token counts
    cost: float = 0.0
    error: str = ""

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class LLMTaskContext:
    sink: Callable[[LLMCall], None] | None = None
    attempt: int = 1  # which fix attempt the task belongs to; lets routing use a stronger model on a retry


_context: ContextVar[LLMTaskContext] = ContextVar("llm_task_context", default=LLMTaskContext())


def current_llm_context() -> LLMTaskContext:
    return _context.get()


@contextmanager
def llm_task(sink: Callable[[LLMCall], None] | None = None, attempt: int = 1) -> Iterator[None]:
    """Collect the model calls made inside this block."""
    token = _context.set(LLMTaskContext(sink=sink, attempt=attempt))
    try:
        yield
    finally:
        _context.reset(token)


def report_llm_call(call: LLMCall) -> None:
    sink = _context.get().sink
    if sink is not None:
        sink(call)
