"""Limits on a whole agent run: time, tool calls and model calls. Exceeding one escalates with the reason."""
import pytest
from agent_harness import CONFIG_PATH, FIXED, build, done, edit

from app.agents.budget import BudgetLimits, RunBudget, RunBudgetExceeded
from app.config import ConfigError, load_settings, validate_settings
from app.tools.base import ToolBudgetExceeded
from app.tools.instrumentation import ChainedBudget, ToolBudget, ToolContext, ToolProxy


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# --- the budget itself ---------------------------------------------------------------------


def test_each_limit_is_reported_in_words_once_it_is_reached():
    clock = Clock()
    budget = RunBudget(BudgetLimits(max_seconds=60, max_tool_calls=2, max_llm_calls=2), clock=clock)
    assert budget.exceeded() is None

    clock.now = 59
    assert budget.exceeded() is None
    clock.now = 60
    assert "60s time limit" in budget.exceeded()

    fresh = RunBudget(BudgetLimits(max_seconds=60, max_tool_calls=2, max_llm_calls=2), clock=clock)
    fresh.tool_calls.spend()
    assert fresh.exceeded() is None
    fresh.tool_calls.spend()
    assert "2 tool calls" in fresh.exceeded()

    models = RunBudget(BudgetLimits(max_llm_calls=1))
    models.spend_llm()
    assert "1 model calls" in models.exceeded()
    with pytest.raises(RunBudgetExceeded):
        models.spend_llm()
    assert models.llm_calls == 1  # a refused call is not counted


def test_usage_reports_what_was_spent_against_what_was_allowed():
    budget = RunBudget(BudgetLimits(max_seconds=30, max_tool_calls=5, max_llm_calls=4), clock=Clock())
    budget.tool_calls.spend()
    budget.spend_llm()

    assert budget.usage() == {
        "seconds": 0.0, "tool_calls": 1, "llm_calls": 1, "limits": {"seconds": 30, "tool_calls": 5, "llm_calls": 4},
    }


def test_an_agents_own_cap_and_the_runs_total_both_apply():
    shared = ToolBudget(3)
    first = ChainedBudget(ToolBudget(2), shared)
    second = ChainedBudget(ToolBudget(5), shared)

    assert first.spend() and first.spend()
    assert not first.spend()  # its own cap of 2
    assert second.spend()
    assert not second.spend()  # the run's total of 3, though this agent has room
    assert first.max_calls == 2 and second.max_calls == 5


def test_a_tool_call_past_the_runs_total_is_refused_and_audited():
    class Echo:
        def ping(self):
            return "pong"

    context = ToolContext(actor="agent:x", entity_id="CHG-1", budget=ChainedBudget(ToolBudget(10), ToolBudget(1)), audit=None)
    proxy = ToolProxy(Echo(), "echo", frozenset({"ping"}), context)

    assert proxy.ping() == "pong"
    with pytest.raises(ToolBudgetExceeded):
        proxy.ping()


# --- inside a real run ---------------------------------------------------------------------


def test_a_run_that_finishes_inside_its_limits_records_what_it_used(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])

    record = h.run()

    used = record.agent_run["budget"]
    assert record.agent_run_status == "ready_for_approval"
    assert used["llm_calls"] == 2 and used["tool_calls"] > 5 and used["limits"]["tool_calls"] == 150


def test_the_time_limit_stops_the_run_before_the_next_stage(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])
    clock = Clock()
    h.runtime._budgets[h.change_id] = RunBudget(BudgetLimits(max_seconds=60), clock=clock)
    original = h.llm.complete_json

    def slow_model(messages, error_label="LLM"):
        clock.now += 1000  # the Fixer's model took far too long
        return original(messages, error_label)

    h.llm.complete_json = slow_model

    record = h.run()

    assert (record.agent_run_status, record.deployment_state) == ("escalated", "escalated")
    assert "run budget exceeded: the run used its 60s time limit" in record.agent_run["escalation_reason"]
    assert record.agent_run["budget"]["seconds"] >= 1000
    assert "verification" not in record.agent_run  # it never got as far as verifying
    assert record.dev_execution_status == "not_started"


def test_the_total_of_tool_calls_stops_a_run_that_would_otherwise_keep_going(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])
    h.runtime.budget_limits = BudgetLimits(max_tool_calls=4)

    record = h.run()

    assert record.agent_run_status == "escalated" and record.deployment_state == "escalated"
    assert record.agent_run["budget"]["tool_calls"] <= 4
    assert record.dev_execution_status == "not_started"


def test_the_model_call_limit_ends_the_fixer_with_a_reason(tmp_path):
    h = build(tmp_path, [{"action": "tool", "tool": "sandbox", "method": "read", "args": {"path": CONFIG_PATH}},
                         edit(CONFIG_PATH, FIXED), done()])
    h.runtime.budget_limits = BudgetLimits(max_llm_calls=1)

    record = h.run()

    assert record.agent_run_status == "escalated"
    assert "the run used its 1 model calls" in record.agent_run["escalation_reason"]
    assert record.agent_run["budget"]["llm_calls"] == 1 and len(h.llm.calls) == 1


def test_a_finished_fix_is_not_discarded_because_packaging_ran_past_the_clock(tmp_path):
    h = build(tmp_path, [edit(CONFIG_PATH, FIXED), done()])
    clock = Clock()
    h.runtime._budgets[h.change_id] = RunBudget(BudgetLimits(max_seconds=60), clock=clock)
    original = h.runtime.critic.review

    def review_then_time_out(*args, **kwargs):
        result = original(*args, **kwargs)
        clock.now = 10_000  # the clock runs out after the Critic approved
        return result

    h.runtime.critic.review = review_then_time_out

    assert h.run().agent_run_status == "ready_for_approval"


# --- settings ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env,message",
    [
        ({"AGENT_RUN_MAX_SECONDS": "0"}, "AGENT_RUN_MAX_SECONDS"),
        ({"AGENT_RUN_MAX_TOOL_CALLS": "0"}, "AGENT_RUN_MAX_TOOL_CALLS"),
        ({"AGENT_RUN_MAX_LLM_CALLS": "0"}, "AGENT_RUN_MAX_LLM_CALLS"),
    ],
)
def test_nonsense_budgets_are_rejected_at_startup(monkeypatch, env, message):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ConfigError, match=message):
        validate_settings(load_settings())


def test_the_defaults_are_generous_but_finite():
    settings = load_settings()

    assert (settings.agent_run_max_seconds, settings.agent_run_max_tool_calls, settings.agent_run_max_llm_calls) == (900.0, 150, 60)
    validate_settings(settings)
