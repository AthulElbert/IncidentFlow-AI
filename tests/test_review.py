"""The case file the dashboard shows a person: overview, review pack, timeline and which actions are open."""
import hashlib

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

from app.agents.graph import MAX_STORED_DIFF_CHARS, stored_diff
from app.audit import new_audit_entry
from app.services.review import build_timeline

OVERVIEW_KEYS = {
    "change_id", "service", "issue_type", "confidence", "status", "deployment_state", "agent_run_status",
    "triage_mode_used", "alert_count", "created_at", "version", "attention", "pr_status", "dev_execution_status",
}


def _entry(actor, action, from_state=None, to_state=None, details=None, entity_type="change"):
    return new_audit_entry(
        actor=actor, action=action, entity_id="CHG-1", from_state=from_state, to_state=to_state,
        details=details, entity_type=entity_type,
    )


def _actions(review):
    return {item["name"]: item for item in review["actions"]}


def _review(client, change_id):
    response = client.get(f"/v1/changes/{change_id}/review", headers=VIEWER)
    assert response.status_code == 200, response.text
    return response.json()


def _verified_change(main_module, client, tmp_path):
    main_module.fixer.llm = ScriptedLLM(edit(CONFIG_PATH, FIXED), done("Lowered latency_ms"))
    main_module.agent_runtime.sandbox_provider = TempRepoProvider(make_repo(tmp_path / "project"))
    change_id = scenario_incident(client)
    assert client.post(f"/v1/changes/{change_id}/investigate", headers=APPROVER).status_code == 202
    assert wait_for_run(client, change_id)["agent_run_status"] == "ready_for_approval"
    return change_id


# --- timeline ------------------------------------------------------------------------------


def test_the_timeline_is_in_plain_language_and_folds_runs_of_tool_calls_into_one_line():
    entries = [
        _entry("pipeline", "change.created", None, "awaiting_dev_execution"),
        _entry("agent:orchestrator", "agent.started", "awaiting_dev_execution", "investigating"),
        _entry("agent:investigator", "tool.metrics.query", entity_type="incident", details={"ok": True}),
        _entry("agent:investigator", "tool.metrics.query", entity_type="incident", details={"ok": True}),
        _entry("agent:investigator", "tool.logs.search", entity_type="incident", details={"ok": False}),
        _entry("agent:investigator", "tool.deploys.recent.denied", entity_type="incident"),
        _entry("agent:investigator", "agent.investigated", "investigating", "hypothesis_ready",
               {"note": "performance_degradation, confidence 0.90"}),
        _entry("approver-user", "decision.approved", "ready_for_prod", "ready_for_prod", {"comment": "diff looks right"}),
        _entry("someone", "something.new", "a", "a"),
    ]

    timeline = build_timeline(entries)

    assert [item["label"] for item in timeline] == [
        "Incident opened", "Agent run started", "used 4 tool calls", "Diagnosis reached", "Approved", "something.new",
    ]
    assert [item["actor_kind"] for item in timeline] == ["system", "agent", "agent", "agent", "human", "human"]
    assert timeline[0]["actor_name"] == "Intake" and timeline[3]["actor_name"] == "Investigator"
    tools = timeline[2]
    assert tools["kind"] == "tools" and tools["note"] == "metrics.query x2, logs.search, deploys.recent"
    assert tools["problem"] == "1 denied"
    assert timeline[3]["note"] == "performance_degradation, confidence 0.90" and timeline[3]["kind"] == "state"
    assert timeline[4]["note"] == "diff looks right" and timeline[4]["kind"] == "event"


def test_tool_calls_by_different_agents_are_not_folded_together():
    entries = [
        _entry("agent:investigator", "tool.metrics.query", entity_type="incident"),
        _entry("agent:verifier", "tool.metrics.query", entity_type="incident"),
    ]

    assert [(item["actor_name"], item["label"]) for item in build_timeline(entries)] == [
        ("Investigator", "used 1 tool call"), ("Verifier", "used 1 tool call"),
    ]


# --- the stored diff -----------------------------------------------------------------------


