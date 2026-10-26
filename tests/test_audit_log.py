import json
import os
import uuid
from datetime import datetime, timezone

import pytest

from app.audit import PG_AUDIT_SCHEMA, JsonlAuditLog, PostgresAuditLog, new_audit_entry
from pg_fakes import FakeDatabase


def _entry(entity="CHG-1", action="dev.recorded", actor="alice", **kwargs):
    return new_audit_entry(actor=actor, action=action, entity_id=entity, **kwargs)


def test_entries_are_returned_oldest_first_and_filtered_by_entity(tmp_path):
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    log.append(_entry("CHG-1", "change.created", from_state=None, to_state="awaiting_dev_execution"))
    log.append(_entry("CHG-2", "change.created"))
    log.append(_entry("CHG-1", "decision.approved", actor="bob", from_state="ready_for_prod", to_state="ready_for_prod"))

    one = log.list(entity_id="CHG-1")
    assert [e.action for e in one] == ["change.created", "decision.approved"]
    assert [e.actor for e in one] == ["alice", "bob"]
    assert one[0].from_state is None and one[0].to_state == "awaiting_dev_execution"
    assert [e.entity_id for e in log.list()] == ["CHG-1", "CHG-2", "CHG-1"]


def test_limit_keeps_the_most_recent_entries(tmp_path):
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    for i in range(5):
        log.append(_entry(action=f"step.{i}"))

    assert [e.action for e in log.list(limit=2)] == ["step.3", "step.4"]


def test_log_survives_a_restart_and_only_ever_grows(tmp_path):
    path = tmp_path / "audit.jsonl"
    JsonlAuditLog(path).append(_entry(action="first"))
    JsonlAuditLog(path).append(_entry(action="second"))

    reopened = JsonlAuditLog(path)
    assert [e.action for e in reopened.list()] == ["first", "second"]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
    # The interface has no way to change or remove an entry.
    assert not hasattr(reopened, "update") and not hasattr(reopened, "delete")


def test_empty_and_missing_logs_are_empty(tmp_path):
    assert JsonlAuditLog(tmp_path / "missing.jsonl").list() == []


def test_a_corrupt_line_is_skipped_not_fatal(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = JsonlAuditLog(path)
    log.append(_entry(action="good.1"))
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    log.append(_entry(action="good.2"))

    assert [e.action for e in log.list()] == ["good.1", "good.2"]


def test_details_and_timestamps_round_trip(tmp_path):
    log = JsonlAuditLog(tmp_path / "audit.jsonl")
    log.append(_entry(details={"comment": "ship it", "n": 3}))

    entry = log.list()[0]
    assert entry.details == {"comment": "ship it", "n": 3}
    assert entry.occurred_at.tzinfo is not None
    assert entry.audit_id.startswith("AUD-")


def test_postgres_schema_makes_the_table_append_only():
    ddl = " ".join(" ".join(PG_AUDIT_SCHEMA).split())
    assert "BEFORE UPDATE OR DELETE ON audit_events" in ddl
    assert "RAISE EXCEPTION 'audit_events is append-only'" in ddl


def test_postgres_log_inserts_and_reads_oldest_first(monkeypatch):
    db = FakeDatabase()
    monkeypatch.setattr(PostgresAuditLog, "_connect", lambda self: db.connect())
    log = PostgresAuditLog("postgresql://fake")

    entry = _entry(details={"k": "v"}, from_state="a", to_state="b")
    log.append(entry)

    inserts = db.sql("INSERT INTO audit_events")
    assert len(inserts) == 1
    params = inserts[0][1]
    assert params[0] == entry.audit_id and params[2] == "alice" and params[3] == "dev.recorded"
    assert json.loads(params[8]) == {"k": "v"}
    assert db.sql("UPDATE") == [] and db.sql("DELETE") == []

    newer = datetime(2026, 1, 2, tzinfo=timezone.utc)
    older = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # The query returns newest first; the log hands them back oldest first.
    db.results["SELECT audit_id"] = [
        ("AUD-2", newer, "bob", "b", "change", "CHG-1", "x", "y", {}),
        ("AUD-1", older, "alice", "a", "change", "CHG-1", None, "x", {"k": 1}),
    ]
    entries = log.list(entity_id="CHG-1", limit=10)
    assert [e.audit_id for e in entries] == ["AUD-1", "AUD-2"]
    assert entries[0].details == {"k": 1}
    assert "WHERE entity_id = %s" in db.sql("SELECT audit_id")[0][0]


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
def test_real_postgres_rejects_update_and_delete():
    import psycopg

    url = os.environ["TEST_DATABASE_URL"]
    log = PostgresAuditLog(url)
    entry = _entry(entity=f"CHG-{uuid.uuid4().hex[:6]}")
    log.append(entry)
    assert [e.audit_id for e in log.list(entity_id=entry.entity_id)] == [entry.audit_id]

    with psycopg.connect(url) as conn:
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute("UPDATE audit_events SET actor = 'mallory' WHERE audit_id = %s", (entry.audit_id,))
    with psycopg.connect(url) as conn:
        with pytest.raises(psycopg.Error, match="append-only"):
            conn.execute("DELETE FROM audit_events WHERE audit_id = %s", (entry.audit_id,))
