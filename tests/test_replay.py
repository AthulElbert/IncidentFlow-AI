import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.agents.investigator import Investigator
from app.audit import JsonlAuditLog
from app.replay import run_replay
from app.tools.scenarios import DEFAULT_SCENARIOS, MetricShape, Scenario, _deploy, _log

ROOT = Path(__file__).resolve().parents[1]


def _scenario(name, service, alert_metric, message, metrics, logs, deploys, expected, escalate=False):
    return Scenario(
        name=name, service=service, environment="prod", alert_metric=alert_metric, alert_message=message,
        metrics=metrics, logs=logs, deploys=deploys, expected_issue_type=expected, expected_root_cause="",
        expect_escalation=escalate,
    )


# Written after the rules were fixed and never used to tune them: they probe where the logic is weak.
STRESS = [
    _scenario(
        "coincidental_deploy", "billing-service", "dependency_timeout_ms", "ledger-db call latency above 1500ms",
        {"dependency_timeout_ms": MetricShape(200, 3000, 16, "ms"), "error_rate_pct": MetricShape(0.3, 4.0, 14, "percent")},
        [_log(17, "billing-service", "ERROR", "timeout contacting ledger-db after 3000ms"),
         _log(15, "billing-service", "ERROR", "connection pool exhausted for ledger-db (50/50 in use)")],
        [_deploy("billing-service", 26, "2026.07.21-9", "ci-bot", "code", "Update invoice footer text")],
        "dependency_failure",
    ),
    _scenario(
        "regression_without_logs", "reports-api", "error_rate_pct", "5xx rate above 2% on reports-api",
        {"error_rate_pct": MetricShape(0.4, 8.0, 20, "percent"), "cpu_pct": MetricShape(30, 32, 20, "percent")},
        [], [_deploy("reports-api", 24, "2026.07.21-4", "ci-bot", "code", "Rewrite export pipeline")],
        "application_error",
    ),
    _scenario(
        "leak_after_deploy", "media-service", "memory_pct", "memory above 90% on media-service",
        {"memory_pct": MetricShape(50, 97, 35, "percent", ramp_minutes=20), "cpu_pct": MetricShape(40, 45, 35, "percent")},
        [_log(10, "media-service", "ERROR", "java.lang.OutOfMemoryError: Java heap space")],
        [_deploy("media-service", 40, "2026.07.21-2", "ci-bot", "code", "Cache decoded thumbnails in memory")],
        "resource_saturation",
    ),
    _scenario(
        "lonely_latency", "gallery-service", "latency_p95_ms", "latency above 800ms on gallery-service",
        {"latency_p95_ms": MetricShape(200, 1500, 15, "ms"), "cpu_pct": MetricShape(35, 37, 15, "percent")},
        [], [], "performance_degradation",
    ),
    _scenario(
        "flat_everything", "notes-service", "disk_pct", "disk usage warning",
        {"disk_pct": MetricShape(60, 62, 30, "percent")}, [], [], "unknown", escalate=True,
    ),
]


@pytest.fixture(scope="module")
def builtin():
    return run_replay(Investigator())


def test_the_investigator_beats_the_keyword_baseline_on_the_builtin_scenarios(builtin):
    assert builtin.total == len(DEFAULT_SCENARIOS) == 7
    assert builtin.accuracy == 1.0
    assert builtin.baseline_accuracy < builtin.accuracy
    assert {r.scenario for r in builtin.results if not r.baseline_correct} == {"dependency_timeouts", "red_herring_deploys"}


def test_it_never_proceeds_when_it_should_not_have(builtin):
    assert builtin.harmful == 0
    assert builtin.automation_rate == 1.0
    assert builtin.unneeded_escalations == 0
    by_name = {r.scenario: r for r in builtin.results}
    assert by_name["unexplained_alert"].outcome == "escalated_as_expected"


def test_root_causes_name_the_things_that_matter(builtin):
    assert builtin.root_cause_hit_rate == 1.0


def test_confidence_separates_clear_cases_from_the_unclear_one(builtin):
    clear = [r.confidence for r in builtin.results if r.scenario != "unexplained_alert"]
    unclear = next(r.confidence for r in builtin.results if r.scenario == "unexplained_alert")
    assert min(clear) >= 0.6 > unclear


def test_every_scenario_finishes_well_within_the_tool_budget(builtin):
    assert max(r.tool_calls for r in builtin.results) <= 12


def test_results_do_not_depend_on_incident_memory():
    without_memory = run_replay(Investigator(), history=[])

    assert without_memory.accuracy == 1.0 and without_memory.harmful == 0


def test_replay_is_deterministic():
    assert run_replay(Investigator()).to_dict() == run_replay(Investigator()).to_dict()


def test_replay_audits_every_investigators_tool_calls(tmp_path):
    audit = JsonlAuditLog(tmp_path / "audit.jsonl")

    run_replay(Investigator(), scenarios=DEFAULT_SCENARIOS[:2], audit=audit)

    entities = {entry.entity_id for entry in audit.list(limit=1000)}
    assert entities == {f"REPLAY-{s.name}" for s in DEFAULT_SCENARIOS[:2]}


# --- stress scenarios ---------------------------------------------------------------------


@pytest.fixture(scope="module")
def stress():
    return {r.scenario: r for r in run_replay(Investigator(), scenarios=STRESS, history=[]).results}


def test_hard_cases_never_produce_a_confident_wrong_answer(stress):
    assert all(r.outcome not in ("proceeded_wrong", "proceeded_unsafely") for r in stress.values())


def test_a_coincidental_deploy_makes_it_hesitate_rather_than_blame_the_deploy(stress):
    result = stress["coincidental_deploy"]
    assert result.predicted_issue_type == "dependency_failure"  # the real cause is still ranked first
    assert result.decision == "escalate"  # but it will not act on a muddied picture


def test_a_regression_with_no_logs_is_escalated_for_lack_of_corroboration(stress):
    result = stress["regression_without_logs"]
    assert result.predicted_issue_type == "application_error"
    assert result.decision == "escalate" and result.confidence < 0.6


def test_a_single_metric_with_nothing_else_is_not_enough(stress):
    result = stress["lonely_latency"]
    assert result.predicted_issue_type == "unknown" and result.decision == "escalate"


def test_a_leak_that_a_deploy_introduced_is_still_called_resource_saturation(stress):
    assert stress["leak_after_deploy"].outcome == "automated_correctly"


def test_nothing_unusual_means_a_human(stress):
    assert stress["flat_everything"].outcome == "escalated_as_expected"


# --- the script ---------------------------------------------------------------------------


def test_the_replay_script_prints_a_scorecard_and_exits_zero(tmp_path):
    out = tmp_path / "report.json"
    env = {**os.environ, "APP_SKIP_DOTENV": "true"}

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "replay_investigations.py"), "--output", str(out)],
        capture_output=True, text=True, env=env, timeout=120, cwd=str(tmp_path),
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "accuracy 100% (baseline 71%)" in proc.stdout and "harmful 0" in proc.stdout
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["scenarios"] == 7 and report["harmful"] == 0
    assert report["results"][0]["scenario"] == "bad_deploy_error_spike"


def test_the_replay_script_needs_a_key_for_llm_mode(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "LLM_API_KEY"}
    env["APP_SKIP_DOTENV"] = "true"

    proc = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "replay_investigations.py"), "--mode", "llm", "--output", str(tmp_path / "r.json")],
        capture_output=True, text=True, env=env, timeout=120, cwd=str(tmp_path),
    )

    assert proc.returncode == 2 and "LLM_API_KEY" in proc.stderr