def test_a_long_diff_is_cut_for_storage_but_its_hash_covers_all_of_it():
    text = "+x\n" * (MAX_STORED_DIFF_CHARS)

    kept = stored_diff(text)

    assert kept["truncated"] and len(kept["text"]) == MAX_STORED_DIFF_CHARS
    assert kept["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert stored_diff("+small\n") == {"text": "+small\n", "truncated": False, "sha256": hashlib.sha256(b"+small\n").hexdigest()}


# --- overview and identity -----------------------------------------------------------------


def test_the_overview_is_small_newest_first_and_changes_version_when_the_change_does(make_app):
    _, client = make_app()
    first = scenario_incident(client)
    second = scenario_incident(client, "dependency_timeouts")

    rows = client.get("/v1/changes/overview", headers=VIEWER).json()

    assert [row["change_id"] for row in rows] == [second, first]
    assert all(set(row) == OVERVIEW_KEYS for row in rows)
    before = rows[1]["version"]
    assert client.post(f"/v1/changes/{first}/decision", headers=APPROVER, json={"decision": "reject"}).status_code == 200
    after = {row["change_id"]: row for row in client.get("/v1/changes/overview", headers=VIEWER).json()}[first]
    assert after["version"] > before and after["status"] == "rejected"
    assert client.get("/v1/changes/overview?limit=1", headers=VIEWER).json()[0]["change_id"] == second
    assert client.get("/v1/changes/overview").status_code == 401


def test_whoami_tells_the_screen_who_each_key_belongs_to(make_app):
    _, client = make_app()

    assert client.get("/v1/whoami", headers=APPROVER).json() == {"actor": "approver-user", "role": "approver"}
    assert client.get("/v1/whoami", headers=RELEASE).json()["role"] == "release_operator"
    assert client.get("/v1/whoami", headers=VIEWER).json()["role"] == "viewer"
    assert client.get("/v1/whoami").status_code == 401


# --- the review of a change nobody has looked at -------------------------------------------


def test_a_change_the_agents_have_not_seen_says_why_each_action_is_closed(make_app):
    _, client = make_app()
    change_id = scenario_incident(client)

    review = _review(client, change_id)

    assert review["headline"] == "Not investigated yet" and review["attention"] is None
    assert review["diagnosis"] is None and review["fix"] is None and review["verification"] is None
    actions = _actions(review)
    assert not actions["run_agents"]["allowed"] and "AGENT_RUN_MODE=off" in actions["run_agents"]["reason"]
    assert not actions["approve"]["allowed"] and "not ready for approval" in actions["approve"]["reason"]
    assert not actions["promote"]["allowed"] and "must be approved" in actions["promote"]["reason"]
    assert not actions["refresh_status"]["allowed"]
    assert [item["label"] for item in review["timeline"]] == ["Incident opened"]
    assert client.get("/v1/changes/CHG-NOPE/review", headers=VIEWER).status_code == 404
    assert client.get(f"/v1/changes/{change_id}/review").status_code == 401


def test_when_agents_are_allowed_to_run_the_screen_offers_to_start_them(make_app):
    _, client = make_app(AGENT_RUN_MODE="manual")
    change_id = scenario_incident(client)

    assert _actions(_review(client, change_id))["run_agents"]["allowed"]


def test_a_change_the_agents_gave_up_on_is_shown_as_handed_over_and_can_be_rerun(make_app):
    _, client = make_app(AGENT_RUN_MODE="manual")
    change_id = scenario_incident(client)
    client.post(f"/v1/changes/{change_id}/investigate", headers=APPROVER)
    wait_for_run(client, change_id)

    review = _review(client, change_id)

    assert review["attention"] == "escalated" and "handed it to a person" in review["headline"]
    assert "no model is configured for the Fixer" in review["agent_run"]["escalation_reason"]
    assert review["diagnosis"]["issue_type"] == "performance_degradation" and review["fix"] is None
    assert review["evidence"] and all({"id", "kind", "summary", "supports_diagnosis"} <= set(item) for item in review["evidence"])
    assert any(item["supports_diagnosis"] for item in review["evidence"])
    assert _actions(review)["run_agents"]["allowed"]
    assert not _actions(review)["approve"]["allowed"]


# --- a verified fix, from the screen's point of view ---------------------------------------


def test_a_verified_fix_shows_the_diff_the_checks_and_the_critic(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual")
    change_id = _verified_change(main_module, client, tmp_path)

    review = _review(client, change_id)

    assert review["attention"] == "needs_approval" and review["headline"] == "A verified fix is waiting for your approval"
    fix = review["fix"]
    assert fix["files"] == [CONFIG_PATH] and fix["attempts"] == 1 and fix["branch"].startswith("agent-fix/")
    assert CONFIG_PATH in fix["diff"]["text"] and '+  "latency_ms": 120' in fix["diff"]["text"]
    assert not fix["diff"]["truncated"]
    assert fix["diff"]["sha256"] == hashlib.sha256(fix["diff"]["text"].encode()).hexdigest()
    verification = review["verification"]
    assert verification["verdict"] == "passed" and {check["status"] for check in verification["checks"]} == {"passed"}
    assert verification["alert_metric"]["after"] < verification["alert_metric"]["before"]
    assert review["critic"]["decision"] == "approve_for_human" and review["critic"]["risk"] == "low"
    assert review["policy"] == {"passes": True, "reasons": []}
    labels = [item["label"] for item in review["timeline"]]
    assert labels.index("Diagnosis reached") < labels.index("Verification finished") < labels.index("Verified fix ready for approval")
    verified = next(item for item in review["timeline"] if item["label"] == "Verification finished")
    assert verified["note"] == "attempt 1: passed" and verified["actor_name"] == "Verifier"
    assert any(item["kind"] == "tools" and item["actor_name"] == "Investigator" for item in review["timeline"])


def test_the_full_flow_names_who_must_promote_and_closes_every_action_at_the_end(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual", ENFORCE_SEPARATION_OF_DUTIES="true")
    change_id = _verified_change(main_module, client, tmp_path)
    actions = _actions(_review(client, change_id))
    assert actions["approve"]["allowed"] and actions["reject"]["allowed"] and not actions["promote"]["allowed"]

    client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve", "comment": "diff reviewed"})
    approved = _review(client, change_id)

    assert approved["attention"] == "needs_promotion"
    assert approved["headline"] == "Approved by approver-user; waiting for a different release operator to promote it"
    assert approved["approval"]["enforce_separation_of_duties"] and approved["approval"]["decided_by"] == "approver-user"
    promote = _actions(approved)["promote"]
    assert promote["allowed"] and "someone other than approver-user" in promote["note"]
    assert not _actions(approved)["approve"]["allowed"]

    assert client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={}).status_code == 200
    done_review = _review(client, change_id)

    assert done_review["headline"] == "Promoted to production" and done_review["terminal"]
    assert done_review["approval"]["promoted_by"] == "release-operator-user"
    assert not any(item["allowed"] for item in done_review["actions"])
    assert [item["label"] for item in done_review["timeline"]][-3:] == ["Approved", "Promotion started", "Promoted to production"]


# --- the screen never offers what the server would refuse, nor hides what it would accept ---


def _try(client, change_id, name):
    request = {
        "run_agents": lambda: client.post(f"/v1/changes/{change_id}/investigate", headers=APPROVER),
        "approve": lambda: client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "approve"}),
        "reject": lambda: client.post(f"/v1/changes/{change_id}/decision", headers=APPROVER, json={"decision": "reject"}),
        "promote": lambda: client.post(f"/v1/changes/{change_id}/promote", headers=RELEASE, json={}),
        "refresh_status": lambda: client.post(f"/v1/changes/{change_id}/refresh-status", headers=APPROVER),
    }[name]
    return request().status_code


