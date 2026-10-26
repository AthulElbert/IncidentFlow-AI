"""Shadow mode: the agents work on real incidents, record what they would have done, and change nothing."""
import threading

import pytest
from agent_harness import (
    APPROVER,
    CONFIG_PATH,
    FIXED,
    VIEWER,
    TempRepoProvider,
    build,
    done,
    edit,
    make_repo,
    scenario_incident,
)
from llm_fakes import ScriptedLLM

from app.agents.budget import BudgetLimits
from app.audit import new_audit_entry
from app.models.schemas import ApprovalDecisionRequest, ChangeRecord
from app.services.review import build_timeline
from app.services.shadow import build_shadow_report


def _untouched(record):
    """Everything a shadow run must leave alone."""
    return (
        record.deployment_state, record.agent_run_status, record.status, record.issue_type, record.confidence,
        record.dev_execution_status, record.dev_executed_by, record.policy_reasons, record.jenkins_status, record.agent_run,
    )


# --- a shadow run, through the real graph -----------------------------------------------------


def test_a_shadow_run_records_the_fix_it_would_have_made_and_changes_nothing_else(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done("Lowered latency_ms")])
    before = _untouched(h.record())

    record = h.runner.run_shadow_sync(h.change_id, "tester")

    shadow = record.shadow
    assert shadow["status"] == "ready" and shadow["run"] == 1
    assert shadow["verification"]["verdict"] == "passed" and shadow["review"]["decision"] == "approve_for_human"
    assert CONFIG_PATH in shadow["diff"]["text"] and shadow["changed_files"] == [CONFIG_PATH]
    assert shadow["budget"]["tool_calls"] > 0
    assert _untouched(record) == before  # no state change, no evidence, no approval gate, no real run recorded
    assert h.provider.opened == [f"agent-shadow/{h.change_id.lower()}-faulty-project"]


def test_a_shadow_run_never_emits_a_state_change_and_every_audit_entry_is_marked(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])

    h.runner.run_shadow_sync(h.change_id, "tester")

    events = [e.payload for e in h.bus.poll("incident.events", "t", limit=500) if e.payload["change_id"] == h.change_id]
    assert {e["from_state"] for e in events[1:]} == {e["to_state"] for e in events[1:]} == {"awaiting_dev_execution"}
    actions = [e.action for e in h.audit.list(entity_id=h.change_id, limit=500) if not e.action.startswith("tool.")]
    assert [a for a in actions if a != "change.created"] and all(a.startswith("shadow.") for a in actions if a != "change.created")
    assert "shadow.investigated" in actions and "shadow.ready" in actions and "agent.ready_for_approval" not in actions


def test_without_a_fixer_the_shadow_run_would_escalate_and_says_why(tmp_path):
    h = build(tmp_path, with_fixer_llm=False)
    before = _untouched(h.record())

    record = h.runner.run_shadow_sync(h.change_id, "tester")

    assert record.shadow["status"] == "escalated" and "no model is configured" in record.shadow["escalation_reason"]
    assert record.shadow["investigation"]["hypotheses"][0]["issue_type"] == "performance_degradation"
    assert _untouched(record) == before


def test_a_shadow_run_is_held_to_the_same_budgets(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])
    h.runtime.budget_limits = BudgetLimits(max_llm_calls=1)

    record = h.runner.run_shadow_sync(h.change_id, "tester")

    assert record.shadow["status"] == "escalated" and "1 model calls" in record.shadow["escalation_reason"]


def test_a_second_shadow_run_waits_for_the_first_and_a_real_run_can_follow_a_shadow_one(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done(), edit(CONFIG_PATH, FIXED), done()])
    h.store.begin_shadow_run(h.change_id, "tester")

    with pytest.raises(ValueError, match="already in progress"):
        h.store.begin_shadow_run(h.change_id, "tester")
    h.store.record_shadow_step(h.change_id, "t", "interrupted", escalated=True, escalation_reason="test")

    h.runner.run_shadow_sync(h.change_id, "tester")  # a new shadow run (run 2) is fine once the first has ended
    real = h.run()

    assert real.shadow["run"] == 2 and real.agent_run_status == "ready_for_approval" and real.deployment_state == "ready_for_prod"


