"""A run that a restart interrupts is resumed from its last completed stage, safely and not for ever.

"The process died" is simulated by raising a BaseException inside a stage: nothing in the run catches it, so the
change is left `running` exactly as a killed process would leave it. "The restart" is a new runtime and a new
connection to the same checkpoint file and the same change store.
"""
import dataclasses
import time

import pytest
from agent_harness import CONFIG_PATH, FIXED, INVALID, VIEWER, build, done, edit, scenario_incident, wait_for_run
from llm_fakes import ScriptedLLM

from app.agents.budget import BudgetLimits, RunBudget
from app.agents.checkpoint import build_checkpointing
from app.agents.fixer import Fixer
from app.agents.runner import AgentRunner
from app.config import ConfigError, load_settings, validate_settings
from app.replay import event_for
from app.tools.scenarios import BAD_CONFIG_LATENCY

GOOD_FIX = [edit(CONFIG_PATH, FIXED), done("Lowered latency_ms")]


class Killed(BaseException):
    """The process died here."""


class Dying:
    """Wraps a component so that one of its methods kills the process."""

    def __init__(self, real, method):
        self._real, self._method = real, method

    def __getattr__(self, name):
        if name == self._method:
            def die(*args, **kwargs):
                raise Killed(name)

            return die
        return getattr(self._real, name)


def _fixer(replies):
    return Fixer(ScriptedLLM(*replies), editable_paths=["faulty_project", "config", "src"], max_changed_lines=50)


def _process(h, tmp_path, fixer_replies=(), max_resumes=2, **replaced):
    """A fresh process over the same change store, repo and checkpoint file."""
    runtime = dataclasses.replace(
        h.runtime, _sandboxes={}, _budgets={}, _resumed=set(), fixer=_fixer(fixer_replies), **replaced
    )
    checkpointing = build_checkpointing("sqlite", tmp_path / "checkpoints.sqlite")
    return AgentRunner(runtime, workers=1, checkpointer=checkpointing.saver, max_resumes=max_resumes)


def _crash(h, tmp_path, fixer_replies=(), **dying):
    """Run until the named component kills the process. Leaves the change `running`."""
    runner = _process(h, tmp_path, fixer_replies, **dying)
    with pytest.raises(Killed):
        runner.run_sync(h.change_id, "tester")
    assert h.record().agent_run_status == "running"
    return runner


def _actions(h):
    return [e.action for e in h.audit.list(entity_id=h.change_id, limit=500) if not e.action.startswith(("tool.", "llm."))]


# --- resume from each stage -------------------------------------------------------------------------


def test_a_run_killed_while_investigating_is_resumed_and_finishes(tmp_path):
    h = build(tmp_path)
    _crash(h, tmp_path, investigator=Dying(h.runtime.investigator, "investigate"))

    record = _process(h, tmp_path, GOOD_FIX).resume_sync(h.change_id)

    assert (record.agent_run_status, record.deployment_state) == ("ready_for_approval", "ready_for_prod")
    assert record.agent_run["resumes"] == 1 and [a["attempt"] for a in record.agent_run["attempts"]] == [1]
    assert "agent.resumed" in _actions(h)


def test_a_run_killed_in_the_middle_of_a_fix_starts_that_fix_again_from_clean_code(tmp_path):
    h = build(tmp_path)
    half_done = _fixer([edit("config/other.yaml", "pool_size: 99\n")])  # one edit, then the process dies
    original = half_done.llm.complete_json
    half_done.llm.complete_json = lambda messages, error_label="LLM": (
        original(messages, error_label) if half_done.llm.replies else (_ for _ in ()).throw(Killed("fix"))
    )
    runner = _process(h, tmp_path)
    runner.runtime.fixer = half_done
    with pytest.raises(Killed):
        runner.run_sync(h.change_id, "tester")
    assert "pool_size: 99" in (h.root / "config/other.yaml").read_text(encoding="utf-8")  # the half-finished edit is on disk

    record = _process(h, tmp_path, GOOD_FIX).resume_sync(h.change_id)

    assert record.agent_run_status == "ready_for_approval"
    assert record.agent_run["changed_files"] == [CONFIG_PATH]  # the abandoned edit is not part of the fix
    assert "other.yaml" not in record.agent_run["diff"]["text"]
    assert (h.root / "config/other.yaml").read_text(encoding="utf-8") == "pool_size: 10\n"


