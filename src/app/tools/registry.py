"""Every tool an agent can use, declared once.

From these declarations come:
- each agent's permissions (which tool methods its proxy lets through),
- which of those a *model* may ask for, and the example lines its prompt shows,
- the argument schema a model's request is checked against before anything runs.

A handler, the code that turns a tool's result into what the agent needs, stays with the agent (the
Investigator turns results into evidence, the Fixer returns file contents). A tool or method that is not
declared here cannot be called by any agent.

Rule enforced at import: a model may only request a tool that writes if the write lands inside its own
disposable worktree (`sandbox.edit`). Deploying, approving and promoting are never model-requestable.
"""
import json
from dataclasses import dataclass, field
from typing import Any, Literal


class ToolRequestError(ValueError):
    """A model asked for something it may not have, or with arguments that do not fit. The message is for the model."""


@dataclass(frozen=True)
class Arg:
    name: str
    type: Literal["string", "integer", "number"]
    required: bool = False
    max_length: int | None = None
    minimum: float | None = None
    maximum: float | None = None


@dataclass(frozen=True)
class ToolSpec:
    tool: str
    method: str
    effect: Literal["read", "write"]
    agents: frozenset[str]  # agents whose proxy may call it at all (from code or on a model's request)
    model_agents: frozenset[str] = frozenset()  # agents whose *model* may request it
    args: tuple[Arg, ...] = ()
    example: dict[str, Any] = field(default_factory=dict)  # shown to the model; must validate

    @property
    def id(self) -> str:
        return f"{self.tool}.{self.method}"


def _spec(tool, method, effect, agents, model_agents=(), args=(), example=None) -> ToolSpec:
    return ToolSpec(tool, method, effect, frozenset(agents), frozenset(model_agents), tuple(args), example or {})


_SERVICE = Arg("service", "string", max_length=80)
_MINUTES = Arg("minutes", "number", minimum=5, maximum=240)

REGISTRY: tuple[ToolSpec, ...] = (
    # --- production telemetry (read) --------------------------------------------------------------
    _spec("metrics", "available_metrics", "read", {"investigator", "verifier"}),
    _spec("metrics", "query", "read", {"investigator", "verifier"}, {"investigator"},
          [Arg("metric", "string", required=True, max_length=80), _MINUTES, _SERVICE],
          {"metric": "cpu_pct", "minutes": 60}),
    _spec("logs", "search", "read", {"investigator"}, {"investigator"},
          [Arg("query", "string", max_length=100), _MINUTES, _SERVICE],
          {"query": "timeout", "minutes": 30}),
    _spec("deploys", "recent", "read", {"investigator", "critic"}, {"investigator"},
          [_MINUTES, _SERVICE], {"minutes": 180}),
    _spec("memory", "search", "read", {"investigator", "fixer", "critic"}, {"investigator", "fixer"},
          [Arg("text", "string", required=True, max_length=300), _SERVICE],
          {"text": "connection pool exhausted"}),
    # --- the worktree ---------------------------------------------------------------------------------
    _spec("sandbox", "list_files", "read", {"fixer", "verifier", "critic"}, {"fixer"},
          [Arg("subdir", "string", max_length=200)], {"subdir": "config"}),
    _spec("sandbox", "read", "read", {"fixer", "verifier", "critic"}, {"fixer"},
          [Arg("path", "string", required=True, max_length=200)], {"path": "config/app.json"}),
    _spec("sandbox", "grep", "read", {"fixer", "verifier", "critic"}, {"fixer"},
          [Arg("pattern", "string", required=True, max_length=200), Arg("subdir", "string", max_length=200)],
          {"pattern": "latency_ms", "subdir": ""}),
    _spec("sandbox", "edit", "write", {"fixer"}, {"fixer"},
          [Arg("path", "string", required=True, max_length=200), Arg("content", "string", required=True, max_length=200_000)],
          {"path": "config/app.json", "content": "<the COMPLETE new file content>"}),
    _spec("sandbox", "diff", "read", {"fixer", "verifier", "critic"}, {"fixer"}),
    _spec("sandbox", "run_tests", "write", {"verifier"}),
    # --- the dev environment (the Verifier, from code only) -------------------------------------------
    _spec("deploy_target", "deploy_dev", "write", {"verifier"}),
    _spec("deploy_target", "get_status", "read", {"verifier"}),
    _spec("dev_metrics", "available_metrics", "read", {"verifier"}),
    _spec("dev_metrics", "query", "read", {"verifier"}),
)

