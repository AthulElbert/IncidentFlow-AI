import importlib
import json
import threading
import time

import pytest

from agent_harness import (
    APPROVER,
    CONFIG_PATH,
    FIXED,
    RELEASE,
    VIEWER,
    TempRepoProvider,
    agent_run,
    done,
    edit,
    make_repo,
    scenario_incident,
    wait_for_run,
)
from fastapi.testclient import TestClient
from llm_fakes import ScriptedLLM

from app.models.schemas import ChangeRecord
from app.services.change_control import ChangeControlStore

_scenario_incident = scenario_incident
_agent_run = agent_run
_wait = wait_for_run


# --- off (the default) ---------------------------------------------------------------------


def test_by_default_nothing_changes_and_the_endpoint_says_why(make_app):
    main_module, client = make_app()
    change_id = _scenario_incident(client)

    assert main_module.pipeline.on_change_created is None
    assert _agent_run(client, change_id)["agent_run_status"] == "not_started"
    refused = client.post(f"/v1/changes/{change_id}/investigate", headers=APPROVER)
    assert refused.status_code == 409 and "AGENT_RUN_MODE=off" in refused.json()["detail"]


def test_every_incident_records_the_alert_that_opened_it(make_app):
    _, client = make_app()

    mock = client.post("/v1/incidents/mock", headers=VIEWER).json()["metadata"]["change_id"]

    alert = client.get(f"/v1/changes/{mock}", headers=VIEWER).json()["alert"]
    assert alert["service"] == "payments-service" and alert["metric"] == "latency_p99_ms" and alert["threshold"] == 800


# --- scenario incidents --------------------------------------------------------------------


def test_the_built_in_scenarios_can_be_listed_and_opened_as_incidents(make_app):
    _, client = make_app()

    scenarios = client.get("/v1/scenarios", headers=VIEWER).json()
    assert {s["name"] for s in scenarios} >= {"bad_config_latency", "dependency_timeouts", "log_prompt_injection"}
    assert all({"name", "service", "alert", "tags"} == set(s) for s in scenarios)

    change_id = _scenario_incident(client, "dependency_timeouts")
    change = client.get(f"/v1/changes/{change_id}", headers=VIEWER).json()
    assert change["service"] == "checkout-service" and change["alert"]["metric"] == "dependency_timeout_ms"
    assert client.post("/v1/incidents/scenario/nope", headers=APPROVER).status_code == 404
    assert client.post("/v1/incidents/scenario/dependency_timeouts", headers=VIEWER).status_code == 403


def test_simulated_incidents_are_refused_when_real_metrics_are_configured(make_app):
    _, client = make_app(METRICS_MODE="prometheus", METRICS_BASE_URL="http://127.0.0.1:9")

    refused = client.post("/v1/incidents/scenario/bad_config_latency", headers=APPROVER)

    assert refused.status_code == 409 and "real metrics" in refused.json()["detail"]


# --- manual mode ---------------------------------------------------------------------------


def test_manual_mode_runs_the_agents_on_request_and_without_a_fixer_hands_over_a_diagnosis(make_app):
    main_module, client = make_app(AGENT_RUN_MODE="manual")
    change_id = _scenario_incident(client)
    assert main_module.pipeline.on_change_created is None  # nothing starts by itself

    assert client.post(f"/v1/changes/{change_id}/investigate", headers=VIEWER).status_code == 403
    assert client.post("/v1/changes/CHG-NOPE/investigate", headers=APPROVER).status_code == 404
    started = client.post(f"/v1/changes/{change_id}/investigate", headers=APPROVER)

    assert started.status_code == 202 and started.json()["run"] == 1
    run = _wait(client, change_id)
    assert run["agent_run_status"] == "escalated" and run["deployment_state"] == "escalated"
    assert "no model is configured for the Fixer" in run["agent_run"]["escalation_reason"]
    assert run["agent_run"]["investigation"]["hypotheses"][0]["issue_type"] == "performance_degradation"
    trail = [e["action"] for e in client.get(f"/v1/changes/{change_id}/audit", headers=VIEWER).json()]
    assert {"agent.started", "agent.investigated", "agent.escalated"} <= set(trail)


