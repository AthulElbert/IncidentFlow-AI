import json

import pytest

from app.audit import JsonlAuditLog
from app.events.bus import INCIDENT_EVENTS_TOPIC
from app.events.inprocess import InProcessBus
from app.models.schemas import ApprovalDecisionRequest, PromoteRequest
from app.services.change_control import ChangeControlStore, ConcurrentModificationError, PolicyConfig
from pg_fakes import FakeDatabase


def _store(tmp_path):
    audit = JsonlAuditLog(tmp_path / "audit.jsonl")
    bus = InProcessBus()
    store_file = tmp_path / "changes.json"
    store_file.write_text("[]", encoding="utf-8")
    return ChangeControlStore(str(store_file), policy=PolicyConfig(), audit=audit, bus=bus), audit, bus


def _create(store, **overrides):
    params = dict(
        incident_id="INC-1",
        service="payments-service",
        environment="prod",
        summary="s",
        jira_key="SUP-1",
        jenkins_job_url="u",
        proposed_actions=[],
        triage_mode_used="heuristic",
        triage_hypothesis_steps=[],
        issue_type="performance_degradation",
        confidence=0.91,
        warning_count=0,
        jenkins_status="QUEUED",
    )
    params.update(overrides)
    return store.create_change(**params)


def _dev_passed(store, change_id):
    return store.record_dev_execution(
        change_id=change_id,
        executed_by="dev-runner",
        execution_url="https://jenkins/job/x/1/",
        jenkins_status="SUCCESS",
        apm_improvement_pct=12.0,
        smoke_tests_passed=True,
        validation_passed=True,
        notes="n",
    )


def test_every_step_of_the_lifecycle_is_audited_with_the_real_actor(tmp_path):
    store, audit, _ = _store(tmp_path)
    change = _create(store)
    _dev_passed(store, change.change_id)
    store.apply_decision(change.change_id, ApprovalDecisionRequest(decision="approve", comment="ok"), "approver-1")
    store.begin_promotion(change.change_id, PromoteRequest(comment="go"), "release-1")
    store.finalize_promotion(change.change_id, "success", "https://jenkins/job/p/9/", actor="release-1")

    trail = audit.list(entity_id=change.change_id)

    assert [(e.action, e.actor) for e in trail] == [
        ("change.created", "pipeline"),
        ("dev.recorded", "dev-runner"),
        ("decision.approved", "approver-1"),
        ("promotion.reserved", "release-1"),
        ("promotion.success", "release-1"),
    ]
    assert [(e.from_state, e.to_state) for e in trail] == [
        (None, "awaiting_dev_execution"),
        ("awaiting_dev_execution", "ready_for_prod"),
        ("ready_for_prod", "ready_for_prod"),
        ("ready_for_prod", "promoting"),
        ("promoting", "promoted_to_prod"),
    ]
    assert trail[2].details == {"comment": "ok"}


def test_each_audited_step_publishes_one_ordered_event_keyed_by_service(tmp_path):
    store, _, bus = _store(tmp_path)
    change = _create(store)
    _dev_passed(store, change.change_id)

    events = bus.poll(INCIDENT_EVENTS_TOPIC, "dashboard")

    assert [e.payload["action"] for e in events] == ["change.created", "dev.recorded"]
    assert {e.key for e in events} == {"payments-service"}
    assert [e.payload["version"] for e in events] == [1, 2]
    assert events[1].payload["from_state"] == "awaiting_dev_execution"
    assert events[1].payload["to_state"] == "ready_for_prod"
    assert events[1].payload["change_id"] == change.change_id


def test_rejected_and_failed_operations_leave_no_audit_entry(tmp_path):
    store, audit, bus = _store(tmp_path)
    change = _create(store)
    before = len(audit.list())

    with pytest.raises(ValueError):
        store.begin_promotion(change.change_id, PromoteRequest(), "release-1")  # not approved
    with pytest.raises(ValueError):
        store.apply_decision("CHG-NOPE", ApprovalDecisionRequest(decision="approve"), "a")

    assert len(audit.list()) == before
    assert len(bus.poll(INCIDENT_EVENTS_TOPIC, "g")) == before


