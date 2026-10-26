import json
import subprocess

import pytest
from agent_harness import BROKEN, CONFIG_PATH, FIXED, INVALID, NO_RETRIES, build, done, edit

from app.agents.models import Review
from app.models.schemas import ApprovalDecisionRequest, PromoteRequest
from app.tools.scenarios import RED_HERRING_DEPLOYS, UNEXPLAINED_ALERT


def _states(h):
    """The sequence of deployment states the change moved through, from the event bus."""
    events = h.bus.poll("incident.events", "test", limit=500)
    return [e.payload["to_state"] for e in events if e.payload["change_id"] == h.change_id]


def _actions(h):
    return [e.action for e in h.audit.list(entity_id=h.change_id, limit=500) if not e.action.startswith("tool.")]


def _repo_file(h, path):
    return (h.root / path).read_text(encoding="utf-8")


# --- the happy path -----------------------------------------------------------------------


def test_a_good_fix_ends_ready_for_a_human_with_everything_recorded(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])

    record = h.run()

    assert (record.deployment_state, record.agent_run_status, record.status) == ("ready_for_prod", "ready_for_approval", "pending_approval")
    assert (record.issue_type, record.confidence) == ("performance_degradation", 0.85)
    assert (record.dev_execution_status, record.jenkins_status, record.dev_executed_by) == ("passed", "SUCCESS", "agent:verifier")
    assert record.dev_execution_url.startswith("mock://dev/") and record.dev_apm_improvement_pct > 80
    assert record.policy_reasons == []
    run = record.agent_run
    assert run["outcome"] == "ready_for_approval" and run["risk"] == "low" and run["changed_files"] == [CONFIG_PATH]
    assert run["investigation"]["decision"] == "proceed" and run["verification"]["verdict"] == "passed"
    assert run["review"]["decision"] == "approve_for_human"
    assert [a["attempt"] for a in run["attempts"]] == [1]
    assert _repo_file(h, CONFIG_PATH) == FIXED


def test_the_change_moves_through_every_stage_in_order(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])

    h.run()

    assert _states(h) == [
        "awaiting_dev_execution", "investigating", "hypothesis_ready", "fixing", "verifying", "verifying",
        "reviewing", "reviewing", "ready_for_prod",
    ]
    assert _actions(h) == [
        "change.created", "agent.started", "agent.investigated", "agent.fixing", "agent.verifying",
        "agent.verified", "agent.reviewing", "agent.reviewed", "agent.ready_for_approval",
    ]


def test_each_agent_acts_under_its_own_name_in_the_audit_trail(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])

    h.run()

    trail = h.audit.list(entity_id=h.change_id, limit=500)
    assert {e.actor for e in trail if e.action.startswith("tool.")} == {
        "agent:investigator", "agent:fixer", "agent:verifier", "agent:critic",
    }
    actor_of = {e.action: e.actor for e in trail}
    assert actor_of["agent.started"] == "tester"
    assert actor_of["agent.investigated"] == "agent:investigator"
    assert actor_of["agent.verified"] == "agent:verifier" and actor_of["agent.reviewed"] == "agent:critic"
    assert actor_of["agent.ready_for_approval"] == "agent:verifier"
    assert not any(e.action == "tool.sandbox.edit" and e.actor != "agent:fixer" for e in trail)


def test_the_run_stops_at_the_gate_and_a_person_still_has_to_approve_and_a_different_one_promote(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])
    record = h.run()
    assert record.decided_by is None and record.promoted_by is None and record.status == "pending_approval"

    approved = h.store.apply_decision(h.change_id, ApprovalDecisionRequest(decision="approve", comment="reviewed"), "approver-1")
    assert approved.status == "approved" and approved.deployment_state == "ready_for_prod"
    with pytest.raises(ValueError, match="different actor"):
        h.store.begin_promotion(h.change_id, PromoteRequest(), "approver-1")
    promoting = h.store.begin_promotion(h.change_id, PromoteRequest(), "release-1")
    assert promoting.deployment_state == "promoting"


