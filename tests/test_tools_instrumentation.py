from datetime import datetime, timezone

import pytest

from app.audit import JsonlAuditLog
from app.config import ConfigError, load_settings, validate_settings
from app.tools.base import TimeWindow, ToolBudgetExceeded, ToolError, ToolNotPermitted, ToolUnavailableError
from app.tools.deploy_target import JenkinsDeployTarget, MockDeployTarget
from app.tools.deploys import MockDeployHistoryTool
from app.tools.instrumentation import AGENT_PERMISSIONS, ToolBudget, Toolkit
from app.tools.logs import MockLogsTool
from app.tools.memory import LexicalIncidentMemory
from app.tools.metrics import MockMetricsTool
from app.tools.sandbox import FileSandbox
from app.tools.scenarios import ANCHOR

WINDOW = TimeWindow.last(45, end=ANCHOR)


@pytest.fixture
def toolkit():
    return Toolkit(
        metrics=MockMetricsTool(),
        logs=MockLogsTool(),
        deploys=MockDeployHistoryTool(),
        memory=LexicalIncidentMemory(lambda: []),
        deploy_target=MockDeployTarget(polls_until_done=0),
        max_calls_per_run=5,
    )


@pytest.fixture
def audit(tmp_path):
    return JsonlAuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def sandbox(tmp_path):
    root = tmp_path / "repo"
    (root / "config").mkdir(parents=True)
    (root / "config" / "app.yaml").write_text("a: 1\n", encoding="utf-8")
    return FileSandbox(root, allowed_edit_paths=["config"])


def test_investigator_can_use_its_tools_and_every_call_is_audited(toolkit, audit):
    tools = toolkit.for_agent("investigator", entity_id="CHG-1", audit=audit)

    series = tools.metrics.query("orders-service", "error_rate_pct", WINDOW)
    logs = tools.logs.search("orders-service", "NullPointerException", WINDOW)
    deploys = tools.deploys.recent("orders-service", WINDOW)

    assert series.points and len(logs) == 3 and len(deploys) == 1
    trail = audit.list(entity_id="CHG-1")
    assert [e.action for e in trail] == [
        "tool.metrics.query",
        "tool.logs.search",
        "tool.deploys.recent",
    ]
    assert {e.actor for e in trail} == {"agent:investigator"}
    assert all(e.entity_type == "incident" for e in trail)
    first = trail[0].details
    assert first["ok"] is True
    assert first["args"][:2] == ["orders-service", "error_rate_pct"]
    assert len(first["result_hash"]) == 16 and first["result_size"] is None or first["result_size"] >= 0
    assert first["duration_ms"] >= 0
    assert trail[1].details["result_size"] == 3


def test_identical_calls_produce_identical_result_hashes(toolkit, audit):
    tools = toolkit.for_agent("investigator", entity_id="CHG-1", audit=audit)

    tools.logs.search("orders-service", "NullPointerException", WINDOW)
    tools.logs.search("orders-service", "NullPointerException", WINDOW)
    tools.logs.search("orders-service", "HTTP 500", WINDOW)

    hashes = [e.details["result_hash"] for e in audit.list(entity_id="CHG-1")]
    assert hashes[0] == hashes[1] != hashes[2]


def test_an_agent_cannot_reach_tools_it_was_not_given(toolkit, audit):
    tools = toolkit.for_agent("investigator", entity_id="CHG-1", audit=audit)

    assert tools.names == ["deploys", "logs", "memory", "metrics"]
    with pytest.raises(ToolNotPermitted, match="no access to the 'sandbox' tool"):
        tools.sandbox
    with pytest.raises(ToolNotPermitted, match="no access to the 'deploy_target' tool"):
        tools.deploy_target.deploy_dev("orders-service", "ref")


def test_an_agent_cannot_call_methods_outside_its_allow_list_and_the_attempt_is_recorded(toolkit, audit, sandbox):
    verifier = toolkit.for_agent("verifier", entity_id="CHG-2", audit=audit, sandbox=sandbox)

    with pytest.raises(ToolNotPermitted, match="agent:verifier may not call sandbox.edit"):
        verifier.sandbox.edit("config/app.yaml", "tampered")
    with pytest.raises(ToolNotPermitted, match="may not call deploy_target.canary"):
        verifier.deploy_target.canary("orders-service", "ref", 10)
    with pytest.raises(ToolNotPermitted, match="may not call deploy_target.rollback"):
        verifier.deploy_target.rollback("orders-service", "ref")

    assert sandbox.read("config/app.yaml") == "a: 1\n"
    denied = [e for e in audit.list(entity_id="CHG-2") if e.action.endswith(".denied")]
    assert [e.action for e in denied] == [
        "tool.sandbox.edit.denied",
        "tool.deploy_target.canary.denied",
        "tool.deploy_target.rollback.denied",
    ]


