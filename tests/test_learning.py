"""What happens to a change becomes what incident memory knows, and a postmortem draft is available afterwards."""
import threading
from datetime import datetime, timezone

import pytest
from agent_harness import (
    APPROVER,
    CONFIG_PATH,
    FIXED,
    RELEASE,
    VIEWER,
    TempRepoProvider,
    done,
    edit,
    make_repo,
    scenario_incident,
    wait_for_run,
)
from llm_fakes import ScriptedLLM

from app.models.schemas import UNRESOLVED_RESOLUTION, IncidentRecord
from app.services.consumer import ConsumerLoop
from app.services.knowledge_base import IncidentKnowledgeBase
from app.services.learning import OutcomeLearner
from app.tools.memory import LexicalIncidentMemory

NOW = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _incident(incident_id="INC-1", outcome="historical", resolution="Rolled back the bad config deploy.", **kw):
    return IncidentRecord(
        incident_id=incident_id, service="faulty-project", metric="latency_p95_seconds",
        issue_type="performance_degradation", summary="config deploy raised latency", resolution=resolution,
        created_at=NOW, outcome=outcome, **kw,
    )


# --- which incidents may teach ---------------------------------------------------------------


def test_only_historical_and_resolved_incidents_with_a_real_resolution_are_reusable():
    assert _incident(outcome="historical").reusable and _incident(outcome="resolved").reusable
    assert not _incident(outcome="open").reusable and not _incident(outcome="rejected").reusable
    assert not _incident(outcome="resolved", resolution=UNRESOLVED_RESOLUTION).reusable


def test_incidents_saved_before_outcomes_existed_still_load_as_historical(tmp_path):
    path = tmp_path / "kb.json"
    path.write_text('[{"incident_id": "INC-OLD", "service": "s", "metric": "m", "issue_type": "t", "summary": "x", '
                    '"resolution": "Restarted it.", "created_at": "2026-01-01T00:00:00Z"}]', encoding="utf-8")

    old = IncidentKnowledgeBase(str(path)).get("INC-OLD")

    assert old.outcome == "historical" and old.reusable


def test_memory_offers_only_reusable_incidents():
    records = [_incident("INC-H", "historical"), _incident("INC-R", "resolved"), _incident("INC-O", "open"), _incident("INC-X", "rejected")]

    found = LexicalIncidentMemory(lambda: records).search("config deploy raised latency", service="faulty-project", k=10)

    assert {item.incident_id for item in found} == {"INC-H", "INC-R"}


def test_an_unresolved_incident_is_still_similar_but_lends_no_resolution(tmp_path):
    kb = IncidentKnowledgeBase(str(tmp_path / "kb.json"))
    kb.add_record(_incident("INC-OPEN", "open", resolution="Copied from somewhere else."))
    kb.add_record(_incident("INC-DONE", "resolved", resolution="Reverted the config."))

    similar = {item.incident_id: item for item in kb.find_similar("faulty-project", "latency_p95_seconds", "performance_degradation")}

    assert similar["INC-OPEN"].resolution == UNRESOLVED_RESOLUTION  # counts for recurrence, teaches nothing
    assert similar["INC-DONE"].resolution == "Reverted the config."


def test_updating_a_record_is_saved_and_an_unknown_one_is_refused(tmp_path):
    path = str(tmp_path / "kb.json")
    kb = IncidentKnowledgeBase(path)
    kb.add_record(_incident("INC-1", "open"))

    kb.update_record(_incident("INC-1", "resolved", resolution="Done."))

    assert IncidentKnowledgeBase(path).get("INC-1").outcome == "resolved"
    with pytest.raises(ValueError):
        kb.update_record(_incident("INC-NOPE"))


# --- the pipeline no longer copies resolutions that nobody confirmed -------------------------------


def test_a_new_incident_does_not_inherit_the_placeholder_of_the_one_before_it(make_app):
    main_module, client = make_app()
    first = scenario_incident(client)
    second = scenario_incident(client)
    kb = main_module.pipeline.kb
    incidents = {c: client.get(f"/v1/changes/{c}", headers=VIEWER).json()["incident_id"] for c in (first, second)}

    assert kb.get(incidents[first]).outcome == "open" and kb.get(incidents[second]).outcome == "open"
    assert kb.get(incidents[second]).resolution == UNRESOLVED_RESOLUTION


# --- the whole loop through the real app -------------------------------------------------------------


def _verified(main_module, client, tmp_path):
    main_module.fixer.llm = ScriptedLLM(edit(CONFIG_PATH, FIXED), done("Lowered latency_ms"))
    main_module.agent_runtime.sandbox_provider = TempRepoProvider(make_repo(tmp_path / "project"))
    change_id = scenario_incident(client)
    assert client.post(f"/v1/changes/{change_id}/investigate", headers=APPROVER).status_code == 202
    assert wait_for_run(client, change_id)["agent_run_status"] == "ready_for_approval"
    return change_id


def _incident_of(main_module, client, change_id):
    incident_id = client.get(f"/v1/changes/{change_id}", headers=VIEWER).json()["incident_id"]
    return main_module.pipeline.kb.get(incident_id)


