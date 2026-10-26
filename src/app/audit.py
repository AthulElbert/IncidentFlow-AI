"""Append-only audit log.

Every state change records who did what, when, and the state before and after.
There is deliberately no update or delete: the JSONL file is only ever appended
to, and the Postgres table has a trigger that rejects UPDATE and DELETE.
"""
import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field

from app.db import pg_connect

logger = logging.getLogger("app.audit")

PG_AUDIT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS audit_events (
        audit_id TEXT PRIMARY KEY,
        occurred_at TIMESTAMPTZ NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT,
        details JSONB NOT NULL DEFAULT '{}'::jsonb
    )
    """,
    "CREATE INDEX IF NOT EXISTS audit_events_entity_idx ON audit_events (entity_id, occurred_at)",
    """
    CREATE OR REPLACE FUNCTION audit_events_immutable() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'audit_events is append-only';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS audit_events_no_change ON audit_events",
    """
    CREATE TRIGGER audit_events_no_change
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION audit_events_immutable()
    """,
)


class AuditEntry(BaseModel):
    audit_id: str
    occurred_at: datetime
    actor: str
    action: str
    entity_type: str = "change"
    entity_id: str
    from_state: str | None = None
    to_state: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


def new_audit_entry(
    actor: str,
    action: str,
    entity_id: str,
    from_state: str | None = None,
    to_state: str | None = None,
    details: dict[str, Any] | None = None,
    entity_type: str = "change",
) -> AuditEntry:
    return AuditEntry(
        audit_id=f"AUD-{uuid4().hex[:12].upper()}",
        occurred_at=datetime.now(timezone.utc),
        actor=actor,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        from_state=from_state,
        to_state=to_state,
        details=details or {},
    )


class AuditLog(Protocol):
    def append(self, entry: AuditEntry) -> None:
        ...

    def list(self, entity_id: str | None = None, limit: int = 100) -> list[AuditEntry]:
        """The most recent `limit` entries (optionally for one entity), oldest first."""
        ...


class JsonlAuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(self, entry: AuditEntry) -> None:
        line = json.dumps(entry.model_dump(mode="json"), separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def list(self, entity_id: str | None = None, limit: int = 100) -> list[AuditEntry]:
        with self._lock:
            if not self.path.exists():
                return []
            lines = self.path.read_text(encoding="utf-8-sig").splitlines()

        entries: list[AuditEntry] = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                entry = AuditEntry(**json.loads(line))
            except Exception:
                logger.error("audit-line-unreadable", extra={"line": number})
                continue
            if entity_id is None or entry.entity_id == entity_id:
                entries.append(entry)
        return entries[-max(1, limit):]


def pg_insert_audit(cur, entry: AuditEntry) -> None:
    """Insert on an existing cursor so the caller can make it part of its own transaction."""
    cur.execute(
        """
        INSERT INTO audit_events
            (audit_id, occurred_at, actor, action, entity_type, entity_id, from_state, to_state, details)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
        """,
        (
            entry.audit_id,
            entry.occurred_at.isoformat(),
            entry.actor,
            entry.action,
            entry.entity_type,
            entry.entity_id,
            entry.from_state,
            entry.to_state,
            json.dumps(entry.details),
        ),
    )


class PostgresAuditLog:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url.strip()
        self._schema_ready = False

    def _connect(self):
        return pg_connect(self.database_url)

    def _run(self, work):
        with self._connect() as conn:
            with conn.cursor() as cur:
                if not self._schema_ready:
                    for statement in PG_AUDIT_SCHEMA:
                        cur.execute(statement)
                result = work(cur)
        self._schema_ready = True
        return result

    def append(self, entry: AuditEntry) -> None:
        self._run(lambda cur: pg_insert_audit(cur, entry))

    def list(self, entity_id: str | None = None, limit: int = 100) -> list[AuditEntry]:
        columns = "audit_id, occurred_at, actor, action, entity_type, entity_id, from_state, to_state, details"

        def work(cur):
            if entity_id is None:
                cur.execute(
                    f"SELECT {columns} FROM audit_events ORDER BY occurred_at DESC, audit_id DESC LIMIT %s",
                    (max(1, limit),),
                )
            else:
                cur.execute(
                    f"SELECT {columns} FROM audit_events WHERE entity_id = %s "
                    "ORDER BY occurred_at DESC, audit_id DESC LIMIT %s",
                    (entity_id, max(1, limit)),
                )
            return cur.fetchall()

        rows = self._run(work)
        entries = [
            AuditEntry(
                audit_id=row[0],
                occurred_at=row[1],
                actor=row[2],
                action=row[3],
                entity_type=row[4],
                entity_id=row[5],
                from_state=row[6],
                to_state=row[7],
                details=row[8] if isinstance(row[8], dict) else json.loads(row[8] or "{}"),
            )
            for row in rows
        ]
        entries.reverse()
        return entries