@pytest.mark.parametrize(
    "component,method",
    [("verifier", "verify"), ("critic", "review")],
)
def test_a_run_killed_after_the_fix_redoes_the_attempt_instead_of_trusting_the_old_worktree(tmp_path, component, method):
    h = build(tmp_path)
    # Before the crash the Fixer made a change that would NOT pass (invalid JSON) plus an unrelated edit.
    bad = [edit(CONFIG_PATH, INVALID), edit("config/other.yaml", "pool_size: 99\n"), done("first try")]
    if component == "critic":
        bad = GOOD_FIX  # the Critic is only reached by a fix that verifies
    _crash(h, tmp_path, bad, **{component: Dying(getattr(h.runtime, component), method)})

    restarted = _process(h, tmp_path, GOOD_FIX)
    record = restarted.resume_sync(h.change_id)

    assert record.agent_run_status == "ready_for_approval"
    assert len(restarted.runtime.fixer.llm.calls) == 2  # the fix was made again in this process
    assert [a["attempt"] for a in record.agent_run["attempts"]] == [1]  # the same attempt, recorded once
    assert record.agent_run["changed_files"] == [CONFIG_PATH] and "other.yaml" not in record.agent_run["diff"]["text"]
    assert record.agent_run["verification"]["verdict"] == "passed"


def test_a_run_killed_while_packaging_reverifies_before_it_offers_a_diff(tmp_path):
    h = build(tmp_path)
    _crash(h, tmp_path, GOOD_FIX, change_store=Dying(h.store, "finish_agent_run"))
    assert h.record().deployment_state == "reviewing"

    restarted = _process(h, tmp_path, GOOD_FIX)
    record = restarted.resume_sync(h.change_id)

    assert record.agent_run_status == "ready_for_approval" and record.dev_execution_status == "passed"
    assert len(restarted.runtime.fixer.llm.calls) == 2
    assert CONFIG_PATH in record.agent_run["diff"]["text"] and '"latency_ms": 120' in record.agent_run["diff"]["text"]
    assert _actions(h).count("agent.ready_for_approval") == 1


# --- a restart cannot reset the budget, and cannot go on for ever ------------------------------------


def test_what_a_run_had_used_is_carried_over_the_restart(tmp_path):
    h = build(tmp_path)
    _crash(h, tmp_path, GOOD_FIX, verifier=Dying(h.runtime.verifier, "verify"))

    record = _process(h, tmp_path, GOOD_FIX).resume_sync(h.change_id)

    used = record.agent_run["budget"]
    assert record.agent_run_status == "ready_for_approval"
    assert used["llm_calls"] == 4  # two before the crash, two for the redone fix
    assert used["tool_calls"] > 20  # the investigation's and first fix's calls are still counted


def test_a_limit_reached_across_a_restart_still_stops_the_run(tmp_path):
    h = build(tmp_path)
    h.runtime.budget_limits = BudgetLimits(max_llm_calls=3)
    _crash(h, tmp_path, GOOD_FIX, verifier=Dying(h.runtime.verifier, "verify"))  # two model calls used

    record = _process(h, tmp_path, GOOD_FIX).resume_sync(h.change_id)  # the redone fix needs two more

    assert record.agent_run_status == "escalated" and "the run used its 3 model calls" in record.agent_run["escalation_reason"]


