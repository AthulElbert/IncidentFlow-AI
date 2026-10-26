"""Logs, deploy history and incident memory."""
import json
from datetime import datetime, timedelta, timezone

from app.models.schemas import ApprovalDecisionRequest, IncidentRecord, PromoteRequest
from app.services.change_control import ChangeControlStore
from app.services.classifier import classify_issue
from app.models.schemas import APMEvent
from app.tools.base import TimeWindow
from app.tools.deploys import ChangeStoreDeployHistory, MockDeployHistoryTool
from app.tools.logs import FileLogsTool, MockLogsTool
from app.tools.memory import LexicalIncidentMemory
from app.tools.scenarios import ANCHOR, DEFAULT_SCENARIOS

WINDOW = TimeWindow.last(60, end=ANCHOR)


# --- logs -------------------------------------------------------------------------------


def test_logs_are_filtered_by_service_window_and_every_query_word():
    tool = MockLogsTool()

    errors = tool.search("orders-service", "NullPointerException", WINDOW)
    assert len(errors) == 3
    assert [line.timestamp for line in errors] == sorted(line.timestamp for line in errors)

    both_words = tool.search("orders-service", "nullpointerexception promo", WINDOW)
    assert len(both_words) == 3
    assert tool.search("orders-service", "nullpointerexception database", WINDOW) == []
    assert tool.search("checkout-service", "NullPointerException", WINDOW) == []


def test_log_window_and_limit_are_respected():
    tool = MockLogsTool()

    recent = tool.search("orders-service", "", TimeWindow.last(10, end=ANCHOR))
    assert all(line.timestamp >= ANCHOR - timedelta(minutes=10) for line in recent)

    latest_two = tool.search("orders-service", "", WINDOW, limit=2)
    assert len(latest_two) == 2
    assert latest_two[-1].message.startswith("error budget burn rate")  # the most recent line is kept


def test_unknown_service_has_no_logs():
    assert MockLogsTool().search("billing-service", "", WINDOW) == []