def test_a_weakly_supported_diagnosis_still_cannot_get_through_the_policy_gate(tmp_path):
    h = build(tmp_path, [edit("config/other.yaml", "pool_size: 100\n"), done()], scenario=RED_HERRING_DEPLOYS, history=False)

    record = h.run()

    assert record.deployment_state == "ready_for_prod" and record.confidence == 0.65
    assert any("confidence 0.65 below minimum 0.80" in r for r in record.policy_reasons)
    assert h.store.apply_decision(h.change_id, ApprovalDecisionRequest(decision="approve"), "approver-1").deployment_state == "blocked_by_policy"


# --- retries ------------------------------------------------------------------------------


def test_a_failed_attempt_is_retried_with_the_verifiers_feedback_on_a_clean_worktree(tmp_path):
    h = build(tmp_path, [
        edit(CONFIG_PATH, INVALID), edit("config/other.yaml", "stray: true\n"), done("first try"),
        edit(CONFIG_PATH, FIXED), done("second try"),
    ])

    record = h.run()

    assert record.deployment_state == "ready_for_prod" and [a["attempt"] for a in record.agent_run["attempts"]] == [1, 2]
    first, second = record.agent_run["attempts"]
    assert first["verification"]["verdict"] == "failed" and "syntax" in {c["name"] for c in first["verification"]["checks"] if c["status"] == "failed"}
    assert second["verification"]["verdict"] == "passed"
    retry_task = json.loads(h.llm.calls[-1][1]["content"])
    assert retry_task["attempt"] == 2 and "syntax" in retry_task["feedback"]["failed_checks"]
    # The stray edit from the first attempt was wiped; only the second attempt's change remains.
    assert _repo_file(h, "config/other.yaml") == "pool_size: 10\n"
    assert record.agent_run["changed_files"] == [CONFIG_PATH]
    assert _states(h)[3:9] == ["fixing", "verifying", "verifying", "fixing", "verifying", "verifying"]


def test_a_fix_that_never_passes_verification_is_escalated_after_the_attempts_are_used(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, INVALID), done()] * 3)

    record = h.run()

    assert (record.deployment_state, record.agent_run_status) == ("escalated", "escalated")
    assert "verification failed on all 3 attempt(s)" in record.agent_run["escalation_reason"]
    assert len(record.agent_run["attempts"]) == 3
    assert record.dev_execution_status == "not_started"  # no agent evidence was recorded for a fix that never worked
    assert h.env.deployed(record.service) is None and h.env._counter == 0  # and nothing broken was ever deployed


def test_the_number_of_attempts_is_configurable(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, INVALID), done()] * 3, max_attempts=1)

    record = h.run()

    assert record.deployment_state == "escalated" and "all 1 attempt(s)" in record.agent_run["escalation_reason"]
    assert len(h.llm.calls) == 2  # one attempt, no retry


def test_a_fix_that_deploys_but_does_not_help_is_retried_then_escalated(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()] * 3, effect="no_effect")

    record = h.run()

    assert record.deployment_state == "escalated"
    assert "alert_metric_recovered" in record.agent_run["escalation_reason"]
    assert h.env._counter == 3  # it did deploy each time, and was measured each time


def test_the_critic_can_send_a_fix_back_and_the_fixer_corrects_it(tmp_path):
    h = build(tmp_path, [
        edit(CONFIG_PATH, FIXED), edit("config/other.yaml", "pool_size: 99\n"), done("also tuned the pool"),
        edit(CONFIG_PATH, FIXED), done("config only"),
    ])

    record = h.run()

    assert record.deployment_state == "ready_for_prod"
    first, second = record.agent_run["attempts"]
    assert first["verification"]["verdict"] == "passed" and first["review"]["decision"] == "send_back"
    assert second["review"]["decision"] == "approve_for_human"
    assert "config/other.yaml" in json.loads(h.llm.calls[-1][1]["content"])["feedback"]["failed_checks"]
    assert _repo_file(h, "config/other.yaml") == "pool_size: 10\n"