def test_nobody_can_trigger_canary_or_rollback(toolkit):
    for agent, grants in AGENT_PERMISSIONS.items():
        methods = grants.get("deploy_target", frozenset())
        assert not methods & {"canary", "rollback"}, agent


def test_only_the_fixer_can_edit_and_only_the_verifier_can_run_tests(toolkit, sandbox):
    editors = [a for a, g in AGENT_PERMISSIONS.items() if "edit" in g.get("sandbox", frozenset())]
    runners = [a for a, g in AGENT_PERMISSIONS.items() if "run_tests" in g.get("sandbox", frozenset())]
    assert editors == ["fixer"] and runners == ["verifier"]

    fixer = toolkit.for_agent("fixer", entity_id="CHG-3", sandbox=sandbox)
    assert fixer.sandbox.edit("config/app.yaml", "a: 2\n").changed is True
    with pytest.raises(ToolNotPermitted):
        fixer.sandbox.run_tests()

    critic = toolkit.for_agent("critic", entity_id="CHG-3", sandbox=sandbox)
    assert "a: 2" in critic.sandbox.diff()
    with pytest.raises(ToolNotPermitted):
        critic.sandbox.edit("config/app.yaml", "a: 3\n")


def test_the_tool_call_budget_stops_a_runaway_agent(toolkit, audit):
    tools = toolkit.for_agent("investigator", entity_id="CHG-4", audit=audit)

    for _ in range(5):
        tools.memory.search("anything")
    with pytest.raises(ToolBudgetExceeded, match="budget of 5"):
        tools.memory.search("one too many")

    trail = audit.list(entity_id="CHG-4")
    assert [e.action for e in trail].count("tool.memory.search") == 5
    assert trail[-1].action == "tool.memory.search.denied"
    assert "budget" in trail[-1].details["reason"]


def test_a_budget_can_be_shared_across_agents_in_one_run(toolkit):
    budget = ToolBudget(3)
    investigator = toolkit.for_agent("investigator", entity_id="CHG-5", budget=budget)
    critic = toolkit.for_agent("critic", entity_id="CHG-5", budget=budget)

    investigator.memory.search("a")
    investigator.memory.search("b")
    critic.memory.search("c")
    with pytest.raises(ToolBudgetExceeded):
        investigator.memory.search("d")


def test_failed_calls_are_audited_with_the_error_and_re_raised(toolkit, audit):
    tools = toolkit.for_agent("investigator", entity_id="CHG-6", audit=audit)

    with pytest.raises(ToolError, match="unknown metric"):
        tools.metrics.query("orders-service", "nope", WINDOW)

    entry = audit.list(entity_id="CHG-6")[0]
    assert entry.action == "tool.metrics.query"
    assert entry.details["ok"] is False
    assert "ToolError" in entry.details["error"] and "unknown metric" in entry.details["error"]
    assert "result_hash" not in entry.details


def test_long_arguments_are_truncated_in_the_audit_log(toolkit, audit):
    tools = toolkit.for_agent("investigator", entity_id="CHG-7", audit=audit)

    tools.memory.search("x" * 5000)

    assert len(audit.list(entity_id="CHG-7")[0].details["args"][0]) < 250


def test_proxies_do_not_expose_internals_or_attributes(toolkit):
    tools = toolkit.for_agent("investigator", entity_id="CHG-8")

    # The raw, unaudited tool must not be reachable from the proxy an agent holds.
    for hidden in ("_tool", "_context", "_allowed", "_name", "__dict__.tool"):
        with pytest.raises(AttributeError):
            getattr(tools.metrics, hidden)
    assert not any(isinstance(v, MockMetricsTool) for v in vars(tools.metrics).values())
    with pytest.raises(ToolNotPermitted):
        tools.metrics.scenarios  # not on the allow-list, so not reachable at all


def test_unknown_agents_get_nothing(toolkit):
    with pytest.raises(ToolNotPermitted, match="unknown agent"):
        toolkit.for_agent("intern", entity_id="CHG-9")


def test_tools_work_without_an_audit_log(toolkit):
    assert toolkit.for_agent("investigator", entity_id="CHG-10").memory.search("x") == []


# --- deploy targets ---------------------------------------------------------------------


