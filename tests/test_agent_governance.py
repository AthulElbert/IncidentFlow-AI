"""The change-store methods an agent run uses to move a change. The graph can only reach what these allow."""
import pytest

from app.models.schemas import ApprovalDecisionRequest
from app.services.change_control import ChangeControlStore, PolicyConfig


def _store(tmp_path):
    path = tmp_path / "changes.json"
    path.write_text("[]", encoding="utf-8")
    return ChangeControlStore(str(path), policy=PolicyConfig())


def _change(store, alert=True, **overrides):
    params = dict(
        incident_id="INC-1", service="svc", environment="prod", summary="s", jira_key="SUP-1", jenkins_job_url="u",
        proposed_actions=[], triage_mode_used="heuristic", triage_hypothesis_steps=[], issue_type="unknown",
        confidence=0.5, warning_count=0, jenkins_status="QUEUED",
        alert={"service": "svc", "metric": "m", "value": 2, "threshold": 1, "timestamp": "2026-07-21T14:30:00+00:00",
               "message": "x", "environment": "prod"} if alert else None,
    )
    params.update(overrides)
    return store.create_change(**params)


def test_a_new_change_remembers_the_alert_that_opened_it(tmp_path):
    store = _store(tmp_path)
    change = _change(store)

    assert change.alert["metric"] == "m" and change.agent_run_status == "not_started" and change.agent_run == {}
    assert ChangeControlStore(str(tmp_path / "changes.json")).get_change(change.change_id).alert["metric"] == "m"


def test_begin_reserves_the_change_and_moves_it_to_investigating(tmp_path):
    store = _store(tmp_path)
    change = _change(store)

    started = store.begin_agent_run(change.change_id, "alice")

    assert started.deployment_state == "investigating" and started.agent_run_status == "running"
    assert started.agent_run["run"] == 1 and started.agent_run["attempts"] == []


def test_a_second_run_cannot_start_while_one_is_running(tmp_path):
    store = _store(tmp_path)
    change = _change(store)
    store.begin_agent_run(change.change_id, "alice")

    with pytest.raises(ValueError, match="already in progress"):
        store.begin_agent_run(change.change_id, "bob")


@pytest.mark.parametrize(
    "setup,message",
    [
        ("no_alert", "No alert was recorded"),
        ("rejected", "already finalized"),
        ("ready", "cannot start from state ready_for_prod"),
    ],
)
def test_a_run_cannot_start_when_there_is_nothing_to_investigate_or_it_is_too_late(tmp_path, setup, message):
    store = _store(tmp_path)
    change = _change(store, alert=setup != "no_alert")
    if setup == "rejected":
        store.apply_decision(change.change_id, ApprovalDecisionRequest(decision="reject"), "alice")
    if setup == "ready":
        store.record_dev_execution(change.change_id, "dev", "u", "SUCCESS", 5.0, True, True, "n")

    with pytest.raises(ValueError, match=message):
        store.begin_agent_run(change.change_id, "alice")


def test_steps_follow_the_lifecycle_table_and_only_allow_agent_states(tmp_path):
    store = _store(tmp_path)
    change = _change(store)
    store.begin_agent_run(change.change_id, "alice")

    for state in ["hypothesis_ready", "fixing", "verifying", "reviewing"]:
        assert store.record_agent_step(change.change_id, "agent:x", state, f"agent.{state}").deployment_state == state

    with pytest.raises(ValueError, match="not a state an agent run may set"):
        store.record_agent_step(change.change_id, "agent:x", "ready_for_prod", "agent.sneaky")
    with pytest.raises(ValueError, match="not a state an agent run may set"):
        store.record_agent_step(change.change_id, "agent:x", "promoted_to_prod", "agent.sneaky")
    with pytest.raises(ValueError, match="Illegal state transition"):
        store.record_agent_step(change.change_id, "agent:x", "hypothesis_ready", "agent.backwards")  # reviewing -> hypothesis_ready


def test_steps_need_a_running_run(tmp_path):
    store = _store(tmp_path)
    change = _change(store)

    with pytest.raises(ValueError, match="No agent run is in progress"):
        store.record_agent_step(change.change_id, "agent:x", "fixing", "agent.fixing")


def test_steps_merge_what_the_agent_produced_and_may_update_the_diagnosis(tmp_path):
    store = _store(tmp_path)
    change = _change(store)
    store.begin_agent_run(change.change_id, "alice")

    updated = store.record_agent_step(
        change.change_id, "agent:investigator", "hypothesis_ready", "agent.investigated",
        {"investigation": {"decision": "proceed"}}, issue_type="dependency_failure", confidence=0.82,
    )

    assert (updated.issue_type, updated.confidence) == ("dependency_failure", 0.82)
    assert updated.agent_run["investigation"] == {"decision": "proceed"} and updated.agent_run["run"] == 1
    assert updated.policy_reasons  # the policy gate is re-evaluated with the new diagnosis (dev evidence still missing)


def test_escalating_ends_the_run_with_a_reason(tmp_path):
    store = _store(tmp_path)
    change = _change(store)
    store.begin_agent_run(change.change_id, "alice")

    escalated = store.record_agent_step(change.change_id, "agent:fixer", "escalated", "agent.escalated", escalation_reason="no safe change")

    assert escalated.deployment_state == "escalated" and escalated.agent_run_status == "escalated"
    assert escalated.agent_run["escalation_reason"] == "no safe change" and "finished_at" in escalated.agent_run
    with pytest.raises(ValueError, match="No agent run is in progress"):
        store.record_agent_step(change.change_id, "agent:x", "fixing", "agent.fixing")