def test_a_budget_snapshot_restores_time_calls_tokens_and_cost():
    clock = iter([100.0, 100.0, 130.0]).__next__
    restored = RunBudget(
        BudgetLimits(max_seconds=60), clock=clock,
        carried={"seconds": 45.0, "tool_calls": 7, "llm_calls": 3, "tokens": 900, "cost": 0.25},
    )

    assert restored.snapshot() == {"seconds": 45.0, "tool_calls": 7, "llm_calls": 3, "tokens": 900, "cost": 0.25}
    assert "60s time limit" in restored.exceeded()  # 45s before the restart + 30s after it


def test_a_run_is_resumed_only_so_many_times(tmp_path):
    h = build(tmp_path)
    _crash(h, tmp_path, investigator=Dying(h.runtime.investigator, "investigate"))

    again = _process(h, tmp_path, max_resumes=1, investigator=Dying(h.runtime.investigator, "investigate"))
    with pytest.raises(Killed):
        again.resume_sync(h.change_id)
    assert h.record().agent_run["resumes"] == 1

    third = _process(h, tmp_path, GOOD_FIX, max_resumes=1)
    with pytest.raises(ValueError, match="already resumed 1 time"):
        third.resume_sync(h.change_id)

    outcome = third.recover()
    record = h.record()
    assert outcome == {"resumed": [], "escalated": [h.change_id]}
    assert record.agent_run_status == "escalated"
    assert "was not resumed (it was already resumed 1 time(s), the limit)" in record.agent_run["escalation_reason"]


# --- recovery at startup ------------------------------------------------------------------------------