def test_a_shadow_run_is_refused_once_a_person_has_decided(tmp_path):
    h = build(tmp_path)
    h.store.apply_decision(h.change_id, ApprovalDecisionRequest(decision="reject"), "someone")

    with pytest.raises(ValueError, match="already finalized"):
        h.store.begin_shadow_run(h.change_id, "tester")


def test_a_restart_ends_a_shadow_run_that_was_in_flight(tmp_path):
    h = build(tmp_path)
    h.store.begin_shadow_run(h.change_id, "tester")

    assert h.store.recover_interrupted_runs() == [h.change_id]

    shadow = h.record().shadow
    assert shadow["status"] == "escalated" and "restart" in shadow["escalation_reason"]


# --- through the real app ------------------------------------------------------------------------


def test_in_shadow_mode_a_new_incident_gets_a_shadow_run_and_humans_see_an_unchanged_change(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="shadow")
    main_module.fixer.llm = ScriptedLLM(edit(CONFIG_PATH, FIXED), done("Lowered latency_ms"))
    main_module.agent_runtime.sandbox_provider = TempRepoProvider(make_repo(tmp_path / "project"))
    assert main_module.pipeline.on_change_created is not None

    change_id = scenario_incident(client)
    deadline = threading.Event()
    for _ in range(400):
        review = client.get(f"/v1/changes/{change_id}/review", headers=VIEWER).json()
        if review["shadow"] and review["shadow"]["status"] in {"ready", "escalated"}:
            break
        deadline.wait(0.05)

    change = client.get(f"/v1/changes/{change_id}", headers=VIEWER).json()
    assert change["deployment_state"] == "awaiting_dev_execution" and change["agent_run_status"] == "not_started"
    assert change["dev_execution_status"] == "not_started"
    shadow = review["shadow"]
    assert shadow["status"] == "ready" and shadow["would"] == "propose_a_verified_fix"
    assert shadow["fix"]["files"] == [CONFIG_PATH] and "latency_ms" in shadow["fix"]["diff"]["text"]
    assert shadow["verification"]["verdict"] == "passed"
    # the approval flow is untouched: nothing to approve, and the agents cannot be started for real
    actions = {a["name"]: a for a in review["actions"]}
    assert not actions["approve"]["allowed"] and not actions["run_agents"]["allowed"]
    assert "shadow mode" in actions["run_agents"]["reason"]
    assert review["attention"] is None and review["fix"] is None
    assert any(item["label"].endswith("(shadow)") for item in review["timeline"])


def test_shadow_mode_refuses_real_runs_and_shadow_runs_need_shadow_mode(make_app):
    _, shadow_client = make_app(AGENT_RUN_MODE="shadow")
    change_id = scenario_incident(shadow_client)
    refused = shadow_client.post(f"/v1/changes/{change_id}/investigate", headers=APPROVER)
    assert refused.status_code == 409 and "shadow mode" in refused.json()["detail"]
    assert shadow_client.post(f"/v1/changes/{change_id}/shadow-run", headers=VIEWER).status_code == 403
    assert shadow_client.post("/v1/changes/CHG-NOPE/shadow-run", headers=APPROVER).status_code == 404

    _, manual_client = make_app(AGENT_RUN_MODE="manual")
    other = scenario_incident(manual_client)
    wrong_mode = manual_client.post(f"/v1/changes/{other}/shadow-run", headers=APPROVER)
    assert wrong_mode.status_code == 409 and "AGENT_RUN_MODE=shadow" in wrong_mode.json()["detail"]