AGENTS = ("investigator", "fixer", "verifier", "critic")


def _check_registry() -> None:
    seen: set[str] = set()
    for spec in REGISTRY:
        if spec.id in seen:
            raise AssertionError(f"tool {spec.id} is declared twice")
        seen.add(spec.id)
        if not spec.model_agents <= spec.agents:
            raise AssertionError(f"{spec.id}: an agent's model may only request what the agent itself may call")
        if spec.model_agents and spec.effect == "write" and spec.tool != "sandbox":
            raise AssertionError(f"{spec.id}: a model may only request a write inside its own worktree")
        if spec.model_agents:
            validate_args(spec, dict(spec.example))  # every example a model is shown must itself be valid


def permissions() -> dict[str, dict[str, frozenset[str]]]:
    """agent -> tool -> methods its proxy lets through. Derived from the registry; nothing else grants access."""
    result: dict[str, dict[str, set[str]]] = {agent: {} for agent in AGENTS}
    for spec in REGISTRY:
        for agent in spec.agents:
            result[agent].setdefault(spec.tool, set()).add(spec.method)
    return {agent: {tool: frozenset(methods) for tool, methods in tools.items()} for agent, tools in result.items()}


def model_tools(agent: str) -> list[ToolSpec]:
    return [spec for spec in REGISTRY if agent in spec.model_agents]


def prompt_examples(agent: str, indent: str = "   ") -> str:
    """The example request lines for an agent's prompt, one per tool its model may request."""
    lines = []
    for spec in model_tools(agent):
        request = {"action": "tool", "tool": spec.tool, "method": spec.method, "args": spec.example}
        lines.append(indent + json.dumps(request))
    return "\n".join(lines)


def validate_request(agent: str, request: dict[str, Any]) -> tuple[ToolSpec, dict[str, Any]]:
    """The spec and the checked arguments for a model's tool request, or ToolRequestError saying what is wrong."""
    tool, method = request.get("tool"), request.get("method")
    spec = next((s for s in REGISTRY if (s.tool, s.method) == (tool, method)), None)
    if spec is None or agent not in spec.model_agents:
        allowed = ", ".join(s.id for s in model_tools(agent))
        raise ToolRequestError(f"unsupported request {tool}.{method}; you may use: {allowed}")
    from app.agents.models import tool_args  # args inside "args", or flattened beside the tool name

    return spec, validate_args(spec, tool_args(request))


def validate_args(spec: ToolSpec, raw: dict[str, Any]) -> dict[str, Any]:
    """Check arguments against the spec. Unknown arguments are dropped, never passed on; numbers are kept in range."""
    checked: dict[str, Any] = {}
    for arg in spec.args:
        if arg.name not in raw or raw[arg.name] is None:
            if arg.required:
                raise ToolRequestError(f"{spec.id} needs '{arg.name}'")
            continue
        value = raw[arg.name]
        if arg.type == "string":
            if not isinstance(value, str):
                raise ToolRequestError(f"{spec.id}: '{arg.name}' must be a string")
            if arg.max_length is not None and len(value) > arg.max_length:
                raise ToolRequestError(f"{spec.id}: '{arg.name}' is longer than {arg.max_length} characters")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                try:
                    value = float(value) if arg.type == "number" else int(value)
                except (TypeError, ValueError):
                    raise ToolRequestError(f"{spec.id}: '{arg.name}' must be a {arg.type}") from None
            if arg.type == "integer":
                value = int(value)
            # Out-of-range numbers are brought into range rather than refused (a 300-minute window becomes 240):
            # the request is still meaningful, and refusing it would only cost the model a step.
            if arg.minimum is not None:
                value = max(arg.minimum, value)
            if arg.maximum is not None:
                value = min(arg.maximum, value)
            if arg.type == "integer":
                value = int(value)
        checked[arg.name] = value
    return checked


_check_registry()
