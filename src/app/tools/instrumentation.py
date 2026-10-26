"""Least-privilege, audited access to tools for an agent run.

Agents never hold a raw tool. `Toolkit.for_agent` hands each agent proxies that:
  - only expose the methods that agent is allowed to call,
  - count every call against a per-run budget,
  - write an audit entry for every call (what, with which arguments, how long,
    a hash of the result, or the error) under the agent's own actor name.
"""
import hashlib
import json
import threading
import time
import weakref
from dataclasses import dataclass, field
from typing import Any

from app.audit import AuditLog, new_audit_entry
from app.tools.base import ToolBudgetExceeded, ToolNotPermitted
from app.tools.deploy_target import DeployTarget
from app.tools.deploys import DeployHistoryTool
from app.tools.logs import LogsTool
from app.tools.memory import IncidentMemory
from app.tools.metrics import MetricsTool
from app.tools.sandbox import RepoSandbox

_MAX_ARG_CHARS = 200

# agent -> tool -> methods it may call. Canary and rollback are deliberately in nobody's
# list: production changes go through the governance layer, not through an agent tool.
AGENT_PERMISSIONS: dict[str, dict[str, frozenset[str]]] = {
    "investigator": {
        "metrics": frozenset({"available_metrics", "query"}),
        "logs": frozenset({"search"}),
        "deploys": frozenset({"recent"}),
        "memory": frozenset({"search"}),
    },
    "fixer": {
        "sandbox": frozenset({"list_files", "read", "grep", "edit", "diff"}),
        "memory": frozenset({"search"}),
    },
    "verifier": {
        "sandbox": frozenset({"list_files", "read", "grep", "diff", "run_tests"}),
        "deploy_target": frozenset({"deploy_dev", "get_status"}),
        "metrics": frozenset({"available_metrics", "query"}),
    },
    "critic": {
        "sandbox": frozenset({"list_files", "read", "grep", "diff"}),
        "deploys": frozenset({"recent"}),
        "memory": frozenset({"search"}),
    },
}


class ToolBudget:
    def __init__(self, max_calls: int) -> None:
        self.max_calls = max(1, max_calls)
        self.used = 0
        self._lock = threading.Lock()

    def spend(self) -> bool:
        """Count one call; False if the budget is already used up."""
        with self._lock:
            if self.used >= self.max_calls:
                return False
            self.used += 1
            return True


@dataclass
class ToolContext:
    actor: str
    entity_id: str
    budget: ToolBudget
    audit: AuditLog | None = None


def _shorten(value: Any) -> str:
    text = repr(value) if not isinstance(value, str) else value
    return text if len(text) <= _MAX_ARG_CHARS else text[:_MAX_ARG_CHARS] + "..."


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _result_digest(result: Any) -> tuple[str, int | None]:
    encoded = json.dumps(_jsonable(result), sort_keys=True, default=str).encode("utf-8")
    size = len(result) if isinstance(result, (list, tuple, str, dict)) else None
    return hashlib.sha256(encoded).hexdigest()[:16], size


@dataclass(frozen=True)
class _Guarded:
    tool: Any
    name: str
    allowed: frozenset[str]
    context: ToolContext


# The raw tool lives here, not on the proxy, so `proxy._tool` cannot hand it out. This stops
# accidental bypass of the allow-list and the audit log; it is not a boundary against
# hostile Python code running in-process.
_GUARDED: "weakref.WeakKeyDictionary[ToolProxy, _Guarded]" = weakref.WeakKeyDictionary()


def _record(guard: _Guarded, action: str, details: dict[str, Any]) -> None:
    if guard.context.audit is None:
        return
    guard.context.audit.append(
        new_audit_entry(
            actor=guard.context.actor,
            action=action,
            entity_id=guard.context.entity_id,
            entity_type="incident",
            details=details,
        )
    )


class ToolProxy:
    def __init__(self, tool: Any, name: str, allowed: frozenset[str], context: ToolContext) -> None:
        _GUARDED[self] = _Guarded(tool, name, allowed, context)

    def __getattr__(self, method: str):
        if method.startswith("_"):
            raise AttributeError(method)
        guard = _GUARDED[self]
        context, name = guard.context, guard.name

        if method not in guard.allowed:
            _record(guard, f"tool.{name}.{method}.denied", {"reason": "not permitted for this agent"})
            raise ToolNotPermitted(f"{context.actor} may not call {name}.{method}")

        target = getattr(guard.tool, method)
        if not callable(target):
            raise AttributeError(method)

        def call(*args: Any, **kwargs: Any) -> Any:
            arguments = {"args": [_shorten(arg) for arg in args], "kwargs": {k: _shorten(v) for k, v in kwargs.items()}}
            if not context.budget.spend():
                _record(guard, f"tool.{name}.{method}.denied", {**arguments, "reason": "tool-call budget exhausted"})
                raise ToolBudgetExceeded(f"{context.actor} exceeded its budget of {context.budget.max_calls} tool calls")

            started = time.monotonic()
            try:
                result = target(*args, **kwargs)
            except Exception as exc:
                _record(
                    guard,
                    f"tool.{name}.{method}",
                    {
                        **arguments,
                        "ok": False,
                        "error": _shorten(f"{type(exc).__name__}: {exc}"),
                        "duration_ms": round((time.monotonic() - started) * 1000, 1),
                    },
                )
                raise

            digest, size = _result_digest(result)
            _record(
                guard,
                f"tool.{name}.{method}",
                {
                    **arguments,
                    "ok": True,
                    "result_hash": digest,
                    "result_size": size,
                    "duration_ms": round((time.monotonic() - started) * 1000, 1),
                },
            )
            return result

        return call


class AgentTools:
    """The tools one agent may use, as attributes: `tools.metrics.query(...)`."""

    def __init__(self, agent: str, proxies: dict[str, ToolProxy], budget: ToolBudget | None = None) -> None:
        self._agent = agent
        self._proxies = proxies
        self._budget = budget

    @property
    def names(self) -> list[str]:
        return sorted(self._proxies)

    @property
    def calls_used(self) -> int:
        return self._budget.used if self._budget else 0

    def __getattr__(self, name: str) -> ToolProxy:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return self._proxies[name]
        except KeyError:
            raise ToolNotPermitted(f"{self._agent} has no access to the '{name}' tool") from None


@dataclass
class Toolkit:
    metrics: MetricsTool
    logs: LogsTool
    deploys: DeployHistoryTool
    memory: IncidentMemory
    deploy_target: DeployTarget
    max_calls_per_run: int = 40
    permissions: dict[str, dict[str, frozenset[str]]] = field(default_factory=lambda: dict(AGENT_PERMISSIONS))

    def for_agent(
        self,
        agent: str,
        entity_id: str,
        audit: AuditLog | None = None,
        budget: ToolBudget | None = None,
        sandbox: RepoSandbox | None = None,
    ) -> AgentTools:
        if agent not in self.permissions:
            raise ToolNotPermitted(f"unknown agent '{agent}'")
        context = ToolContext(
            actor=f"agent:{agent}",
            entity_id=entity_id,
            budget=budget or ToolBudget(self.max_calls_per_run),
            audit=audit,
        )
        raw: dict[str, Any] = {
            "metrics": self.metrics,
            "logs": self.logs,
            "deploys": self.deploys,
            "memory": self.memory,
            "deploy_target": self.deploy_target,
            "sandbox": sandbox,
        }
        proxies = {
            tool_name: ToolProxy(raw[tool_name], tool_name, methods, context)
            for tool_name, methods in self.permissions[agent].items()
            if raw.get(tool_name) is not None
        }
        return AgentTools(agent, proxies, context.budget)
