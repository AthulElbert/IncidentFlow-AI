"""One declaration per tool; evidence follow-ups chosen from missing signals; re-diagnosing when a fix misses."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from agent_harness import CONFIG_PATH, FIXED, INVALID, build, done, edit
from llm_fakes import ScriptedLLM

from app.agents.collector import collect_evidence
from app.agents.followups import plan_follow_ups
from app.agents.investigator import Investigator
from app.agents.models import Hypothesis, IncidentContext
from app.agents.prompts import CRITIC_SYSTEM_PROMPT, FIXER_SYSTEM_PROMPT, INVESTIGATOR_SYSTEM_PROMPT
from app.agents.scoring import score_all
from app.config import ConfigError, load_settings, validate_settings
from app.replay import event_for, run_replay, toolkit_for
from app.tools.instrumentation import AGENT_PERMISSIONS, ToolBudget
from app.tools.registry import REGISTRY, ToolRequestError, model_tools, permissions, validate_request
from app.tools.scenarios import (
    ALL_SCENARIOS,
    BAD_CONFIG_LATENCY,
    DEFAULT_SCENARIOS,
    FOLLOW_UP_SCENARIOS,
    NOISY_DEPENDENCY_TIMEOUTS,
    UNEXPLAINED_ALERT,
    eval_history,
)

ROOT = Path(__file__).resolve().parents[1]

# The table that was written by hand before the registry existed. The registry must grant exactly this.
HAND_WRITTEN_PERMISSIONS = {
    "investigator": {
        "metrics": frozenset({"available_metrics", "query"}),
        "logs": frozenset({"search"}),
        "deploys": frozenset({"recent"}),
        "memory": frozenset({"search"}),
    },
    "fixer": {"sandbox": frozenset({"list_files", "read", "grep", "edit", "diff"}), "memory": frozenset({"search"})},
    "verifier": {
        "sandbox": frozenset({"list_files", "read", "grep", "diff", "run_tests"}),
        "deploy_target": frozenset({"deploy_dev", "get_status"}),
        "metrics": frozenset({"available_metrics", "query"}),
        "dev_metrics": frozenset({"available_metrics", "query"}),
    },
    "critic": {
        "sandbox": frozenset({"list_files", "read", "grep", "diff"}),
        "deploys": frozenset({"recent"}),
        "memory": frozenset({"search"}),
    },
}


# --- the registry ------------------------------------------------------------------------------


def test_the_registry_grants_exactly_what_the_hand_written_table_granted():
    assert permissions() == HAND_WRITTEN_PERMISSIONS
    assert AGENT_PERMISSIONS == HAND_WRITTEN_PERMISSIONS


def test_no_model_can_ask_to_deploy_test_or_change_anything_outside_its_worktree():
    for spec in REGISTRY:
        if spec.model_agents and spec.effect == "write":
            assert spec.id == "sandbox.edit" and spec.model_agents == {"fixer"}
    requestable = {spec.id for spec in REGISTRY if spec.model_agents}
    assert not requestable & {"sandbox.run_tests", "deploy_target.deploy_dev", "deploy_target.get_status"}
    assert not any(word in spec.id for spec in REGISTRY for word in ("canary", "rollback", "promote", "approve"))


def test_each_prompt_offers_exactly_the_tools_that_agents_model_may_request():
    def offered(prompt):
        return {f"{r['tool']}.{r['method']}" for r in (json.loads(line) for line in prompt.splitlines() if line.strip().startswith('{"action": "tool"'))}

    assert offered(INVESTIGATOR_SYSTEM_PROMPT) == {s.id for s in model_tools("investigator")} == {
        "metrics.query", "logs.search", "deploys.recent", "memory.search",
    }
    assert offered(FIXER_SYSTEM_PROMPT) == {s.id for s in model_tools("fixer")} == {
        "sandbox.list_files", "sandbox.read", "sandbox.grep", "sandbox.edit", "sandbox.diff", "memory.search",
    }
    assert offered(CRITIC_SYSTEM_PROMPT) == set() and model_tools("critic") == []


@pytest.mark.parametrize(
    "agent,request_,message",
    [
        ("investigator", {"tool": "sandbox", "method": "edit", "args": {"path": "x", "content": "y"}}, "unsupported request sandbox.edit"),
        ("fixer", {"tool": "sandbox", "method": "run_tests"}, "unsupported request sandbox.run_tests"),
        ("fixer", {"tool": "deploy_target", "method": "deploy_dev"}, "unsupported request deploy_target.deploy_dev"),
        ("investigator", {"tool": "metrics", "method": "query", "args": {}}, "metrics.query needs 'metric'"),
        ("investigator", {"tool": "metrics", "method": "query", "args": {"metric": 42}}, "'metric' must be a string"),
        ("investigator", {"tool": "logs", "method": "search", "args": {"minutes": "soon"}}, "'minutes' must be a number"),
        ("fixer", {"tool": "sandbox", "method": "read", "args": {"path": "x" * 300}}, "longer than 200"),
    ],
)
def test_a_bad_request_is_refused_with_a_reason_the_model_can_act_on(agent, request_, message):
    with pytest.raises(ToolRequestError, match=message):
        validate_request(agent, {"action": "tool", **request_})


def test_a_refusal_lists_what_the_agent_may_use_instead():
    with pytest.raises(ToolRequestError) as refused:
        validate_request("investigator", {"tool": "sandbox", "method": "edit"})

    assert "you may use: metrics.query, logs.search, deploys.recent, memory.search" in str(refused.value)


def test_arguments_are_kept_in_range_coerced_and_stripped_of_extras():
    spec, args = validate_request(
        "investigator",
        {"tool": "logs", "method": "search", "args": {"query": "timeout", "minutes": "999", "evil": "rm -rf /"}},
    )
    _, flattened = validate_request("fixer", {"tool": "sandbox", "method": "grep", "pattern": "x", "subdir": "src"})

    assert spec.id == "logs.search" and args == {"query": "timeout", "minutes": 240}
    assert flattened == {"pattern": "x", "subdir": "src"}


def test_a_bad_model_request_costs_a_step_but_never_reaches_a_tool(tmp_path):
    from app.agents.fixer import Fixer
    from app.tools.sandbox import FileSandbox
    from app.tools.instrumentation import Toolkit
    from app.tools.deploys import MockDeployHistoryTool
    from app.tools.logs import MockLogsTool
    from app.tools.memory import LexicalIncidentMemory
    from app.tools.metrics import MockMetricsTool
    from app.tools.dev_environment import MockDevEnvironment

    calls = []

    class Spy(FileSandbox):
        def read(self, path):
            calls.append(path)
            return super().read(path)

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")
    env = MockDevEnvironment()
    toolkit = Toolkit(MockMetricsTool(), MockLogsTool(), MockDeployHistoryTool(), LexicalIncidentMemory(lambda: []), env.deploy_target, env.metrics)
    tools = toolkit.for_agent("fixer", entity_id="X", sandbox=Spy(tmp_path, ["src"]))
    llm = ScriptedLLM(
        {"action": "tool", "tool": "sandbox", "method": "read", "args": {"path": 7}},
        {"action": "tool", "tool": "sandbox", "method": "read", "args": {"path": "src/a.py"}},
        {"action": "final", "summary": "n", "rationale": "n", "no_fix": True},
    )
    ctx = IncidentContext.from_event("X", event_for(BAD_CONFIG_LATENCY))
    hyp = Hypothesis(rank=1, issue_type="performance_degradation", root_cause="r", confidence=0.9)

    Fixer(llm).propose(ctx, hyp, [], tools, branch="b")

    assert calls == ["src/a.py"]
    assert "'path' must be a string" in json.loads(llm.calls[1][-1]["content"])["observation"]["error"]


# --- follow-up evidence --------------------------------------------------------------------------


def _investigate(scenario, follow_ups=0, budget=None, llm=None):
    toolkit = toolkit_for(ALL_SCENARIOS, eval_history())
    ctx = IncidentContext.from_event(f"T-{scenario.name}", event_for(scenario))
    tools = toolkit.for_agent("investigator", entity_id=ctx.incident_id, budget=budget)
    return Investigator(llm=llm, max_follow_ups=follow_ups).investigate(ctx, tools), tools


def test_the_fixed_plan_alone_hands_the_noisy_incident_to_a_person():
    report, _ = _investigate(NOISY_DEPENDENCY_TIMEOUTS)

    assert report.decision == "escalate" and report.top.issue_type == "performance_degradation" and report.follow_ups == []


def test_a_follow_up_finds_the_metric_the_plan_could_not_see_and_the_answer_becomes_right():
    report, _ = _investigate(NOISY_DEPENDENCY_TIMEOUTS, follow_ups=3)

    assert report.decision == "proceed" and report.top.issue_type == "dependency_failure"
    assert len(report.follow_ups) == 1 and report.follow_ups[0].startswith("metrics.query(metric='write_pool_wait_ms'")
    assert "1 new evidence item(s)" in report.follow_ups[0] and "dependency/timeout metric anomaly" in report.follow_ups[0]


def test_the_plan_puts_unqueried_metrics_first_and_never_follows_up_deploys():
    toolkit = toolkit_for(ALL_SCENARIOS, eval_history())
    ctx = IncidentContext.from_event("T", event_for(NOISY_DEPENDENCY_TIMEOUTS))
    ledger = collect_evidence(ctx, toolkit.for_agent("investigator", entity_id="T"))

    plans = plan_follow_ups(score_all(ledger.items), ledger, ctx, limit=10)

    assert (plans[0].tool, plans[0].args) == ("metrics", {"metric": "write_pool_wait_ms", "minutes": 60})
    assert [p.tool for p in plans] == sorted((p.tool for p in plans), key=["metrics", "logs", "memory"].index)
    assert not any(p.tool == "deploys" for p in plans)
    assert {"query": "timeout", "minutes": 180} in [p.args for p in plans if p.tool == "logs"]
    assert plan_follow_ups(score_all(ledger.items), ledger, ctx, limit=0) == []
    for plan in plans:
        validate_request("investigator", plan.request())  # every follow-up is a request the Investigator may make


def test_follow_ups_never_run_when_the_investigation_would_proceed_anyway():
    report, _ = _investigate(BAD_CONFIG_LATENCY, follow_ups=5)

    assert report.decision == "proceed" and report.follow_ups == []


def test_an_incident_with_no_cause_still_goes_to_a_person_after_its_follow_ups():
    report, _ = _investigate(UNEXPLAINED_ALERT, follow_ups=3)

    assert report.decision == "escalate" and report.top.issue_type == "unknown"
    assert len(report.follow_ups) == 3 and all("0 new evidence" in f for f in report.follow_ups)


def test_follow_ups_stop_when_the_tool_budget_is_spent():
    probe, probe_tools = _investigate(NOISY_DEPENDENCY_TIMEOUTS)  # how many calls the plan itself makes
    report, tools = _investigate(NOISY_DEPENDENCY_TIMEOUTS, follow_ups=3, budget=ToolBudget(probe_tools.calls_used))

    assert report.follow_ups == [] and report.decision == "escalate"
    assert any("follow-ups stopped" in w for w in report.warnings)


def test_a_model_is_offered_the_same_follow_ups_as_suggestions():
    llm = ScriptedLLM({"action": "final", "hypotheses": [{"issue_type": "unknown", "root_cause": "not sure", "supporting": []}]})

    _investigate(NOISY_DEPENDENCY_TIMEOUTS, llm=llm)

    payload = json.loads(llm.calls[0][1]["content"])
    suggested = payload["suggested_requests"]
    assert suggested[0]["request"] == {"action": "tool", "tool": "metrics", "method": "query", "args": {"metric": "write_pool_wait_ms", "minutes": 60}}
    assert all("would show" in s["why"] for s in suggested) and len(suggested) <= 3


def test_the_follow_up_scenarios_are_kept_apart_from_the_regression_set():
    assert NOISY_DEPENDENCY_TIMEOUTS not in DEFAULT_SCENARIOS and FOLLOW_UP_SCENARIOS == (NOISY_DEPENDENCY_TIMEOUTS,)
    off = run_replay(Investigator(), scenarios=ALL_SCENARIOS)
    on = run_replay(Investigator(max_follow_ups=3), scenarios=ALL_SCENARIOS)

    assert (off.harmful, off.unneeded_escalations) == (0, 1)
    assert (on.harmful, on.unneeded_escalations, on.accuracy) == (0, 0, 1.0)


def test_the_replay_script_can_show_follow_ups(tmp_path):
    out = tmp_path / "r.json"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "replay_investigations.py"), "--scenarios", "all", "--follow-ups", "3", "--output", str(out)],
        capture_output=True, text=True, env={**os.environ, "APP_SKIP_DOTENV": "true"}, timeout=120, cwd=str(tmp_path),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "follow-up: metrics.query(metric='write_pool_wait_ms'" in proc.stdout
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["scenarios"] == 8 and report["unneeded_escalations"] == 0


@pytest.mark.parametrize("value", ["-1", "6"])
def test_the_follow_up_setting_is_bounded(monkeypatch, value):
    monkeypatch.setenv("INVESTIGATION_FOLLOW_UPS", value)

    with pytest.raises(ConfigError, match="INVESTIGATION_FOLLOW_UPS"):
        validate_settings(load_settings())


# --- re-diagnosing when a fix does not move the alert metric -----------------------------------------


def _with_runner_up(h, issue_type, confidence):
    """Make the Investigator also offer a strong second diagnosis."""
    original = h.runtime.investigator.investigate

    def investigate(ctx, tools, llm_gate=None):
        report = original(ctx, tools, llm_gate=llm_gate)
        second = Hypothesis(rank=2, issue_type=issue_type, root_cause=f"the {issue_type} explanation", confidence=confidence)
        return report.model_copy(update={"hypotheses": [report.hypotheses[0], second]})

    h.runtime.investigator.investigate = investigate


def _second_attempt_resolves(h):
    """The dev environment shows no improvement for the first fix and recovery for the second."""
    h.env.default_effect = "no_effect"
    original = h.runtime.fixer.propose

    def propose(*args, **kwargs):
        if kwargs.get("attempt", 1) >= 2:
            h.env.default_effect = "resolves"
        return original(*args, **kwargs)

    h.runtime.fixer.propose = propose


def _task(h, call):
    return json.loads(h.llm.calls[call][1]["content"])


def test_when_the_fix_does_not_move_the_metric_the_next_strong_diagnosis_is_tried(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done("first try"), edit(CONFIG_PATH, FIXED), done("second try")])
    _with_runner_up(h, "dependency_failure", 0.70)
    _second_attempt_resolves(h)

    record = h.run()

    assert record.agent_run_status == "ready_for_approval"
    assert [a["diagnosis"] for a in record.agent_run["attempts"]] == ["performance_degradation", "dependency_failure"]
    assert (record.issue_type, record.confidence) == ("dependency_failure", 0.70)  # the change now says what was fixed
    second_task = _task(h, 2)
    assert second_task["diagnosis"]["issue_type"] == "dependency_failure"
    assert "diagnosis 'performance_degradation' was probably wrong" in second_task["feedback"]["failed_checks"]
    revised = [e for e in h.audit.list(entity_id=h.change_id, limit=500) if e.action == "agent.rediagnosed"]
    assert len(revised) == 1 and revised[0].details["note"].startswith("performance_degradation -> dependency_failure (0.70)")


def test_a_runner_up_too_weak_to_act_on_is_not_tried(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done(), edit(CONFIG_PATH, FIXED), done()])
    _with_runner_up(h, "dependency_failure", 0.45)
    _second_attempt_resolves(h)

    record = h.run()

    assert [a["diagnosis"] for a in record.agent_run["attempts"]] == ["performance_degradation", "performance_degradation"]
    assert record.issue_type == "performance_degradation"
    assert not any(e.action == "agent.rediagnosed" for e in h.audit.list(entity_id=h.change_id, limit=500))


def test_a_fix_that_fails_its_own_checks_is_retried_on_the_same_diagnosis(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, INVALID), done("broken"), edit(CONFIG_PATH, FIXED), done("fixed")])
    _with_runner_up(h, "dependency_failure", 0.80)

    record = h.run()

    assert record.agent_run_status == "ready_for_approval"
    assert [a["diagnosis"] for a in record.agent_run["attempts"]] == ["performance_degradation", "performance_degradation"]