def _wait_until_finished(h, change_id, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        record = h.store.get_change(change_id)
        if record.agent_run_status != "running":
            return record
        time.sleep(0.05)
    raise AssertionError("the resumed run did not finish")


def test_startup_resumes_what_it_can_and_escalates_the_rest(tmp_path):
    h = build(tmp_path)
    _crash(h, tmp_path, investigator=Dying(h.runtime.investigator, "investigate"))
    event = event_for(BAD_CONFIG_LATENCY)
    def another(name):
        return h.store.create_change(
            incident_id=f"INC-{name}", service=event.service, environment="prod", summary="s", jira_key="K",
            jenkins_job_url="u", proposed_actions=[], triage_mode_used="heuristic", triage_hypothesis_steps=[],
            issue_type="unknown", confidence=0.5, warning_count=0, jenkins_status="QUEUED", alert=event.model_dump(mode="json"),
        )

    lost, shadowed = another("lost"), another("shadowed")
    h.store.begin_agent_run(lost.change_id, "tester")  # running, but nothing was ever saved for it
    h.store.begin_shadow_run(shadowed.change_id, "tester")  # a shadow run was in flight too

    outcome = _process(h, tmp_path, GOOD_FIX).recover()

    assert outcome["resumed"] == [h.change_id] and lost.change_id in outcome["escalated"]
    assert _wait_until_finished(h, h.change_id).agent_run_status == "ready_for_approval"
    abandoned = h.store.get_change(lost.change_id)
    assert abandoned.agent_run_status == "escalated" and "no saved progress" in abandoned.agent_run["escalation_reason"]
    closed = h.store.get_change(shadowed.change_id)
    assert closed.shadow["status"] == "escalated" and "restart" in closed.shadow["escalation_reason"]  # never resumed
    assert shadowed.change_id in outcome["escalated"]


def test_with_the_in_memory_checkpointer_a_restart_cannot_resume(tmp_path):
    h = build(tmp_path)
    with pytest.raises(Killed):
        AgentRunner(dataclasses.replace(h.runtime, investigator=Dying(h.runtime.investigator, "investigate"))).run_sync(h.change_id, "t")

    restarted = AgentRunner(dataclasses.replace(h.runtime, _sandboxes={}, _budgets={}, _resumed=set()))  # memory: nothing saved
    with pytest.raises(ValueError, match="no saved progress"):
        restarted.resume_sync(h.change_id)
    assert restarted.recover() == {"resumed": [], "escalated": [h.change_id]}
    assert "interrupted by a restart" in h.record().agent_run["escalation_reason"]


def test_a_change_that_is_not_running_cannot_be_resumed(tmp_path):
    h = build(tmp_path, GOOD_FIX)
    runner = _process(h, tmp_path, GOOD_FIX)

    with pytest.raises(ValueError, match="No agent run is in progress"):
        runner.resume_sync(h.change_id)
    with pytest.raises(ValueError, match="Change not found"):
        runner.resume_sync("CHG-NOPE")


# --- saved progress is kept only while it is needed ----------------------------------------------------


def test_a_finished_runs_saved_progress_is_deleted_and_an_interrupted_runs_is_kept(tmp_path):
    h = build(tmp_path)
    crashed = _crash(h, tmp_path, investigator=Dying(h.runtime.investigator, "investigate"))
    config = crashed._config(h.record())
    assert crashed.graph.get_state(config).next == ("investigate",)

    finisher = _process(h, tmp_path, GOOD_FIX)
    finisher.resume_sync(h.change_id)

    after = finisher.graph.get_state(config)
    assert after.next == () and dict(after.values) == {}


# --- settings and the builder -----------------------------------------------------------------------------


def test_the_checkpoint_builder_makes_durable_and_non_durable_stores(tmp_path):
    memory = build_checkpointing("memory")
    sqlite = build_checkpointing("sqlite", tmp_path / "nested" / "dir" / "cp.sqlite")

    assert not memory.durable and sqlite.durable and (tmp_path / "nested" / "dir" / "cp.sqlite").exists()
    sqlite.close()
    with pytest.raises(ValueError, match="requires DATABASE_URL"):
        build_checkpointing("postgres")
    with pytest.raises(ValueError, match="must be one of"):
        build_checkpointing("redis")


@pytest.mark.parametrize(
    "env,message",
    [
        ({"CHECKPOINT_BACKEND": "redis"}, "CHECKPOINT_BACKEND"),
        ({"CHECKPOINT_BACKEND": "postgres"}, "CHECKPOINT_BACKEND=postgres requires DATABASE_URL"),
        ({"AGENT_RUN_MAX_RESUMES": "9"}, "AGENT_RUN_MAX_RESUMES"),
    ],
)
def test_bad_checkpoint_settings_stop_the_app_at_startup(monkeypatch, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ConfigError, match=message):
        validate_settings(load_settings())


def test_progress_is_kept_in_memory_unless_asked_otherwise():
    settings = load_settings()

    assert (settings.checkpoint_backend, settings.agent_run_max_resumes) == ("memory", 2)


# --- the whole app: die, restart, carry on ------------------------------------------------------------------


def test_the_app_resumes_an_interrupted_run_when_it_starts_again(make_app):
    env = {"AGENT_RUN_MODE": "manual", "CHECKPOINT_BACKEND": "sqlite"}
    first, client = make_app(**env)
    change_id = scenario_incident(client)
    first.agent_runtime.investigator = Dying(first.agent_runtime.investigator, "investigate")
    _, future = first.agent_runner.start(change_id, "approver-user")
    assert isinstance(future.exception(timeout=10), Killed)
    assert client.get(f"/v1/changes/{change_id}/agent-run", headers=VIEWER).json()["agent_run_status"] == "running"
    first.checkpointing.close()

    second, restarted_client = make_app(**env)  # the app starts again on the same data directory

    assert second.run_recovery["resumed"] == [change_id]
    run = wait_for_run(restarted_client, change_id)
    assert run["agent_run"]["resumes"] == 1 and run["agent_run"]["investigation"]["decision"] == "proceed"
    assert "no model is configured for the Fixer" in run["agent_run"]["escalation_reason"]  # it ran on, to its real end
    labels = [item["label"] for item in restarted_client.get(f"/v1/changes/{change_id}/review", headers=VIEWER).json()["timeline"]]
    assert "Run resumed after a restart" in labels
