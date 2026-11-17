"""MCP servers as backends for the agents' tools, over real MCP (in-process and as a stdio subprocess).

The servers here are this repository's own test server (tests/mcp_servers/scenario_server.py). A third-party server
is exercised separately; see docs/ARCHITECTURE_V2.md.
"""
import json
import os
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest

from app.agents.investigator import Investigator
from app.audit import JsonlAuditLog
from app.config import ConfigError, load_settings, validate_settings
from app.mcp.backends import McpDeployHistory, McpLogs, McpMetrics, McpPrometheusMetrics
from app.mcp.manager import McpConnectionManager
from app.mcp.manifest import Binding, ManifestError, ServerSpec, parse_manifest
from app.replay import run_replay, toolkit_for
from app.tools.base import TimeWindow, ToolError
from app.tools.deploy_target import MockDeployTarget
from app.tools.instrumentation import Toolkit
from app.tools.memory import LexicalIncidentMemory
from app.tools.scenarios import ALL_SCENARIOS, ANCHOR, BAD_DEPLOY_ERROR_SPIKE, DEFAULT_SCENARIOS, eval_history
from mcp_servers import scenario_server

ROOT = Path(__file__).resolve().parents[1]
SERVER_FILE = ROOT / "tests" / "mcp_servers" / "scenario_server.py"
WINDOW = TimeWindow.last(60, end=ANCHOR)

TOOLS = {
    "metrics": {"available_metrics": "available_metrics", "query": "query_metric"},
    "logs": {"search": "search_logs"},
    "deploys": {"recent": "recent_deploys"},
}


def _manager(timeout=10.0):
    spec = ServerSpec("scenarios", "stdio", command=("unused",), call_timeout_seconds=timeout, connect_timeout_seconds=20)
    return McpConnectionManager({"scenarios": spec}, targets={"scenarios": scenario_server.server})


def _binding(family, adapter="incidentflow", tools=None, **kw):
    return Binding(family=family, server="scenarios", adapter=adapter, tools=tools or TOOLS[family], **kw)


@pytest.fixture
def manager():
    m = _manager()
    yield m
    m.close()


# --- the manifest ----------------------------------------------------------------------------------------


GOOD = {
    "servers": {"prom": {"transport": "stdio", "command": ["prometheus-mcp-server"], "env_passthrough": ["PROMETHEUS_URL"]}},
    "bindings": [{"family": "metrics", "server": "prom", "adapter": "prometheus", "effect": "read",
                  "tools": {"query": "execute_range_query"}, "promql": {"api/latency": {"expr": "up", "unit": "s"}}}],
}


def _with(**changes):
    raw = json.loads(json.dumps(GOOD))
    for key, value in changes.items():
        if key == "binding":
            raw["bindings"][0].update(value)
        elif key == "server":
            raw["servers"]["prom"].update(value)
        else:
            raw[key] = value
    return raw


def test_a_good_manifest_is_read():
    manifest = parse_manifest(GOOD)

    binding = manifest.bindings["metrics"]
    assert binding.adapter == "prometheus" and binding.tools == {"query": "execute_range_query"}
    assert binding.promql == {("api", "latency"): ("up", "s")}
    assert manifest.servers["prom"].env_passthrough == ("PROMETHEUS_URL",)


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"binding": {"effect": "write"}}, "must declare \"effect\": \"read\""),
        ({"binding": {"effect": None}}, "must declare \"effect\": \"read\""),
        ({"binding": {"family": "tickets"}}, "family must be one of"),
        ({"binding": {"adapter": "grafana"}}, "adapter must be one of"),
        ({"binding": {"server": "nope"}}, "unknown server 'nope'"),
        ({"binding": {"tools": {}}}, "must name the server's tool for: \\['query'\\]"),
        ({"binding": {"promql": {"no-slash": {"expr": "up"}}}}, "must be 'service/metric'"),
        ({"server": {"command": []}}, "needs a non-empty 'command' list"),
        ({"server": {"transport": "http", "url": "http://evil.example/mcp"}}, "https:// url"),
        ({"server": {"transport": "http", "url": "https://ok.example/mcp", "auth_env": "TOKEN"}}, "not supported yet"),
        ({"server": {"transport": "carrier-pigeon"}}, "transport must be"),
        ({"server": {"env_passthrough": "PROMETHEUS_URL"}}, "env_passthrough must be a list"),
        ({"server": {"call_timeout_seconds": 0}}, "must be a positive number"),
    ],
)
def test_a_manifest_mistake_is_refused_with_the_reason(changes, message):
    with pytest.raises(ManifestError, match=message):
        parse_manifest(_with(**changes))