def test_a_released_fix_becomes_a_resolved_incident_that_memory_can_offer(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual")
    change_id = _verified(main_module, client, tmp_path)
    assert _incident_of(main_module, client, change_id).outcome == "open"
    client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve"})
    assert client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={}).status_code == 200

    handled = main_module.outcome_learner.run_once()

    incident = _incident_of(main_module, client, change_id)
    assert handled > 0 and incident.outcome == "resolved" and incident.outcome_at is not None
    assert CONFIG_PATH in incident.resolution and "Lowered latency_ms" in incident.resolution
    assert "approved by approver-user" in incident.resolution.lower() and "released by release-operator-user" in incident.resolution
    assert "went from" in incident.resolution
    memory = main_module.toolkit.memory.search("config deploy latency", service=incident.service)
    assert incident.incident_id in [item.incident_id for item in memory]


def test_learning_the_same_outcome_twice_changes_nothing(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual")
    change_id = _verified(main_module, client, tmp_path)
    client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve"})
    client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={})
    main_module.outcome_learner.run_once()
    first = _incident_of(main_module, client, change_id)

    assert main_module.outcome_learner.learn(change_id) is None
    assert main_module.outcome_learner.reconcile() == []
    assert _incident_of(main_module, client, change_id) == first


def test_a_rejected_fix_is_remembered_as_rejected_and_never_offered(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual")
    change_id = _verified(main_module, client, tmp_path)
    client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "reject", "comment": "wrong service"})

    main_module.outcome_learner.run_once()

    incident = _incident_of(main_module, client, change_id)
    assert incident.outcome == "rejected" and "wrong service" in incident.outcome_note
    assert not incident.reusable
    assert incident.incident_id not in [i.incident_id for i in main_module.toolkit.memory.search("config deploy latency", service=incident.service)]


def test_a_fix_waiting_for_a_decision_or_an_escalation_teaches_nothing(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual")
    waiting = _verified(main_module, client, tmp_path)
    main_module.fixer.llm = None  # the next run has no Fixer, so it escalates
    handed_over = scenario_incident(client, "dependency_timeouts")
    client.post(f"/v1/changes/{handed_over}/investigate", headers=APPROVER)
    wait_for_run(client, handed_over)

    main_module.outcome_learner.run_once()

    assert _incident_of(main_module, client, waiting).outcome == "open"
    assert _incident_of(main_module, client, handed_over).outcome == "open"


def test_a_restart_that_lost_the_events_still_learns_the_outcome(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual")
    change_id = _verified(main_module, client, tmp_path)
    client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve"})
    client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={})
    restarted = OutcomeLearner(main_module.change_store, main_module.pipeline.kb, bus=None)  # no bus survived

    assert restarted.reconcile() == [change_id]
    assert _incident_of(main_module, client, change_id).outcome == "resolved"


# --- the background consumer ---------------------------------------------------------------------------


def test_the_consumer_loop_keeps_going_after_a_failure_and_stops_when_asked():
    calls, ran_twice = [], threading.Event()

    def work():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first attempt fails")
        ran_twice.set()
        return 0

    loop = ConsumerLoop("test", work, interval_seconds=0.1)
    loop.start()
    assert ran_twice.wait(5) and loop.running
    loop.stop()

    assert not loop.running and len(calls) >= 2


# --- postmortem draft ----------------------------------------------------------------------------------


def test_the_postmortem_draft_is_built_from_the_records_and_leaves_judgement_to_people(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual")
    change_id = _verified(main_module, client, tmp_path)

    early = client.get(f"/v1/changes/{change_id}/postmortem", headers=VIEWER).json()
    assert early["final"] is False

    client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve", "comment": "diff reviewed"})
    client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={})
    body = client.get(f"/v1/changes/{change_id}/postmortem", headers=VIEWER).json()
    text = body["markdown"]

    assert body["final"] is True
    for heading in ("# Postmortem draft", "## Summary", "## Diagnosis", "## What was changed", "## How it was checked", "## Decisions", "## Timeline"):
        assert heading in text
    assert "```diff" in text and '+  "latency_ms": 120' in text and CONFIG_PATH in text
    assert "Approved by approver-user" in text
    assert "diff reviewed" in text and "Released by release-operator-user" in text
    assert "- [ ] Was the diagnosis right?" in text and "must review and edit" in text
    assert client.get("/v1/changes/CHG-NOPE/postmortem", headers=VIEWER).status_code == 404
    assert client.get(f"/v1/changes/{change_id}/postmortem").status_code == 401


def test_the_postmortem_of_a_change_the_agents_gave_up_on_says_so(make_app):
    _, client = make_app(AGENT_RUN_MODE="manual")
    change_id = scenario_incident(client)
    client.post(f"/v1/changes/{change_id}/investigate", headers=APPROVER)
    wait_for_run(client, change_id)

    text = client.get(f"/v1/changes/{change_id}/postmortem", headers=VIEWER).json()["markdown"]

    assert "No agent-produced change" in text and "did not reach production" in text


# --- how the agents are doing ------------------------------------------------------------------------------


def test_the_summary_reports_how_often_agents_escalate_and_people_override_them(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual")
    rejected = _verified(main_module, client, tmp_path)
    client.post(f"/v1/changes/{rejected}/decision", headers=APPROVER, json={"decision": "reject"})
    main_module.fixer.llm = None
    escalated = scenario_incident(client, "dependency_timeouts")
    client.post(f"/v1/changes/{escalated}/investigate", headers=APPROVER)
    wait_for_run(client, escalated)
    scenario_incident(client)  # never run

    summary = client.get("/v1/metrics/summary", headers=VIEWER).json()

    assert summary["total_changes"] == 3 and summary["agent_runs"] == 2
    assert summary["agent_escalation_rate"] == 0.5 and summary["agent_fix_ready_rate"] == 0.5
    assert summary["human_override_rate"] == 1.0
    assert summary["avg_agent_run_seconds"] >= 0