def test_illegal_lifecycle_transition_is_refused_and_not_persisted(tmp_path):
    store, audit, _ = _store(tmp_path)
    change = _create(store)
    before = len(audit.list())

    def skip_the_deploy(record):
        return record.model_copy(update={"deployment_state": "promoted_to_prod"})

    with pytest.raises(ValueError, match="Illegal state transition awaiting_dev_execution -> promoted_to_prod"):
        store._mutate(change.change_id, skip_the_deploy, action="bogus", actor="mallory")

    assert store.get_change(change.change_id).deployment_state == "awaiting_dev_execution"
    assert store.get_change(change.change_id).version == 1
    assert len(audit.list()) == before
    persisted = json.loads((tmp_path / "changes.json").read_text(encoding="utf-8"))
    assert persisted[0]["deployment_state"] == "awaiting_dev_execution"


def test_a_failing_audit_sink_does_not_break_the_state_change(tmp_path):
    store, _, _ = _store(tmp_path)

    class BrokenAudit:
        def append(self, entry):
            raise OSError("disk full")

        def list(self, **kwargs):
            return []

    store.audit = BrokenAudit()
    change = _create(store)

    assert store.get_change(change.change_id) is not None  # JSON backend logs and carries on


def test_store_works_without_audit_or_bus(tmp_path):
    store_file = tmp_path / "changes.json"
    store_file.write_text("[]", encoding="utf-8")
    store = ChangeControlStore(str(store_file))

    change = _create(store)
    assert _dev_passed(store, change.change_id).deployment_state == "ready_for_prod"


# --- Postgres: audit and outbox rows are part of the state-change transaction ---------------


def _pg_store(monkeypatch, db):
    db.results["SELECT payload, version FROM change_records ORDER BY"] = []  # empty table at startup
    monkeypatch.setattr(ChangeControlStore, "_pg_connect", lambda self: db.connect())
    return ChangeControlStore(
        "unused.json", policy=PolicyConfig(), storage_backend="postgres", database_url="postgresql://fake"
    )


def test_postgres_create_writes_record_audit_and_event_in_one_transaction(monkeypatch):
    db = FakeDatabase()
    store = _pg_store(monkeypatch, db)

    change = _create(store)

    ordered = [sql.split(" (")[0] for sql, _ in db.statements if sql.startswith("INSERT")]
    assert ordered == ["INSERT INTO change_records", "INSERT INTO audit_events", "INSERT INTO event_outbox"]
    audit_params = db.sql("INSERT INTO audit_events")[0][1]
    assert audit_params[3] == "change.created" and audit_params[5] == change.change_id


def test_postgres_update_writes_audit_and_event_only_after_the_version_check_passes(monkeypatch):
    db = FakeDatabase()
    store = _pg_store(monkeypatch, db)
    created = _create(store)
    db.results["SELECT payload, version FROM change_records WHERE"] = (created.model_dump(mode="json"), 1)
    audit_before = len(db.sql("INSERT INTO audit_events"))

    store.apply_decision(created.change_id, ApprovalDecisionRequest(decision="reject", comment="no"), "alice")

    assert len(db.sql("INSERT INTO audit_events")) == audit_before + 1
    assert len(db.sql("INSERT INTO event_outbox")) == audit_before + 1
    positions = [i for i, (sql, _) in enumerate(db.statements) if sql.startswith(("UPDATE change_records", "INSERT INTO audit"))]
    assert db.statements[positions[-2]][0].startswith("UPDATE change_records")
    assert db.statements[positions[-1]][0].startswith("INSERT INTO audit_events")


def test_postgres_version_conflict_writes_no_audit_or_event(monkeypatch):
    db = FakeDatabase()
    store = _pg_store(monkeypatch, db)
    created = _create(store)
    db.results["SELECT payload, version FROM change_records WHERE"] = (created.model_dump(mode="json"), 1)
    audit_before = len(db.sql("INSERT INTO audit_events"))
    db.update_rowcounts = [0, 0, 0]

    with pytest.raises(ConcurrentModificationError):
        store.apply_decision(created.change_id, ApprovalDecisionRequest(decision="reject"), "alice")

    assert len(db.sql("INSERT INTO audit_events")) == audit_before
    assert len(db.sql("INSERT INTO event_outbox")) == audit_before