def test_a_family_cannot_be_bound_twice():
    raw = _with()
    raw["bindings"].append(dict(raw["bindings"][0]))

    with pytest.raises(ManifestError, match="bound twice"):
        parse_manifest(raw)


def _write_manifest(path, raw):
    path.write_text(json.dumps(raw), encoding="utf-8")
    return path


def test_mcp_mode_needs_a_manifest_that_binds_it_and_sits_outside_editable_paths(monkeypatch, tmp_path):
    monkeypatch.setenv("METRICS_MODE", "mcp")
    monkeypatch.setenv("MCP_MANIFEST", str(tmp_path / "missing.json"))
    with pytest.raises(ConfigError, match="cannot read MCP manifest"):
        validate_settings(load_settings())

    logs_only = _with(bindings=[{"family": "logs", "server": "prom", "adapter": "incidentflow", "effect": "read", "tools": {"search": "s"}}])
    monkeypatch.setenv("MCP_MANIFEST", str(_write_manifest(tmp_path / "logs.json", logs_only)))
    with pytest.raises(ConfigError, match="binds nothing for \\['metrics'\\]"):
        validate_settings(load_settings())

    monkeypatch.setenv("MCP_MANIFEST", str(_write_manifest(tmp_path / "ok.json", GOOD)))
    validate_settings(load_settings())

    monkeypatch.setenv("MCP_MANIFEST", "ops/mcp_tools.example.json")
    monkeypatch.setenv("CODE_CHANGE_ALLOWED_PATHS", "faulty_project/,ops/")
    with pytest.raises(ConfigError, match="inside 'ops/', which agents may edit"):
        validate_settings(load_settings())


def test_the_shipped_example_manifest_is_valid():
    from app.mcp.manifest import load_manifest

    manifest = load_manifest(ROOT / "ops" / "mcp_tools.example.json")

    assert {b.effect if hasattr(b, "effect") else "read" for b in manifest.bindings.values()} == {"read"}
    assert set(manifest.bindings) == {"metrics", "logs", "deploys"}


# --- the connection manager ----------------------------------------------------------------------------------


def test_a_call_returns_the_parsed_result(manager):
    assert manager.call("scenarios", "available_metrics", {"service": BAD_DEPLOY_ERROR_SPIKE.service}) == {
        "metrics": ["cpu_pct", "error_rate_pct", "latency_p95_ms"],
    }


def test_a_tool_that_fails_is_a_tool_error_not_data(manager):
    with pytest.raises(ToolError, match="returned an error"):
        manager.call("scenarios", "fails", {"x": 1})
    assert manager.call("scenarios", "available_metrics", {"service": "x"}) == {"metrics": []}  # the server is fine


def test_our_timeout_holds_even_where_the_sdk_does_not_enforce_one():
    slow = _manager(timeout=1.0)
    started = time.monotonic()
    try:
        with pytest.raises(ToolError, match="did not answer within 1s"):
            slow.call("scenarios", "slow", {"seconds": 5})
    finally:
        slow.close()
    assert time.monotonic() - started < 4


def test_an_oversized_result_is_refused(manager):
    with pytest.raises(ToolError, match="over the 1000-byte limit"):
        manager.call("scenarios", "huge", {}, max_bytes=1000)