def test_a_second_start_and_a_manual_dev_run_are_refused_while_the_agents_are_working(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual")
    gate = threading.Event()

    class SlowLLM(ScriptedLLM):
        def complete_json(self, messages, error_label="LLM"):
            gate.wait(10)  # the Fixer is parked here, mid-run, until the test opens the gate
            return super().complete_json(messages, error_label)

    main_module.fixer.llm = SlowLLM({"action": "final", "summary": "n/a", "rationale": "nothing safe to do", "no_fix": True})
    main_module.agent_runtime.sandbox_provider = TempRepoProvider(make_repo(tmp_path / "project"))
    change_id = _scenario_incident(client)
    try:
        assert client.post(f"/v1/changes/{change_id}/investigate", headers=APPROVER).status_code == 202
        deadline = time.monotonic() + 10
        while _agent_run(client, change_id)["deployment_state"] != "fixing" and time.monotonic() < deadline:
            time.sleep(0.05)
        assert _agent_run(client, change_id)["deployment_state"] == "fixing"

        again = client.post(f"/v1/changes/{change_id}/investigate", headers=APPROVER)
        manual = client.post(f"/v1/changes/{change_id}/execute-dev", headers=APPROVER, json={})
        approve = client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve"})
    finally:
        gate.set()

    assert again.status_code == 409 and "already in progress" in again.json()["detail"]
    assert manual.status_code == 400 and "agent run is in progress" in manual.json()["detail"]
    assert approve.status_code == 400  # not ready_for_prod yet, so there is nothing to approve
    assert _wait(client, change_id)["agent_run_status"] == "escalated"


# --- auto mode -----------------------------------------------------------------------------


def test_auto_mode_starts_a_run_for_every_new_incident(make_app):
    main_module, client = make_app(AGENT_RUN_MODE="auto")
    assert main_module.pipeline.on_change_created is not None

    scenario_change = _scenario_incident(client, "dependency_timeouts")
    mock_change = client.post("/v1/incidents/mock", headers=VIEWER).json()["metadata"]["change_id"]

    assert _wait(client, scenario_change)["agent_run"]["run"] == 1
    assert _wait(client, mock_change)["agent_run_status"] == "escalated"  # "now" has no data in the simulated tools
    started_by = {e["actor"] for e in client.get(f"/v1/changes/{scenario_change}/audit", headers=VIEWER).json() if e["action"] == "agent.started"}
    assert started_by == {"agent:orchestrator"}


def test_a_failure_to_start_a_run_never_breaks_incident_intake(make_app):
    main_module, client = make_app(AGENT_RUN_MODE="auto")

    def explode(*args, **kwargs):
        raise RuntimeError("scheduler pool is gone")

    main_module.agent_runner.start = explode  # type: ignore[method-assign]
    response = client.post("/v1/incidents/mock", headers=VIEWER)

    assert response.status_code == 200 and response.json()["metadata"]["change_id"].startswith("CHG-")


# --- the whole thing through the API -------------------------------------------------------


def test_a_fix_goes_from_alert_to_promotion_with_people_at_the_two_gates(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual")
    main_module.fixer.llm = ScriptedLLM(edit(CONFIG_PATH, FIXED), done("Lowered latency_ms"))
    main_module.agent_runtime.sandbox_provider = TempRepoProvider(make_repo(tmp_path / "project"))
    change_id = _scenario_incident(client)

    assert client.post(f"/v1/changes/{change_id}/investigate", headers=APPROVER).status_code == 202
    run = _wait(client, change_id)
    assert run["agent_run_status"] == "ready_for_approval" and run["deployment_state"] == "ready_for_prod"
    assert run["agent_run"]["review"]["decision"] == "approve_for_human"

    change = client.get(f"/v1/changes/{change_id}", headers=VIEWER).json()
    assert change["status"] == "pending_approval" and change["policy_reasons"] == []
    assert change["dev_executed_by"] == "agent:verifier" and change["issue_type"] == "performance_degradation"

    early = client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={})
    assert early.status_code == 400 and "must be approved" in early.json()["detail"]
    approved = client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve", "comment": "reviewed the diff"})
    assert approved.json()["deployment_state"] == "ready_for_prod"
    promoted = client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={"comment": "ship"})
    assert promoted.status_code == 200 and promoted.json()["deployment_state"] == "promoted_to_prod"

    actors = {(e["action"], e["actor"]) for e in client.get(f"/v1/changes/{change_id}/audit", headers=VIEWER).json()}
    assert ("agent.ready_for_approval", "agent:verifier") in actors
    assert ("decision.approved", "approver-user") in actors and ("promotion.success", "release-operator-user") in actors


# --- startup -------------------------------------------------------------------------------


def test_a_run_left_going_by_a_restart_is_escalated_when_the_app_starts(make_app, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    store = ChangeControlStore(str(data_dir / "change_records.json"))
    change = store.create_change(
        incident_id="INC-1", service="svc", environment="prod", summary="s", jira_key="SUP-1", jenkins_job_url="u",
        proposed_actions=[], triage_mode_used="heuristic", triage_hypothesis_steps=[], issue_type="unknown",
        confidence=0.5, warning_count=0, jenkins_status="QUEUED",
        alert={"service": "svc", "metric": "m", "value": 2, "threshold": 1, "timestamp": "2026-07-21T14:30:00+00:00", "message": "x"},
    )
    store.begin_agent_run(change.change_id, "alice")

    _, client = make_app()

    run = _agent_run(client, change.change_id)
    assert run["agent_run_status"] == "escalated" and "interrupted by a restart" in run["agent_run"]["escalation_reason"]


# --- settings ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env,message",
    [
        ({"AGENT_RUN_MODE": "yolo"}, "AGENT_RUN_MODE"),
        ({"AGENT_MAX_FIX_ATTEMPTS": "0"}, "AGENT_MAX_FIX_ATTEMPTS"),
        ({"AGENT_MAX_FIX_ATTEMPTS": "9"}, "AGENT_MAX_FIX_ATTEMPTS"),
        ({"AGENT_RUN_WORKERS": "0"}, "AGENT_RUN_WORKERS"),
    ],
)
def test_invalid_agent_run_settings_are_rejected_at_startup(monkeypatch, env, message):
    from app.config import ConfigError, load_settings, validate_settings

    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ConfigError, match=message):
        validate_settings(load_settings())


def test_the_defaults_leave_agent_runs_off(monkeypatch):
    from app.config import load_settings

    settings = load_settings()

    assert (settings.agent_run_mode, settings.agent_max_fix_attempts, settings.agent_run_workers) == ("off", 3, 2)
