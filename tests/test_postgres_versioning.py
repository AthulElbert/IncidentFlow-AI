"""Per-record versioned Postgres writes.

The first group runs everywhere against a scripted fake connection and checks the
SQL contract (no table rewrite, version-conditional updates, retry on conflict).
The second group runs against a real database when TEST_DATABASE_URL is set.
"""
import os
import threading
import uuid

import pytest

from app.models.schemas import ApprovalDecisionRequest
from app.services.change_control import ChangeControlStore, ConcurrentModificationError, PolicyConfig


class _FakeCursor:
    def __init__(self, db):
        self.db = db
        self.rowcount = -1
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        normalized = " ".join(sql.split())
        self.db.statements.append((normalized, params))
        self._result = None
        if normalized.startswith("UPDATE change_records"):
            self.rowcount = self.db.update_rowcounts.pop(0) if self.db.update_rowcounts else 1
        elif normalized.startswith("SELECT payload, version FROM change_records WHERE"):
            self._result = self.db.row
        elif normalized.startswith("SELECT payload, version FROM change_records"):
            self._result = self.db.all_rows

    def fetchone(self):
        return self._result

    def fetchall(self):
        return self._result


class _FakeConnection:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self.db)


class _FakeDatabase:
    def __init__(self):
        self.statements: list[tuple[str, object]] = []
        self.update_rowcounts: list[int] = []
        self.row = None
        self.all_rows: list = []

    def sql(self, prefix: str) -> list[tuple[str, object]]:
        return [item for item in self.statements if item[0].startswith(prefix)]


def _pg_store(monkeypatch, db) -> ChangeControlStore:
    monkeypatch.setattr(ChangeControlStore, "_pg_connect", lambda self: _FakeConnection(db))
    return ChangeControlStore(
        "unused.json",
        policy=PolicyConfig(),
        storage_backend="postgres",
        database_url="postgresql://fake",
    )


def _create(store):
    return store.create_change(
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
        confidence=0.9,
        warning_count=0,
        jenkins_status="QUEUED",
    )


def test_create_inserts_one_row_and_never_rewrites_the_table(monkeypatch):
    db = _FakeDatabase()
    store = _pg_store(monkeypatch, db)

    created = _create(store)

    inserts = db.sql("INSERT INTO change_records")
    assert len(inserts) == 1
    assert inserts[0][1][0] == created.change_id
    assert db.sql("DELETE") == []


def test_update_is_conditional_on_the_version_that_was_read(monkeypatch):
    db = _FakeDatabase()
    store = _pg_store(monkeypatch, db)
    created = _create(store)
    db.row = (created.model_dump(mode="json"), created.version)

    store.apply_decision(created.change_id, ApprovalDecisionRequest(decision="reject", comment="no"), "alice")

    updates = db.sql("UPDATE change_records")
    assert len(updates) == 1
    sql, params = updates[0]
    assert "WHERE change_id = %s AND version = %s" in sql
    payload, new_version, change_id, expected_version = params
    assert (change_id, expected_version, new_version) == (created.change_id, 1, 2)
    assert db.sql("DELETE") == []


def test_conflicting_write_is_retried_against_a_fresh_read(monkeypatch):
    db = _FakeDatabase()
    store = _pg_store(monkeypatch, db)
    created = _create(store)
    db.row = (created.model_dump(mode="json"), created.version)
    db.update_rowcounts = [0, 1]  # first UPDATE loses the race, second wins

    updated = store.apply_decision(created.change_id, ApprovalDecisionRequest(decision="reject"), "alice")

    assert updated.status == "rejected"
    assert len(db.sql("UPDATE change_records")) == 2
    assert len(db.sql("SELECT payload, version FROM change_records WHERE")) == 2


def test_persistent_conflict_surfaces_instead_of_overwriting(monkeypatch):
    db = _FakeDatabase()
    store = _pg_store(monkeypatch, db)
    created = _create(store)
    db.row = (created.model_dump(mode="json"), created.version)
    db.update_rowcounts = [0, 0, 0]

    with pytest.raises(ConcurrentModificationError):
        store.apply_decision(created.change_id, ApprovalDecisionRequest(decision="reject"), "alice")
    assert len(db.sql("UPDATE change_records")) == ChangeControlStore._MAX_WRITE_ATTEMPTS


def test_unknown_change_is_not_found(monkeypatch):
    db = _FakeDatabase()
    store = _pg_store(monkeypatch, db)
    db.row = None

    with pytest.raises(ValueError, match="not found"):
        store.apply_decision("CHG-NOPE", ApprovalDecisionRequest(decision="reject"), "alice")


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
def test_real_postgres_concurrent_writers_never_lose_an_update():
    url = os.environ["TEST_DATABASE_URL"]
    first = ChangeControlStore("unused.json", policy=PolicyConfig(), storage_backend="postgres", database_url=url)
    second = ChangeControlStore("unused.json", policy=PolicyConfig(), storage_backend="postgres", database_url=url)

    created = _create(first)
    assert second.get_change(created.change_id).version == 1

    errors: list[Exception] = []
    successes: list[int] = []

    def work(store, count):
        for i in range(count):
            try:
                store.record_pr_preparation(
                    change_id=created.change_id,
                    generated_by=f"writer-{uuid.uuid4().hex[:4]}",
                    pr_status="draft",
                    pr_url="u",
                    pr_branch="b",
                    pr_title="t",
                    pr_summary=str(i),
                    patch_artifact_path="p",
                    patch_preview="p",
                    local_branch_created=False,
                    local_branch_message="m",
                    code_change_status="not_started",
                    code_change_message="m",
                    sandbox_worktree_path="",
                    changed_files=[],
                    commit_sha="",
                    push_status="",
                    test_evidence_status="passed",
                    test_command="c",
                    test_output="o",
                    test_pass_rate=1.0,
                )
                successes.append(i)
            except ConcurrentModificationError:
                pass  # a writer may legitimately give up after its retries
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(exc)

    threads = [threading.Thread(target=work, args=(store, 10)) for store in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    # No lost updates: every write that reported success is exactly one version bump.
    expected = 1 + len(successes)
    assert first.get_change(created.change_id).version == expected
    assert second.get_change(created.change_id).version == expected