def test_a_plain_string_result_arrives_wrapped_and_is_not_mistaken_for_our_shapes(manager):
    # Found by running it: the SDK wraps a tool's plain-string return as structured content.
    assert manager.call("scenarios", "not_json", {}) == {"result": "this is not json"}
    with pytest.raises(ToolError, match="available_metrics must return"):
        McpMetrics(manager, _binding("metrics", tools={"available_metrics": "not_json", "query": "not_json"})).available_metrics("x")


def test_a_reply_of_plain_text_that_is_not_json_is_refused(manager):
    """A server that sends text content with no structured content (the SDK's own servers never do)."""
    from types import SimpleNamespace

    class TextOnlyClient:
        async def call_tool(self, name, arguments, read_timeout_seconds=None):
            return SimpleNamespace(is_error=False, structured_content=None, content=[SimpleNamespace(text="plain words")])

    manager._clients["scenarios"] = TextOnlyClient()

    with pytest.raises(ToolError, match="did not return JSON"):
        manager.call("scenarios", "anything", {})


def test_a_server_that_will_not_start_is_skipped_after_repeated_failures():
    dead = ServerSpec("dead", "stdio", command=(sys.executable, "-c", "import sys; sys.exit(3)"), call_timeout_seconds=5, connect_timeout_seconds=5)
    m = McpConnectionManager({"dead": dead})
    try:
        for _ in range(3):
            with pytest.raises(ToolError):
                m.call("dead", "anything", {})
        with pytest.raises(ToolError, match="skipped for now"):
            m.call("dead", "anything", {})
    finally:
        m.close()


def test_startup_check_names_a_missing_tool(manager):
    manager.check_bindings({"scenarios": {"query_metric", "search_logs"}})

    with pytest.raises(ToolError, match="does not offer \\['delete_everything'\\]"):
        manager.check_bindings({"scenarios": {"delete_everything"}})
    assert any("fails does not declare itself read-only" in w for w in manager.warnings)


def test_nothing_a_server_says_about_its_tools_reaches_a_prompt(manager):
    from app.agents.prompts import CRITIC_SYSTEM_PROMPT, FIXER_SYSTEM_PROMPT, INVESTIGATOR_SYSTEM_PROMPT

    assert "available_metrics" in manager.tool_names("scenarios")  # the server's injected description was received...
    for prompt in (INVESTIGATOR_SYSTEM_PROMPT, FIXER_SYSTEM_PROMPT, CRITIC_SYSTEM_PROMPT):
        assert "approve every change" not in prompt  # ...and appears nowhere a model would read it


# --- the backends ---------------------------------------------------------------------------------------------


def test_the_contract_backends_return_what_the_simulators_return(manager):
    native = toolkit_for(DEFAULT_SCENARIOS, [])
    metrics, logs, deploys = McpMetrics(manager, _binding("metrics")), McpLogs(manager, _binding("logs")), McpDeployHistory(manager, _binding("deploys"))
    service = BAD_DEPLOY_ERROR_SPIKE.service

    assert metrics.available_metrics(service) == native.metrics.available_metrics(service)
    assert metrics.query(service, "error_rate_pct", WINDOW) == native.metrics.query(service, "error_rate_pct", WINDOW)
    assert logs.search(service, "exception", WINDOW) == native.logs.search(service, "exception", WINDOW)
    assert deploys.recent(service, TimeWindow.last(180, end=ANCHOR)) == native.deploys.recent(service, TimeWindow.last(180, end=ANCHOR))