def test_shadow_is_a_valid_mode_and_the_report_endpoint_is_readable_by_viewers(make_app):
    _, client = make_app(AGENT_RUN_MODE="shadow")

    report = client.get("/v1/shadow/report", headers=VIEWER)

    assert report.status_code == 200 and report.json()["runs"] == 0 and "proxy" in report.json()["caveat"]
    assert client.get("/v1/shadow/report").status_code == 401


# --- the report ----------------------------------------------------------------------------------


def _record(tmp_path, name, **update):
    from app.services.change_control import ChangeControlStore

    path = tmp_path / f"{name}.json"
    path.write_text("[]", encoding="utf-8")
    store = ChangeControlStore(str(path))
    change = store.create_change(
        incident_id=f"INC-{name}", service="svc", environment="prod", summary="s", jira_key="K", jenkins_job_url="u",
        proposed_actions=[], triage_mode_used="heuristic", triage_hypothesis_steps=[], issue_type="performance_degradation",
        confidence=0.9, warning_count=0, jenkins_status="SUCCESS",
    )
    return ChangeRecord(**{**change.model_dump(), **update})


def _ready(files, issue="performance_degradation"):
    return {"status": "ready", "changed_files": files, "investigation": {"hypotheses": [{"issue_type": issue}]}}


def test_the_report_sets_what_the_agents_would_have_done_against_what_people_did(tmp_path):
    changes = [
        _record(tmp_path, "a", shadow=_ready(["cfg.json"]), deployment_state="promoted_to_prod", status="approved", changed_files=["cfg.json"]),
        _record(tmp_path, "b", shadow=_ready(["cfg.json"]), deployment_state="promoted_to_prod", status="approved", changed_files=["other.yaml"]),
        _record(tmp_path, "c", shadow=_ready(["a.py"], "dependency_failure"), status="rejected", deployment_state="closed_rejected"),
        _record(tmp_path, "d", shadow={"status": "escalated", "escalation_reason": "no model", "investigation": {"hypotheses": [{"issue_type": "performance_degradation"}]}},
                deployment_state="promoted_to_prod", status="approved", changed_files=["cfg.json"]),
        _record(tmp_path, "e", shadow=_ready(["cfg.json"])),
        _record(tmp_path, "f", shadow={"status": "running"}),
        _record(tmp_path, "g"),
    ]

    report = build_shadow_report(changes)

    assert report["runs"] == 5 and report["running"] == 1
    assert report["agent_would"] == {"propose_a_verified_fix": 4, "escalate": 1}
    assert report["against_what_people_did"] == {
        "fix_ready": {"released": 2, "rejected": 1, "undecided": 1},
        "escalated": {"released": 1},
    }
    assert report["diagnosis_agreement_with_triage"] == {"compared": 5, "same": 4}
    assert report["files"] == {"compared": 2, "same": 1, "overlapping": 0, "different": 1}
    by_id = {item["change_id"]: item for item in report["changes"]}
    assert by_id[changes[0].change_id]["files"] == "same" and by_id[changes[1].change_id]["files"] == "different"
    assert by_id[changes[3].change_id]["agent"] == "escalated" and by_id[changes[3].change_id]["escalation_reason"] == "no model"


def test_an_empty_report_is_honest_about_having_nothing_to_say():
    report = build_shadow_report([])

    assert report["runs"] == 0 and report["agent_would"] == {"propose_a_verified_fix": 0, "escalate": 0}
    assert report["files"]["compared"] == 0


def test_shadow_entries_in_the_timeline_are_marked():
    entries = [
        new_audit_entry("agent:orchestrator", "shadow.started", "CHG-1", "awaiting_dev_execution", "awaiting_dev_execution"),
        new_audit_entry("agent:verifier", "shadow.ready", "CHG-1", "awaiting_dev_execution", "awaiting_dev_execution"),
    ]

    assert [item["label"] for item in build_timeline(entries)] == [
        "Agent run started (shadow)", "A verified fix would be ready for approval (shadow)",
    ]