def test_a_critic_that_keeps_sending_it_back_ends_in_escalation(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), edit("config/other.yaml", "pool_size: 99\n"), done()] * 2, max_attempts=2)

    record = h.run()

    assert record.deployment_state == "escalated" and "sent the fix back on all 2 attempt(s)" in record.agent_run["escalation_reason"]


# --- escalation ---------------------------------------------------------------------------


def test_an_unexplained_alert_is_escalated_before_any_fix_is_attempted(tmp_path):
    h = build(tmp_path, [], scenario=UNEXPLAINED_ALERT)

    record = h.run()

    assert record.deployment_state == "escalated" and record.issue_type == "unknown"
    assert "does not support a specific cause" in record.agent_run["escalation_reason"]
    assert h.llm.calls == [] and h.provider.opened == []  # the Fixer was never consulted and no worktree was created
    assert record.agent_run["investigation"]["decision"] == "escalate"


def test_without_a_fixer_model_the_run_investigates_then_hands_over_and_creates_no_branch(tmp_path):
    h = build(tmp_path, with_fixer_llm=False)

    record = h.run()

    assert record.deployment_state == "escalated"
    assert "no model is configured for the Fixer" in record.agent_run["escalation_reason"]
    assert record.agent_run["investigation"]["decision"] == "proceed"  # the diagnosis is still useful to the human
    assert (record.issue_type, record.confidence) == ("performance_degradation", 0.85)
    assert h.provider.opened == []


def test_a_fixer_that_declines_escalates_with_its_reason(tmp_path):
    h = build(tmp_path, [{"action": "final", "summary": "n/a", "rationale": "The cause is in a vendor system.", "no_fix": True}])

    record = h.run()

    assert record.deployment_state == "escalated" and "vendor system" in record.agent_run["escalation_reason"]


def test_an_inconclusive_verification_is_escalated_without_retrying(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()], dev_has_service=False)

    record = h.run()

    assert record.deployment_state == "escalated" and "inconclusive" in record.agent_run["escalation_reason"]
    assert len(h.llm.calls) == 2 and len(record.agent_run["attempts"]) == 1  # more attempts would not create missing data


def test_a_critic_block_escalates_even_after_a_passed_verification(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])
    h.runtime.critic.review = lambda ctx, inv, prop, ver, tools: Review(  # type: ignore[method-assign]
        incident_id=ctx.incident_id, decision="block", risk="high", feedback_for_fixer="- looks like a workaround",
    )

    record = h.run()

    assert record.deployment_state == "escalated" and "blocked the fix" in record.agent_run["escalation_reason"]
    assert record.dev_execution_status == "not_started"


def test_an_unexpected_crash_escalates_instead_of_leaving_the_change_stuck(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])
    h.runtime.verifier.verify = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))  # type: ignore[method-assign]

    record = h.run()

    assert (record.deployment_state, record.agent_run_status) == ("escalated", "escalated")
    assert "failed unexpectedly" in record.agent_run["escalation_reason"] and "verify: RuntimeError: boom" in record.agent_run["escalation_reason"]
    assert "agent.failed" in _actions(h)


# --- re-running and refusing --------------------------------------------------------------


def test_an_escalated_change_can_be_run_again_once_a_fixer_is_available(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()], with_fixer_llm=False)
    assert h.run().deployment_state == "escalated"

    h.runtime.fixer.llm = h.llm
    record = h.run()

    assert record.deployment_state == "ready_for_prod" and record.agent_run["run"] == 2
    assert [a["attempt"] for a in record.agent_run["attempts"]] == [1]


def test_a_finished_run_cannot_be_started_again(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])
    h.run()

    with pytest.raises(ValueError, match="cannot start from state ready_for_prod"):
        h.run()


def test_only_the_fixed_file_differs_from_the_base_commit_afterwards(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])

    h.run()

    changed = subprocess.run(["git", "diff", "--name-only", "HEAD"], cwd=h.root, capture_output=True, text=True).stdout.split()
    assert changed == [CONFIG_PATH]


def test_the_released_worktree_is_forgotten_after_a_run(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])

    h.run()

    assert h.runtime._sandboxes == {}