def test_mock_deploy_target_progresses_to_the_configured_outcome():
    target = MockDeployTarget(outcome="failure", polls_until_done=1)

    started = target.deploy_dev("orders-service", "ref-1")
    assert started.status == "QUEUED" and started.url.startswith("mock://deploy/dev/orders-service/")
    assert target.get_status(started.url).status == "RUNNING"
    assert target.get_status(started.url).status == "FAILURE"
    assert target.canary("orders-service", "ref-1", 10).url != started.url


def test_jenkins_deploy_target_runs_dev_deploys_but_not_prod_operations():
    from app.adapters.jenkins_client import MockJenkinsClient

    target = JenkinsDeployTarget(MockJenkinsClient(polls_until_done=1))

    started = target.deploy_dev("orders-service", "ref-1", issue_type="application_error")
    assert started.status == "QUEUED" and "/job/orders-service-dev-validation/" in started.url
    assert target.get_status(started.url).status == "RUNNING"
    assert target.get_status(started.url).status == "SUCCESS"
    with pytest.raises(ToolUnavailableError):
        target.canary("orders-service", "ref-1", 10)
    with pytest.raises(ToolUnavailableError):
        target.rollback("orders-service", "ref-1")


# --- factory and config -----------------------------------------------------------------


def _build(monkeypatch, tmp_path, **env):
    from app.services.change_control import ChangeControlStore
    from app.adapters.jenkins_client import MockJenkinsClient
    from app.tools.factory import build_toolkit

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    store = ChangeControlStore(str(tmp_path / "changes.json"))
    return build_toolkit(load_settings(), store, lambda: [], MockJenkinsClient(), tmp_path)


def test_factory_defaults_to_mock_backends(monkeypatch, tmp_path):
    toolkit, provider, modes = _build(monkeypatch, tmp_path)

    assert modes == {
        "metrics": "mock",
        "logs": "mock",
        "deploys": "mock",
        "deploy_target": "mock",
        "memory": "lexical",
    }
    assert toolkit.max_calls_per_run == 40
    assert provider.repo_root == tmp_path.resolve()


def test_factory_builds_the_local_backends(monkeypatch, tmp_path):
    (tmp_path / "logs.jsonl").write_text("", encoding="utf-8")
    _, _, modes = _build(
        monkeypatch,
        tmp_path,
        METRICS_MODE="prometheus",
        METRICS_BASE_URL="http://127.0.0.1:9090",
        LOGS_MODE="file",
        LOGS_FILE_PATH="logs.jsonl",
        DEPLOY_HISTORY_MODE="changes",
        DEPLOY_TARGET_MODE="jenkins",
        TOOL_MAX_CALLS_PER_RUN="12",
    )

    assert modes["metrics"] == "prometheus" and modes["logs"] == "file"
    assert modes["deploys"] == "changes" and modes["deploy_target"] == "jenkins"


def test_a_prometheus_tool_with_no_url_fails_loudly_unless_fallback_is_allowed(monkeypatch, tmp_path):
    from app.services.integration_factory import IntegrationConfigError

    with pytest.raises(IntegrationConfigError, match="ALLOW_MOCK_FALLBACK"):
        _build(monkeypatch, tmp_path, METRICS_MODE="prometheus", METRICS_BASE_URL="")
    _, _, modes = _build(
        monkeypatch, tmp_path, METRICS_MODE="prometheus", METRICS_BASE_URL="", ALLOW_MOCK_FALLBACK="true"
    )
    assert modes["metrics"] == "mock-fallback-startup"


@pytest.mark.parametrize(
    "env,message",
    [
        ({"METRICS_MODE": "datadog"}, "METRICS_MODE"),
        ({"LOGS_MODE": "loki"}, "LOGS_MODE"),
        ({"DEPLOY_HISTORY_MODE": "git"}, "DEPLOY_HISTORY_MODE"),
        ({"DEPLOY_TARGET_MODE": "kubernetes"}, "DEPLOY_TARGET_MODE"),
        ({"LOGS_MODE": "file"}, "LOGS_FILE_PATH"),
    ],
)
def test_invalid_tool_configuration_is_rejected_at_startup(monkeypatch, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ConfigError, match=message):
        validate_settings(load_settings())


def test_the_app_starts_with_the_toolkit_and_reports_nothing_unexpected(monkeypatch):
    import importlib

    import app.main as main_module

    monkeypatch.setenv("VIEWER_API_KEY", "viewer-key")
    main_module = importlib.reload(main_module)

    assert main_module.tool_modes["metrics"] == "mock"
    assert main_module.toolkit.for_agent("investigator", entity_id="CHG-X").names == [
        "deploys",
        "logs",
        "memory",
        "metrics",
    ]