def test_finishing_opens_the_approval_gate_without_approving_anything(tmp_path):
    store = _store(tmp_path)
    change = _change(store)
    store.begin_agent_run(change.change_id, "alice")
    store.record_agent_step(change.change_id, "agent:investigator", "hypothesis_ready", "agent.investigated", issue_type="performance_degradation", confidence=0.9)
    for state in ["fixing", "verifying", "reviewing"]:
        store.record_agent_step(change.change_id, "agent:x", state, f"agent.{state}")

    done = store.finish_agent_run(change.change_id, "agent:verifier", "mock://dev/svc/1", 88.9, "verified", {"outcome": "ready"})

    assert done.deployment_state == "ready_for_prod" and done.agent_run_status == "ready_for_approval"
    assert done.status == "pending_approval" and done.decided_by is None and done.promoted_by is None
    assert (done.dev_execution_status, done.jenkins_status, done.dev_executed_by) == ("passed", "SUCCESS", "agent:verifier")
    assert done.dev_apm_improvement_pct == 88.9 and done.policy_reasons == []
    assert done.agent_run["outcome"] == "ready"


def test_the_policy_gate_still_stands_in_the_way_of_a_weak_diagnosis(tmp_path):
    store = _store(tmp_path)
    change = _change(store)
    store.begin_agent_run(change.change_id, "alice")
    store.record_agent_step(change.change_id, "agent:investigator", "hypothesis_ready", "agent.investigated", issue_type="dependency_failure", confidence=0.65)
    for state in ["fixing", "verifying", "reviewing"]:
        store.record_agent_step(change.change_id, "agent:x", state, f"agent.{state}")
    done = store.finish_agent_run(change.change_id, "agent:verifier", "u", 70.0, "n")

    assert done.deployment_state == "ready_for_prod"
    assert any("confidence 0.65 below minimum 0.80" in reason for reason in done.policy_reasons)
    blocked = store.apply_decision(change.change_id, ApprovalDecisionRequest(decision="approve"), "approver")
    assert blocked.deployment_state == "blocked_by_policy"


def test_finishing_needs_the_review_stage_and_a_running_run(tmp_path):
    store = _store(tmp_path)
    change = _change(store)

    with pytest.raises(ValueError, match="No agent run is in progress"):
        store.finish_agent_run(change.change_id, "agent:verifier", "u", 1.0, "n")

    store.begin_agent_run(change.change_id, "alice")  # investigating -> ready_for_prod is not a legal move
    with pytest.raises(ValueError, match="Illegal state transition"):
        store.finish_agent_run(change.change_id, "agent:verifier", "u", 1.0, "n")


def test_a_manual_dev_run_is_refused_while_the_agents_are_working(tmp_path):
    store = _store(tmp_path)
    change = _change(store)
    store.begin_agent_run(change.change_id, "alice")

    with pytest.raises(ValueError, match="agent run is in progress"):
        store.ensure_dev_executable(change.change_id)


def test_an_escalated_change_can_be_run_again_and_the_run_number_increases(tmp_path):
    store = _store(tmp_path)
    change = _change(store)
    store.begin_agent_run(change.change_id, "alice")
    store.record_agent_step(change.change_id, "agent:fixer", "escalated", "agent.escalated", {"attempts": [1]}, escalation_reason="x")

    again = store.begin_agent_run(change.change_id, "alice")

    assert again.agent_run["run"] == 2 and again.agent_run["attempts"] == [] and "escalation_reason" not in again.agent_run
    assert again.deployment_state == "investigating"


def test_a_restart_escalates_runs_that_were_left_running(tmp_path):
    store = _store(tmp_path)
    interrupted = _change(store)
    idle = _change(store, incident_id="INC-2")
    store.begin_agent_run(interrupted.change_id, "alice")
    store.record_agent_step(interrupted.change_id, "agent:x", "hypothesis_ready", "agent.investigated")

    reopened = ChangeControlStore(str(tmp_path / "changes.json"))
    recovered = reopened.recover_interrupted_runs()

    assert recovered == [interrupted.change_id]
    after = reopened.get_change(interrupted.change_id)
    assert after.deployment_state == "escalated" and "interrupted by a restart" in after.agent_run["escalation_reason"]
    assert reopened.get_change(idle.change_id).agent_run_status == "not_started"
    assert reopened.recover_interrupted_runs() == []


def test_every_agent_step_is_audited_with_its_actor(tmp_path):
    from app.audit import JsonlAuditLog

    audit = JsonlAuditLog(tmp_path / "audit.jsonl")
    path = tmp_path / "changes.json"
    path.write_text("[]", encoding="utf-8")
    store = ChangeControlStore(str(path), audit=audit)
    change = _change(store)

    store.begin_agent_run(change.change_id, "alice")
    store.record_agent_step(change.change_id, "agent:fixer", "escalated", "agent.escalated", escalation_reason="why")

    trail = audit.list(entity_id=change.change_id)
    assert [(e.action, e.actor) for e in trail] == [
        ("change.created", "pipeline"), ("agent.started", "alice"), ("agent.escalated", "agent:fixer"),
    ]
    assert trail[-1].details["reason"] == "why" and trail[-1].to_state == "escalated"