def test_file_logs_tool_reads_jsonl_and_skips_bad_lines(tmp_path):
    path = tmp_path / "app.log.jsonl"
    rows = [
        {"timestamp": (ANCHOR - timedelta(minutes=5)).isoformat(), "service": "svc", "level": "ERROR", "message": "disk full"},
        {"timestamp": (ANCHOR - timedelta(minutes=4)).isoformat(), "service": "svc", "level": "INFO", "message": "retrying"},
        {"timestamp": (ANCHOR - timedelta(minutes=3)).isoformat(), "service": "other", "level": "ERROR", "message": "disk full"},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n\n", encoding="utf-8")

    found = FileLogsTool(path).search("svc", "disk", WINDOW)

    assert [line.message for line in found] == ["disk full"]
    assert FileLogsTool(tmp_path / "missing.jsonl").search("svc", "", WINDOW) == []


# --- deploy history ---------------------------------------------------------------------


def test_mock_deploy_history_returns_deploys_inside_the_window():
    tool = MockDeployHistoryTool()

    deploys = tool.recent("orders-service", WINDOW)
    assert [d.ref for d in deploys] == ["2026.07.21-3"]
    assert tool.recent("orders-service", TimeWindow.last(10, end=ANCHOR)) == []
    assert tool.recent("checkout-service", WINDOW) == []  # the no-deploy scenario
    config = tool.recent("faulty-project", WINDOW)[0]
    assert config.kind == "config" and "latency_ms" in config.summary


def test_deploy_history_is_derived_from_promoted_changes(tmp_path):
    path = tmp_path / "changes.json"
    path.write_text("[]", encoding="utf-8")
    store = ChangeControlStore(str(path))

    def make(service):
        return store.create_change(
            incident_id="INC-1", service=service, environment="prod", summary=f"fix {service}", jira_key="SUP-1",
            jenkins_job_url="u", proposed_actions=[], triage_mode_used="heuristic", triage_hypothesis_steps=[],
            issue_type="performance_degradation", confidence=0.9, warning_count=0, jenkins_status="QUEUED",
        )

    promoted, pending = make("payments-service"), make("payments-service")
    store.record_dev_execution(promoted.change_id, "dev", "u", "SUCCESS", 10.0, True, True, "n")
    store.apply_decision(promoted.change_id, ApprovalDecisionRequest(decision="approve"), "approver")
    store.begin_promotion(promoted.change_id, PromoteRequest(), "releaser")
    store.finalize_promotion(promoted.change_id, "success", "https://jenkins/job/x/1/")

    deploys = ChangeStoreDeployHistory(store).recent("payments-service", TimeWindow.last(5))

    assert [d.change_id for d in deploys] == [promoted.change_id]
    assert deploys[0].deployed_by == "releaser"
    assert pending.change_id not in [d.change_id for d in deploys]
    assert ChangeStoreDeployHistory(store).recent("orders-service", TimeWindow.last(5)) == []


# --- incident memory --------------------------------------------------------------------


def _record(incident_id, service, metric, issue_type, summary, resolution, actions=()):
    return IncidentRecord(
        incident_id=incident_id, service=service, metric=metric, issue_type=issue_type, summary=summary,
        resolution=resolution, runbook_actions=list(actions), created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )


HISTORY = [
    _record("INC-A", "orders-service", "error_rate_pct", "application_error",
            "orders-service 5xx spike after deploy",
            "Rolled back the faulty deploy and enabled the safe-mode feature flag for the checkout module.",
            ["Inspect recent deployment diff and rollback candidate changes."]),
    _record("INC-B", "checkout-service", "dependency_timeout_ms", "dependency_failure",
            "inventory-db connection pool exhausted",
            "Raised the connection pool size and adjusted upstream timeout; dependency stabilised."),
    _record("INC-C", "payments-service", "latency_p99_ms", "performance_degradation",
            "payments latency regression", "Enabled response caching."),
]


def _memory():
    return LexicalIncidentMemory(lambda: HISTORY)


def test_memory_ranks_the_relevant_incident_first_and_says_why():
    results = _memory().search("connection pool exhausted timeout contacting inventory-db", service="checkout-service")

    assert results[0].incident_id == "INC-B"
    assert results[0].score > 0.5
    assert any("same service" in item for item in results[0].evidence)
    matched = next(item for item in results[0].evidence if item.startswith("matched terms"))
    assert "pool" in matched or "inventory" in matched


def test_same_service_outranks_an_equally_similar_other_service():
    records = [
        _record("INC-1", "svc-a", "m", "application_error", "checkout failure", "restart"),
        _record("INC-2", "svc-b", "m", "application_error", "checkout failure", "restart"),
    ]
    results = LexicalIncidentMemory(lambda: records).search("checkout failure", service="svc-b")

    assert [item.incident_id for item in results] == ["INC-2", "INC-1"]


def test_unrelated_text_returns_nothing_and_k_limits_results():
    memory = _memory()

    assert memory.search("quantum entanglement of photons") == []
    assert LexicalIncidentMemory(lambda: []).search("anything") == []
    assert memory.search("") == []
    assert len(memory.search("error latency deploy pool", k=1)) == 1


def test_memory_results_carry_the_resolution_and_runbook():
    result = _memory().search("deploy rollback feature flag error", service="orders-service")[0]

    assert result.incident_id == "INC-A"
    assert "Rolled back" in result.resolution
    assert result.runbook_actions == ["Inspect recent deployment diff and rollback candidate changes."]


def test_memory_reads_current_records_each_time():
    records: list[IncidentRecord] = []
    memory = LexicalIncidentMemory(lambda: records)
    assert memory.search("disk full on host") == []

    records.append(_record("INC-N", "svc", "disk_pct", "resource_saturation", "disk full on host", "Expanded the volume."))
    assert memory.search("disk full on host")[0].incident_id == "INC-N"


def test_baseline_keyword_classifier_gets_one_scenario_wrong():
    """Documents the weakness Phase 3 is meant to fix: 'latency' in the message wins over the real cause."""
    got = {}
    for scenario in DEFAULT_SCENARIOS:
        event = APMEvent(
            service=scenario.service, metric=scenario.alert_metric, value=1, threshold=1,
            environment=scenario.environment, timestamp=ANCHOR, message=scenario.alert_message,
        )
        got[scenario.name] = classify_issue(event)[0] == scenario.expected_issue_type

    assert got == {"bad_deploy_error_spike": True, "dependency_timeouts": False, "bad_config_latency": True}
