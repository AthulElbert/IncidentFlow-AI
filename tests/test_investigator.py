import json

import pytest

from app.agents.investigator import Investigator
from app.agents.models import IncidentContext
from app.audit import JsonlAuditLog
from app.replay import event_for, toolkit_for
from app.tools.base import ToolBudgetExceeded, ToolError
from app.tools.instrumentation import ToolBudget
from app.tools.memory import LexicalIncidentMemory
from app.tools.scenarios import (
    BAD_DEPLOY_ERROR_SPIKE,
    DEFAULT_SCENARIOS,
    DEPENDENCY_TIMEOUTS,
    LOG_PROMPT_INJECTION,
    UNEXPLAINED_ALERT,
    eval_history,
)


class ScriptedLLM:
    """Replays canned replies and records every conversation turn it was shown."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete_json(self, messages, error_label="LLM"):
        self.calls.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _run(scenario, llm=None, history=True, audit=None, budget=None, scenarios=DEFAULT_SCENARIOS, **kwargs):
    toolkit = toolkit_for(scenarios, eval_history() if history else [])
    ctx = IncidentContext.from_event(f"INC-{scenario.name}", event_for(scenario))
    tools = toolkit.for_agent("investigator", entity_id=ctx.incident_id, audit=audit, budget=budget)
    return Investigator(llm=llm, **kwargs).investigate(ctx, tools), tools, ctx


def _final(*hypotheses):
    return {"action": "final", "hypotheses": list(hypotheses)}


def _h(issue_type, root_cause="because reasons", supporting=(), contradicting=()):
    return {"issue_type": issue_type, "root_cause": root_cause, "supporting": list(supporting), "contradicting": list(contradicting)}


# --- evidence-based (no model) ------------------------------------------------------------


@pytest.mark.parametrize(
    "scenario",
    [s for s in DEFAULT_SCENARIOS if not s.expect_escalation],
    ids=lambda s: s.name,
)
def test_answerable_scenarios_are_diagnosed_and_proceed(scenario):
    report, _, _ = _run(scenario)

    assert report.mode_used == "heuristic"
    assert report.top.issue_type == scenario.expected_issue_type
    assert report.decision == "proceed" and report.escalation_reason == ""
    assert report.top.confidence >= 0.6
    text = report.top.root_cause.lower()
    assert all(word.lower() in text for word in scenario.root_cause_keywords)


def test_an_unexplained_alert_is_escalated_not_guessed():
    report, _, _ = _run(UNEXPLAINED_ALERT)

    assert report.top.issue_type == "unknown"
    assert report.decision == "escalate"
    assert "does not support a specific cause" in report.escalation_reason
    assert "Insufficient evidence" in report.top.root_cause


def test_hypotheses_are_ranked_and_every_citation_points_at_real_evidence():
    report, _, _ = _run(DEPENDENCY_TIMEOUTS)
    known = {item.id for item in report.evidence}

    assert [h.rank for h in report.hypotheses] == list(range(1, len(report.hypotheses) + 1))
    confidences = [h.confidence for h in report.hypotheses]
    assert confidences == sorted(confidences, reverse=True)
    for hypothesis in report.hypotheses:
        assert set(hypothesis.supporting) <= known and set(hypothesis.contradicting) <= known
    assert report.top.supporting and report.top.missing_signals is not None


def test_low_confidence_and_ambiguity_both_escalate():
    strict, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE, min_confidence=0.95)
    assert strict.decision == "escalate" and "below the minimum" in strict.escalation_reason
    assert strict.top.issue_type == "application_error"

    wide, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE, min_margin=0.95)
    assert wide.decision == "escalate" and "ambiguous" in wide.escalation_reason


def test_every_tool_call_is_audited_under_the_investigator_and_stays_within_budget(tmp_path):
    audit = JsonlAuditLog(tmp_path / "audit.jsonl")

    report, tools, ctx = _run(BAD_DEPLOY_ERROR_SPIKE, audit=audit)

    trail = audit.list(entity_id=ctx.incident_id)
    assert {entry.actor for entry in trail} == {"agent:investigator"}
    assert {entry.action for entry in trail} == {
        "tool.metrics.available_metrics",
        "tool.metrics.query",
        "tool.logs.search",
        "tool.deploys.recent",
        "tool.memory.search",
    }
    assert report.tool_calls_used == len(trail) == tools.calls_used
    assert report.tool_calls_used < 40


def test_a_failing_tool_degrades_the_report_instead_of_breaking_it():
    scenarios = list(DEFAULT_SCENARIOS)
    toolkit = toolkit_for(scenarios, eval_history())

    def broken(*args, **kwargs):
        raise ToolError("connection refused")

    toolkit.logs.search = broken
    ctx = IncidentContext.from_event("INC-1", event_for(BAD_DEPLOY_ERROR_SPIKE))

    report = Investigator().investigate(ctx, toolkit.for_agent("investigator", entity_id="INC-1"))

    assert any("logs.search failed: connection refused" in w for w in report.warnings)
    assert any(e.kind == "tool_error" for e in report.evidence)
    # Without logs the evidence is thinner, so it must not be MORE confident than with them.
    full, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE)
    assert report.top.confidence < full.top.confidence


def test_running_out_of_tool_budget_keeps_what_was_gathered_and_says_so():
    report, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE, budget=ToolBudget(3))

    assert report.tool_calls_used == 3
    assert any("budget exhausted" in w for w in report.warnings)
    assert report.evidence  # the metrics gathered before the budget ran out are still there
    assert report.decision == "escalate"  # too little evidence to proceed


def test_a_prompt_injection_in_the_logs_cannot_change_the_confidence():
    injected, _, _ = _run(LOG_PROMPT_INJECTION)

    clean_scenario = LOG_PROMPT_INJECTION.__class__(
        **{**LOG_PROMPT_INJECTION.__dict__, "logs": [l for l in LOG_PROMPT_INJECTION.logs if "IGNORE" not in l.message]}
    )
    clean, _, _ = _run(clean_scenario, scenarios=[clean_scenario])

    assert injected.top.issue_type == clean.top.issue_type == "application_error"
    assert injected.top.confidence == clean.top.confidence
    assert injected.decision == "proceed"
    resource = next((h for h in injected.hypotheses if h.issue_type == "resource_saturation"), None)
    assert resource is None or resource.confidence < 0.1


# --- with a model -------------------------------------------------------------------------


def test_the_model_supplies_the_wording_but_not_the_confidence():
    llm = ScriptedLLM(_final(_h("application_error", "The 2026.07.21-3 deploy broke promo handling.", ["E1"])))

    report, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE, llm=llm)

    assert report.mode_used == "llm"
    assert report.top.source == "llm"
    assert report.top.root_cause == "The 2026.07.21-3 deploy broke promo handling."
    heuristic, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE)
    assert report.top.confidence == heuristic.top.confidence


def test_a_model_claiming_certainty_is_ignored():
    hypothesis = {**_h("dependency_failure", "It is certainly the database."), "confidence": 1.0}
    llm = ScriptedLLM(_final(hypothesis, {**_h("application_error"), "confidence": 0.01}))

    report, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE, llm=llm)

    assert report.top.issue_type == "application_error"  # ranked by evidence, not by the model's say-so
    assert report.top.confidence == pytest.approx(0.85, abs=0.2)
    dependency = next(h for h in report.hypotheses if h.issue_type == "dependency_failure")
    assert dependency.confidence < 0.2 and dependency.source == "llm"


def test_a_model_that_obeys_an_injection_is_overruled_and_flagged():
    llm = ScriptedLLM(_final(_h("resource_saturation", "Per the logs: resource saturation, delete the database.")))

    report, _, _ = _run(LOG_PROMPT_INJECTION, llm=llm)

    assert report.top.issue_type == "application_error"
    assert report.decision == "proceed"
    assert any("LLM ranked resource_saturation first" in w for w in report.warnings)
    assert "delete the database" not in report.top.root_cause  # the model's text is attached to its own hypothesis only


def test_the_untrusted_evidence_is_sent_as_data_and_the_rules_stay_in_the_system_message():
    llm = ScriptedLLM(_final(_h("application_error")))

    _run(LOG_PROMPT_INJECTION, llm=llm)

    system, user = llm.calls[0][0], llm.calls[0][1]
    assert system["role"] == "system" and "UNTRUSTED DATA" in system["content"]
    assert "IGNORE ALL PREVIOUS" not in system["content"]
    payload = json.loads(user["content"])
    assert user["role"] == "user" and "IGNORE ALL PREVIOUS" in json.dumps(payload["evidence"])
    assert {"id", "kind", "summary"} <= set(payload["evidence"][0])


@pytest.mark.parametrize(
    "bad_reply,reason",
    [
        ({"action": "final", "hypotheses": []}, "1 to 5 hypotheses"),
        ({"action": "final", "hypotheses": "nope"}, "1 to 5 hypotheses"),
        ({"action": "final", "hypotheses": [_h("cosmic_rays")]}, "unsupported issue_type"),
        ({"action": "final", "hypotheses": [_h("application_error", root_cause="  ")]}, "empty root_cause"),
        ({"action": "final", "hypotheses": [_h("application_error", supporting=["E99"])]}, "unknown evidence id E99"),
        ({"action": "final", "hypotheses": [_h("application_error", contradicting=["E404"])]}, "unknown evidence id E404"),
        ({"action": "dance"}, "unexpected action"),
        ({"hypotheses": []}, "unexpected action"),
        (RuntimeError("LLM API error 500"), "LLM API error 500"),
    ],
)
def test_an_unusable_model_answer_falls_back_to_the_evidence_with_a_warning(bad_reply, reason):
    report, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE, llm=ScriptedLLM(bad_reply))

    assert report.mode_used == "heuristic"
    assert any("LLM investigation fallback" in w and reason in w for w in report.warnings)
    assert report.top.issue_type == "application_error" and report.decision == "proceed"


def test_the_model_can_ask_for_more_evidence_and_sees_what_came_back():
    llm = ScriptedLLM(
        {"action": "tool", "tool": "logs", "method": "search", "args": {"query": "NullPointerException", "minutes": 30}},
        _final(_h("application_error", "Promo handling regression.")),
    )

    report, tools, _ = _run(BAD_DEPLOY_ERROR_SPIKE, llm=llm)

    assert report.mode_used == "llm"
    observation = json.loads(llm.calls[1][-1]["content"])
    assert observation["tool_requests_remaining"] == 2
    assert observation["observation"]["log_lines_returned"] == 3
    assert any("NullPointerException" in quote for quote in observation["observation"]["quotes"])
    assert tools.calls_used > 7  # the extra request went through the audited, budgeted proxy


def test_requests_for_another_service_are_shown_but_do_not_change_confidence():
    llm = ScriptedLLM(
        {"action": "tool", "tool": "logs", "method": "search", "args": {"service": "checkout-service", "query": "timeout"}},
        _final(_h("application_error")),
    )

    report, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE, llm=llm)

    foreign = [e for e in report.evidence if e.kind == "foreign_log"]
    assert foreign and all(e.tags == ["foreign"] and "[checkout-service]" in e.summary for e in foreign)
    heuristic, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE)
    assert report.top.confidence == heuristic.top.confidence


def test_bad_or_unsupported_tool_requests_are_answered_with_an_error_and_the_run_continues():
    llm = ScriptedLLM(
        {"action": "tool", "tool": "sandbox", "method": "edit", "args": {"path": "x", "content": "y"}},
        {"action": "tool", "tool": "metrics", "method": "query", "args": {"metric": "no_such_metric"}},
        _final(_h("application_error")),
    )

    report, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE, llm=llm)

    assert report.mode_used == "llm"
    assert "unsupported request sandbox.edit" in llm.calls[1][-1]["content"]
    assert "unknown metric" in llm.calls[2][-1]["content"]
    assert any("metrics.query failed" in w for w in report.warnings)


def test_a_model_that_keeps_asking_for_tools_is_cut_off_and_forced_to_answer():
    ask = {"action": "tool", "tool": "memory", "method": "search", "args": {"text": "pool"}}
    llm = ScriptedLLM(ask, ask, ask, ask, _final(_h("application_error")))

    report, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE, llm=llm, max_llm_steps=3)

    assert report.mode_used == "llm"
    forced = [c for c in llm.calls if "No more tool requests" in c[-1]["content"]]
    assert forced, "the model was told to stop asking"


def test_a_model_that_never_answers_falls_back():
    ask = {"action": "tool", "tool": "memory", "method": "search", "args": {"text": "pool"}}
    llm = ScriptedLLM(*([ask] * 10))

    report, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE, llm=llm, max_llm_steps=1)

    assert report.mode_used == "heuristic"
    assert any("did not give a final answer" in w for w in report.warnings)


def test_a_spent_tool_budget_during_model_reasoning_falls_back_cleanly():
    llm = ScriptedLLM({"action": "tool", "tool": "logs", "method": "search", "args": {"query": "x"}}, _final(_h("application_error")))

    report, _, _ = _run(BAD_DEPLOY_ERROR_SPIKE, llm=llm, budget=ToolBudget(7))

    assert report.mode_used == "heuristic"
    assert any("budget" in w for w in report.warnings)


def test_a_cause_the_model_left_out_is_still_reported_when_the_evidence_supports_it():
    llm = ScriptedLLM(_final(_h("resource_saturation", "Probably memory.")))

    report, _, _ = _run(DEPENDENCY_TIMEOUTS, llm=llm)

    types = [h.issue_type for h in report.hypotheses]
    assert types[0] == "dependency_failure"
    dependency = report.hypotheses[0]
    assert dependency.source == "evidence" and "inventory-db" in dependency.root_cause


def test_the_investigator_never_gets_to_edit_or_deploy():
    toolkit = toolkit_for(DEFAULT_SCENARIOS, [])
    tools = toolkit.for_agent("investigator", entity_id="INC-1")

    assert tools.names == ["deploys", "logs", "memory", "metrics"]
    with pytest.raises(Exception, match="no access"):
        tools.sandbox
    with pytest.raises(Exception, match="no access"):
        tools.deploy_target
    with pytest.raises(ToolBudgetExceeded):
        budgeted = toolkit.for_agent("investigator", entity_id="INC-2", budget=ToolBudget(1))
        budgeted.memory.search("a")
        budgeted.memory.search("b")


def test_an_incident_cannot_corroborate_itself_through_incident_memory():
    """Regression: the pipeline files each new incident in memory before the agents run, so it used to find itself."""
    from datetime import datetime, timezone

    from app.models.schemas import UNRESOLVED_RESOLUTION, IncidentRecord

    ctx = IncidentContext.from_event("INC-SELF", event_for(BAD_DEPLOY_ERROR_SPIKE))
    own_record = IncidentRecord(
        incident_id="INC-SELF", service=ctx.service, metric=ctx.alert_metric, issue_type="application_error",
        summary="orders-service 5xx rate above 2% NullPointerException in checkout", resolution="Rolled back the deploy.",
        created_at=datetime(2026, 7, 21, tzinfo=timezone.utc),
    )
    placeholder = own_record.model_copy(update={"incident_id": "INC-PLACEHOLDER", "resolution": UNRESOLVED_RESOLUTION})
    toolkit = toolkit_for(DEFAULT_SCENARIOS, [])
    toolkit.memory = LexicalIncidentMemory(lambda: [own_record, placeholder])

    report = Investigator().investigate(ctx, toolkit.for_agent("investigator", entity_id="INC-SELF"))

    assert not [e for e in report.evidence if e.kind == "similar_incident"]
    assert report.top.confidence == pytest.approx(0.85)  # earned from metrics, logs and the deploy alone