def _check_agreement(client, change_id, open_actions, then):
    """The screen offers exactly `open_actions`; the server refuses every other one, and accepts `then`."""
    actions = _actions(_review(client, change_id))
    assert {name for name, action in actions.items() if action["allowed"]} == open_actions
    for name, action in actions.items():
        if not action["allowed"]:
            assert _try(client, change_id, name) in {400, 409}, f"{name} was closed but the server did not refuse it"
    assert actions[then]["allowed"], f"{then} should have been open: {actions[then]}"
    assert _try(client, change_id, then) in {200, 202}, f"{then} was open but the server refused it"


def test_the_actions_on_screen_agree_with_what_the_server_accepts_at_every_stage(make_app, tmp_path):
    main_module, client = make_app(AGENT_RUN_MODE="manual", ENFORCE_SEPARATION_OF_DUTIES="true")
    main_module.fixer.llm = ScriptedLLM(edit(CONFIG_PATH, FIXED), done("Lowered latency_ms"))
    main_module.agent_runtime.sandbox_provider = TempRepoProvider(make_repo(tmp_path / "project"))
    change_id = scenario_incident(client)

    _check_agreement(client, change_id, {"run_agents", "reject"}, then="run_agents")  # not investigated yet
    wait_for_run(client, change_id)
    _check_agreement(client, change_id, {"approve", "reject"}, then="approve")  # verified: promote and a rerun are closed
    _check_agreement(client, change_id, {"promote"}, then="promote")  # approved: a second decision is closed
    assert _review(client, change_id)["headline"] == "Promoted to production"
