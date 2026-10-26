import statistics
from datetime import timedelta

import pytest

from app.tools.base import TimeWindow, ToolError
from app.tools.dev_environment import EFFECTS, MockDevEnvironment
from app.tools.scenarios import ANCHOR, BAD_CONFIG_LATENCY

SERVICE = BAD_CONFIG_LATENCY.service
ALERT = BAD_CONFIG_LATENCY.alert_metric  # latency_p95_seconds: baseline 0.25, peak 2.2
OTHER = "error_rate_per_sec"  # baseline 0.01, peak 0.31
WINDOW = TimeWindow(start=ANCHOR, end=ANCHOR + timedelta(minutes=10))


def _level(env, metric):
    return statistics.median(p.value for p in env.metrics.query(SERVICE, metric, WINDOW).points)


def _deploy(env, ref="agent/x"):
    started = env.deploy_target.deploy_dev(SERVICE, ref)
    for _ in range(5):
        status = env.deploy_target.get_status(started.url).status
        if status not in {"QUEUED", "RUNNING"}:
            return status
    raise AssertionError("deploy never finished")


def test_before_any_deploy_the_problem_is_still_there():
    env = MockDevEnvironment()

    assert _level(env, ALERT) == pytest.approx(2.2, rel=0.1)
    assert _level(env, OTHER) == pytest.approx(0.31, rel=0.1)


@pytest.mark.parametrize(
    "effect,alert_level,other_level",
    [
        ("resolves", 0.25, 0.01),
        ("no_effect", 2.2, 0.31),
        ("partial", 0.25 + 0.6 * (2.2 - 0.25), 0.01),
        ("regresses_other", 0.25, 0.31),
    ],
)
def test_a_deployed_fix_changes_the_metrics_according_to_its_declared_effect(effect, alert_level, other_level):
    env = MockDevEnvironment()
    env.register_fix("agent/x", effect)

    assert _deploy(env) == "SUCCESS"

    assert _level(env, ALERT) == pytest.approx(alert_level, rel=0.1)
    assert _level(env, OTHER) == pytest.approx(other_level, rel=0.1)


def test_a_failed_deploy_leaves_the_environment_unchanged():
    env = MockDevEnvironment()
    env.register_fix("agent/bad", "deploy_fails")

    assert _deploy(env, "agent/bad") == "FAILURE"

    assert env.deployed(SERVICE) is None and _level(env, ALERT) == pytest.approx(2.2, rel=0.1)


def test_the_last_successful_deploy_wins():
    env = MockDevEnvironment()
    env.register_fix("agent/good", "resolves")
    env.register_fix("agent/bad", "no_effect")

    _deploy(env, "agent/good")
    assert _level(env, ALERT) < 0.5
    _deploy(env, "agent/bad")
    assert _level(env, ALERT) > 2.0


def test_unregistered_branches_get_the_default_effect_and_reset_clears_state():
    env = MockDevEnvironment(default_effect="no_effect")
    _deploy(env, "agent/unknown")
    assert env.deployed(SERVICE) == "no_effect"

    env.reset()
    assert env.deployed(SERVICE) is None


def test_deploys_report_running_before_they_finish():
    env = MockDevEnvironment(polls_until_done=2)
    started = env.deploy_target.deploy_dev(SERVICE, "agent/x")

    assert started.status == "QUEUED"
    assert [env.deploy_target.get_status(started.url).status for _ in range(3)] == ["RUNNING", "RUNNING", "SUCCESS"]


def test_unknown_inputs_are_tool_errors_not_silent_successes():
    env = MockDevEnvironment()

    with pytest.raises(ToolError, match="unknown dev deploy"):
        env.deploy_target.get_status("mock://dev/nope/1")
    with pytest.raises(ToolError, match="no dev metric"):
        env.metrics.query("no-such-service", ALERT, WINDOW)
    with pytest.raises(ToolError, match="no dev metric"):
        env.metrics.query(SERVICE, "no_such_metric", WINDOW)
    with pytest.raises(ToolError, match="not available"):
        env.deploy_target.canary(SERVICE, "ref", 10)
    with pytest.raises(ToolError, match="not available"):
        env.deploy_target.rollback(SERVICE, "ref")


def test_effects_are_validated():
    with pytest.raises(ValueError, match="unknown dev effect"):
        MockDevEnvironment(default_effect="explodes")
    with pytest.raises(ValueError, match="unknown dev effect"):
        MockDevEnvironment().register_fix("b", "explodes")
    assert set(EFFECTS) == {"resolves", "no_effect", "partial", "regresses_other", "deploy_fails"}


def test_the_series_is_deterministic_and_lists_the_services_metrics():
    env = MockDevEnvironment()

    assert env.metrics.query(SERVICE, ALERT, WINDOW) == env.metrics.query(SERVICE, ALERT, WINDOW)
    assert env.metrics.available_metrics(SERVICE) == [OTHER, ALERT]
    assert env.metrics.available_metrics("unknown") == []
