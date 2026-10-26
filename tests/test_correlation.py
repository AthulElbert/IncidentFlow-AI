import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.adapters.apm_alert_source import APMAlert, MockAPMAlertSource
from app.adapters.jenkins_client import MockJenkinsClient
from app.adapters.jira_client import MockJiraClient
from app.audit import JsonlAuditLog
from app.models.schemas import APMEvent, ApprovalDecisionRequest
from app.services.change_control import ChangeControlStore
from app.services.correlation import AlertCorrelator
from app.services.pipeline import SupportAgentPipeline
from app.services.scheduler import AlertDedupStore, IncidentScheduler
from app.services.triage_agent import TriageAgent


def _alert(source_id, service="payments-service", metric="latency_p99_ms", environment="prod"):
    return APMAlert(
        source_alert_id=source_id,
        event=APMEvent(
            service=service,
            metric=metric,
            value=1200,
            threshold=800,
            environment=environment,
            timestamp=datetime.now(timezone.utc),
            message="High latency",
        ),
    )


def _store(tmp_path):
    path = tmp_path / "changes.json"
    path.write_text("[]", encoding="utf-8")
    return ChangeControlStore(str(path), audit=JsonlAuditLog(tmp_path / "audit.jsonl"))


def _change(store, service="payments-service", environment="prod", **kwargs):
    return store.create_change(
        incident_id="INC-1",
        service=service,
        environment=environment,
        summary="s",
        jira_key="SUP-1",
        jenkins_job_url="u",
        proposed_actions=[],
        triage_mode_used="heuristic",
        triage_hypothesis_steps=[],
        issue_type="performance_degradation",
        confidence=0.9,
        warning_count=0,
        jenkins_status="QUEUED",
        **kwargs,
    )


def test_related_alert_is_folded_into_the_open_change(tmp_path):
    store = _store(tmp_path)
    change = _change(store)
    correlator = AlertCorrelator(store, window_minutes=15)

    assert correlator.correlate(_alert("a-2", metric="error_rate_pct")) == change.change_id

    updated = store.get_change(change.change_id)
    assert updated.related_alerts == ["a-2"]
    assert updated.alert_count == 2
    assert updated.last_alert_at is not None
    assert len(store.list_changes()) == 1


def test_attaching_is_audited_and_idempotent(tmp_path):
    store = _store(tmp_path)
    change = _change(store)
    store.attach_alert(change.change_id, "a-2", "cpu", actor="scheduler-agent")
    store.attach_alert(change.change_id, "a-2", "cpu", actor="scheduler-agent")

    updated = store.get_change(change.change_id)
    assert updated.alert_count == 2 and updated.related_alerts == ["a-2"]
    actions = [e.action for e in store.audit.list(entity_id=change.change_id)]
    assert actions.count("alert.correlated") == 2  # each attempt is recorded


@pytest.mark.parametrize(
    "service,environment",
    [("orders-service", "prod"), ("payments-service", "staging")],
)
def test_different_service_or_environment_is_not_correlated(tmp_path, service, environment):
    store = _store(tmp_path)
    _change(store)

    assert AlertCorrelator(store, 15).correlate(_alert("a-2", service=service, environment=environment)) is None


def test_alerts_outside_the_window_open_a_new_change(tmp_path):
    store = _store(tmp_path)
    change = _change(store)
    stale = change.model_copy(update={"created_at": datetime.now(timezone.utc) - timedelta(minutes=45)})
    store._records[0] = stale

    assert AlertCorrelator(store, window_minutes=15).correlate(_alert("a-2")) is None
    assert AlertCorrelator(store, window_minutes=60).correlate(_alert("a-3")) == change.change_id


def test_a_window_of_zero_disables_correlation(tmp_path):
    store = _store(tmp_path)
    _change(store)

    assert AlertCorrelator(store, window_minutes=0).correlate(_alert("a-2")) is None


def test_decided_and_deploying_changes_do_not_absorb_new_alerts(tmp_path):
    store = _store(tmp_path)
    rejected = _change(store)
    store.apply_decision(rejected.change_id, ApprovalDecisionRequest(decision="reject"), "alice")

    assert AlertCorrelator(store, 15).correlate(_alert("a-2")) is None


def test_the_most_recently_touched_change_wins(tmp_path):
    store = _store(tmp_path)
    older = _change(store)
    newer = _change(store)
    store._records[0] = older.model_copy(update={"created_at": datetime.now(timezone.utc) - timedelta(minutes=10)})

    assert AlertCorrelator(store, 15).correlate(_alert("a-2")) == newer.change_id


def _scheduler(tmp_path: Path, alerts: list[dict], window: float):
    queue = tmp_path / "queue.json"
    queue.write_text(json.dumps(alerts), encoding="utf-8")
    kb = tmp_path / "kb.json"
    kb.write_text("[]", encoding="utf-8")
    store = _store(tmp_path)
    pipeline = SupportAgentPipeline(
        kb_file=str(kb),
        jira_client=MockJiraClient(project_key="SUP"),
        jenkins_client=MockJenkinsClient(),
        jira_mode="mock",
        jenkins_mode="mock",
        change_store=store,
        triage_agent=TriageAgent(mode="heuristic"),
    )
    scheduler = IncidentScheduler(
        alert_source=MockAPMAlertSource(str(queue)),
        pipeline=pipeline,
        dedup_store=AlertDedupStore(str(tmp_path / "dedup.json")),
        correlator=AlertCorrelator(store, window_minutes=window),
    )
    return scheduler, store


def _raw(source_id, service="payments-service", metric="latency_p99_ms"):
    return {
        "source_alert_id": source_id,
        "service": service,
        "metric": metric,
        "value": 1200,
        "threshold": 800,
        "environment": "prod",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": "High latency",
    }


def test_scheduler_turns_a_burst_of_related_alerts_into_one_change(tmp_path):
    scheduler, store = _scheduler(
        tmp_path,
        [_raw("a-1"), _raw("a-2", metric="error_rate_pct"), _raw("a-3", metric="cpu_pct"), _raw("b-1", service="orders-service")],
        window=15,
    )

    status = scheduler.run_once()

    changes = {c.service: c for c in store.list_changes()}
    assert len(changes) == 2
    assert changes["payments-service"].alert_count == 3
    assert changes["payments-service"].related_alerts == ["a-2", "a-3"]
    assert changes["orders-service"].alert_count == 1
    assert status["stats"]["alerts_processed"] == 2
    assert status["stats"]["alerts_correlated"] == 2

    # Correlated alerts are still deduplicated on the next poll.
    again = scheduler.run_once()
    assert len(store.list_changes()) == 2
    assert again["stats"]["alerts_skipped_dedup"] == 4


def test_scheduler_without_a_window_keeps_one_change_per_alert(tmp_path):
    scheduler, store = _scheduler(tmp_path, [_raw("a-1"), _raw("a-2")], window=0)

    scheduler.run_once()

    assert len(store.list_changes()) == 2
