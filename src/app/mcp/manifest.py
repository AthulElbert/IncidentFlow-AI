"""The reviewed list of MCP servers and which of their tools may answer which of our tool methods.

The manifest is the only source of truth about a server. Nothing a server says about itself (tool descriptions,
"read-only" hints) is trusted or ever shown to a model: a description is the easiest place to hide instructions.

Rules checked when the manifest is loaded:
- a binding must be `"effect": "read"`. Anything with side effects (open a PR, create a ticket, trigger a build)
  is reachable only from governed services, never through an agent tool;
- a binding names one of the families and adapters below; the adapter, which is reviewed Python, decides what
  arguments are sent and how the result is read;
- a server's environment is only the variables it names (`env_passthrough`), and its credentials are named by
  environment variable, never written in the file;
- the manifest itself may not sit where agents can edit (checked in `app.config`).
"""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

FAMILIES = {
    # family: the adapters that may answer it
    "metrics": {"prometheus", "incidentflow"},
    "logs": {"incidentflow"},
    "deploys": {"incidentflow"},
}


class ManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ServerSpec:
    name: str
    transport: Literal["stdio", "http"]
    command: tuple[str, ...] = ()
    url: str = ""
    env_passthrough: tuple[str, ...] = ()
    auth_env: str = ""  # http: the environment variable holding a bearer token
    call_timeout_seconds: float = 15.0
    connect_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class Binding:
    family: str
    server: str
    adapter: str
    tools: dict[str, str]  # our operation -> the server's tool name, e.g. {"query": "execute_range_query"}
    max_result_bytes: int = 500_000
    promql: dict[tuple[str, str], tuple[str, str]] = field(default_factory=dict)  # (service, metric) -> (expr, unit)


@dataclass(frozen=True)
class Manifest:
    servers: dict[str, ServerSpec]
    bindings: dict[str, Binding]  # family -> binding


# What each adapter needs the server to provide.
ADAPTER_TOOLS = {
    ("metrics", "prometheus"): {"query"},
    ("metrics", "incidentflow"): {"available_metrics", "query"},
    ("logs", "incidentflow"): {"search"},
    ("deploys", "incidentflow"): {"recent"},
}


def load_manifest(path: str | Path) -> Manifest:
    file = Path(path)
    try:
        raw = json.loads(file.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ManifestError(f"cannot read MCP manifest {file}: {exc}") from exc
    except ValueError as exc:
        raise ManifestError(f"MCP manifest {file} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError("MCP manifest must be a JSON object")
    return parse_manifest(raw)


def parse_manifest(raw: dict) -> Manifest:
    servers: dict[str, ServerSpec] = {}
    for name, spec in (raw.get("servers") or {}).items():
        if not isinstance(spec, dict):
            raise ManifestError(f"server '{name}' must be an object")
        transport = spec.get("transport")
        if transport == "stdio":
            command = spec.get("command")
            if not (isinstance(command, list) and command and all(isinstance(part, str) and part for part in command)):
                raise ManifestError(f"server '{name}': a stdio server needs a non-empty 'command' list")
            server = ServerSpec(name, "stdio", command=tuple(command))
        elif transport == "http":
            url = str(spec.get("url", "")).strip()
            local = url.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))
            if not (url.startswith("https://") or local):
                raise ManifestError(f"server '{name}': an http server needs an https:// url (or a localhost one)")
            if spec.get("auth_env"):
                raise ManifestError(f"server '{name}': authenticated http servers are not supported yet; use stdio")
            server = ServerSpec(name, "http", url=url)
        else:
            raise ManifestError(f"server '{name}': transport must be 'stdio' or 'http'")
        passthrough = spec.get("env_passthrough", [])
        if not (isinstance(passthrough, list) and all(isinstance(v, str) and v for v in passthrough)):
            raise ManifestError(f"server '{name}': env_passthrough must be a list of variable names")
        servers[name] = ServerSpec(
            name=server.name,
            transport=server.transport,
            command=server.command,
            url=server.url,
            auth_env=server.auth_env,
            env_passthrough=tuple(passthrough),
            call_timeout_seconds=_positive(spec, "call_timeout_seconds", 15.0, name),
            connect_timeout_seconds=_positive(spec, "connect_timeout_seconds", 30.0, name),
        )

    bindings: dict[str, Binding] = {}
    for entry in raw.get("bindings") or []:
        if not isinstance(entry, dict):
            raise ManifestError("each binding must be an object")
        family, adapter, server = entry.get("family"), entry.get("adapter"), entry.get("server")
        if family not in FAMILIES:
            raise ManifestError(f"binding family must be one of {sorted(FAMILIES)}, got '{family}'")
        if entry.get("effect") != "read":
            raise ManifestError(
                f"binding for '{family}' must declare \"effect\": \"read\": a tool with side effects cannot be bound to an agent tool"
            )
        if adapter not in FAMILIES[family]:
            raise ManifestError(f"binding for '{family}': adapter must be one of {sorted(FAMILIES[family])}")
        if server not in servers:
            raise ManifestError(f"binding for '{family}' uses unknown server '{server}'")
        if family in bindings:
            raise ManifestError(f"'{family}' is bound twice")
        tools = entry.get("tools")
        needed = ADAPTER_TOOLS[(family, adapter)]
        if not (isinstance(tools, dict) and needed <= set(tools) and all(isinstance(v, str) and v for v in tools.values())):
            raise ManifestError(f"binding for '{family}' ({adapter}) must name the server's tool for: {sorted(needed)}")
        promql: dict[tuple[str, str], tuple[str, str]] = {}
        for key, query in (entry.get("promql") or {}).items():
            service, _, metric = str(key).partition("/")
            if not (service and metric and isinstance(query, dict) and query.get("expr")):
                raise ManifestError(f"promql entry '{key}' must be 'service/metric': {{\"expr\": ..., \"unit\": ...}}")
            promql[(service, metric)] = (str(query["expr"]), str(query.get("unit", "")))
        bindings[family] = Binding(
            family=family,
            server=server,
            adapter=adapter,
            tools={k: v for k, v in tools.items() if k in needed},
            max_result_bytes=int(entry.get("max_result_bytes", 500_000)),
            promql=promql,
        )
    return Manifest(servers, bindings)


def _positive(spec: dict, key: str, default: float, server: str) -> float:
    value = spec.get(key, default)
    if not isinstance(value, (int, float)) or value <= 0:
        raise ManifestError(f"server '{server}': {key} must be a positive number")
    return float(value)