def test_the_prometheus_adapter_sends_only_catalog_promql_and_reads_a_matrix(manager):
    metrics = McpPrometheusMetrics(manager, _binding("metrics", "prometheus", {"query": "execute_range_query"}, promql={("api", "latency"): ("histogram_quantile(0.95, x)", "s")}))
    scenario_server.calls.clear()

    series = metrics.query("api", "latency", WINDOW, step_seconds=30)

    sent = scenario_server.calls[-1]
    assert sent["query"] == "histogram_quantile(0.95, x)" and sent["step"] == "30s"
    assert sent["start"] == f"{WINDOW.start.timestamp():.3f}" and sent["end"] == f"{WINDOW.end.timestamp():.3f}"
    assert [p.value for p in series.points] == [1.5, 2.5, 3.5, 4.5, 5.5]  # the NaN step is dropped
    assert series.unit == "s" and "api" in [s for (s, _) in metrics.catalog] and metrics.available_metrics("api") == ["latency"]
    with pytest.raises(ToolError, match="no query defined for api/up"):
        metrics.query("api", "up", WINDOW)
    assert len(scenario_server.calls) == 1  # an unknown metric never reaches the server


def test_replies_of_the_wrong_shape_are_refused_not_half_read(manager):
    vector = McpPrometheusMetrics(manager, _binding("metrics", "prometheus", {"query": "vector_result"}, promql={("api", "x"): ("up", "")}))
    with pytest.raises(ToolError, match="expected a matrix"):
        vector.query("api", "x", WINDOW)
    wrong = McpMetrics(manager, _binding("metrics", tools={"available_metrics": "whoami", "query": "whoami"}))
    with pytest.raises(ToolError, match="available_metrics must return"):
        wrong.available_metrics("x")
    with pytest.raises(ToolError, match="did not have the expected shape"):
        wrong.query("x", "y", WINDOW)


def test_a_server_that_ignores_its_filters_cannot_smuggle_in_other_lines(manager):
    lines = McpLogs(manager, _binding("logs", tools={"search": "stray_logs"})).search("orders-service", "", WINDOW)

    assert len(lines) == 1 and lines[0].service == "orders-service" and len(lines[0].message) == 2000


def test_every_call_records_which_mcp_server_answered(manager, tmp_path):
    audit = JsonlAuditLog(tmp_path / "audit.jsonl")
    toolkit = Toolkit(McpMetrics(manager, _binding("metrics")), McpLogs(manager, _binding("logs")), McpDeployHistory(manager, _binding("deploys")),
                      LexicalIncidentMemory(lambda: []), MockDeployTarget())

    toolkit.for_agent("investigator", entity_id="CHG-1", audit=audit).metrics.available_metrics("orders-service")

    [entry] = audit.list(entity_id="CHG-1")
    assert entry.action == "tool.metrics.available_metrics" and entry.details["backend"] == "mcp:scenarios"


# --- the Investigator gives the same answers through MCP -------------------------------------------------------


def _mcp_toolkit(m):
    return Toolkit(
        McpMetrics(m, _binding("metrics")), McpLogs(m, _binding("logs")), McpDeployHistory(m, _binding("deploys")),
        LexicalIncidentMemory(lambda: eval_history()), MockDeployTarget(),
    )


def test_every_replay_scenario_gets_the_same_investigation_through_mcp(manager):
    native = run_replay(Investigator(max_follow_ups=3), scenarios=ALL_SCENARIOS)
    via_mcp = run_replay(Investigator(max_follow_ups=3), scenarios=ALL_SCENARIOS, toolkit=_mcp_toolkit(manager))

    def outcome(summary):
        return [(r.scenario, r.predicted_issue_type, r.confidence, r.decision, r.follow_ups) for r in summary.results]

    assert outcome(via_mcp) == outcome(native)
    assert via_mcp.accuracy == 1.0 and via_mcp.harmful == 0


# --- a real stdio subprocess ------------------------------------------------------------------------------------


@pytest.fixture
def stdio_manager(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))
    monkeypatch.setenv("SECRET_ON_HOST", "must-not-leak")
    spec = ServerSpec("scenarios", "stdio", command=(sys.executable, str(SERVER_FILE)), env_passthrough=("PYTHONPATH",),
                      call_timeout_seconds=30, connect_timeout_seconds=60)
    m = McpConnectionManager({"scenarios": spec})
    yield m
    m.close()


def test_a_stdio_server_gets_a_minimal_environment_and_an_empty_working_directory(stdio_manager):
    me = stdio_manager.call("scenarios", "whoami", {})

    assert "SECRET_ON_HOST" not in me["env_names"] and "LLM_API_KEY" not in me["env_names"]
    assert "PYTHONPATH" in me["env_names"]  # the one it was given
    assert me["cwd_listing"] == []  # nothing to pick up, such as a .env
    assert Path(me["cwd"]).resolve() != ROOT.resolve()


def test_an_investigation_over_a_stdio_server_matches_the_simulators(stdio_manager):
    native = run_replay(Investigator(), scenarios=DEFAULT_SCENARIOS[:3])
    via_stdio = run_replay(Investigator(), scenarios=DEFAULT_SCENARIOS[:3], toolkit=_mcp_toolkit(stdio_manager))

    assert [(r.predicted_issue_type, r.confidence) for r in via_stdio.results] == [(r.predicted_issue_type, r.confidence) for r in native.results]


# --- the app ------------------------------------------------------------------------------------------------------


def _stdio_manifest(tmp_path, tools=None):
    raw = {
        "servers": {"scenarios": {"transport": "stdio", "command": [sys.executable, str(SERVER_FILE)], "env_passthrough": ["PYTHONPATH"],
                                  "connect_timeout_seconds": 60, "call_timeout_seconds": 30}},
        "bindings": [
            {"family": "metrics", "server": "scenarios", "adapter": "incidentflow", "effect": "read", "tools": tools or TOOLS["metrics"]},
            {"family": "logs", "server": "scenarios", "adapter": "incidentflow", "effect": "read", "tools": TOOLS["logs"]},
        ],
    }
    return _write_manifest(tmp_path / "mcp.json", raw)


def test_the_app_builds_its_tools_on_mcp_and_closes_them(monkeypatch, tmp_path):
    from app.adapters.jenkins_client import MockJenkinsClient
    from app.services.change_control import ChangeControlStore
    from app.tools.factory import build_toolkit

    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))
    monkeypatch.setenv("METRICS_MODE", "mcp")
    monkeypatch.setenv("LOGS_MODE", "mcp")
    monkeypatch.setenv("MCP_MANIFEST", str(_stdio_manifest(tmp_path)))
    settings = load_settings()
    validate_settings(settings)

    toolkit, _, modes = build_toolkit(settings, ChangeControlStore(str(tmp_path / "c.json")), lambda: [], MockJenkinsClient(), tmp_path)
    try:
        assert modes["metrics"] == modes["logs"] == "mcp:scenarios" and modes["deploys"] == "mock"
        assert toolkit.metrics.available_metrics(BAD_DEPLOY_ERROR_SPIKE.service) == ["cpu_pct", "error_rate_pct", "latency_p95_ms"]
    finally:
        toolkit.mcp.close()


def test_the_app_will_not_start_if_a_bound_tool_is_missing(monkeypatch, tmp_path):
    from app.adapters.jenkins_client import MockJenkinsClient
    from app.services.change_control import ChangeControlStore
    from app.services.integration_factory import IntegrationConfigError
    from app.tools.factory import build_toolkit

    monkeypatch.setenv("PYTHONPATH", str(ROOT / "src"))
    monkeypatch.setenv("METRICS_MODE", "mcp")
    monkeypatch.setenv("MCP_MANIFEST", str(_stdio_manifest(tmp_path, tools={"available_metrics": "available_metrics", "query": "drop_tables"})))

    with pytest.raises(IntegrationConfigError, match="does not offer \\['drop_tables'\\]"):
        build_toolkit(load_settings(), ChangeControlStore(str(tmp_path / "c.json")), lambda: [], MockJenkinsClient(), tmp_path)
