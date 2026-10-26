"""Postgres-backed event bus using the transactional outbox pattern.

The change store writes event rows in the same transaction as the state change
(`pg_insert_event`), so an event exists if and only if the change was committed.
Consumers read the outbox with per-group offsets.

Known limitation: `seq` is assigned when a row is inserted, but rows become
visible in commit order. A slow transaction can therefore commit after a
higher-numbered row has already been read, and a consumer that has advanced past
it will not see it. Kafka does not have this problem; for this backend keep
transactions short and treat it as a demo/low-volume bus.
"""
import json
from datetime import datetime

from app.db import pg_connect
from app.events.bus import Event

PG_EVENT_SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS event_outbox (
        seq BIGSERIAL PRIMARY KEY,
        event_id TEXT NOT NULL UNIQUE,
        topic TEXT NOT NULL,
        event_key TEXT NOT NULL,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS event_outbox_topic_seq_idx ON event_outbox (topic, seq)",
    """
    CREATE TABLE IF NOT EXISTS event_offsets (
        topic TEXT NOT NULL,
        consumer_group TEXT NOT NULL,
        last_seq BIGINT NOT NULL,
        PRIMARY KEY (topic, consumer_group)
    )
    """,
)


def pg_insert_event(cur, event: Event) -> None:
    """Insert on an existing cursor so the caller can make it part of its own transaction."""
    cur.execute(
        """
        INSERT INTO event_outbox (event_id, topic, event_key, payload, created_at)
        VALUES (%s, %s, %s, %s::jsonb, %s)
        """,
        (event.event_id, event.topic, event.key, json.dumps(event.payload), event.created_at.isoformat()),
    )


class PostgresBus:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url.strip()
        self._schema_ready = False

    def _connect(self):
        return pg_connect(self.database_url)

    def _run(self, work):
        with self._connect() as conn:
            with conn.cursor() as cur:
                if not self._schema_ready:
                    for statement in PG_EVENT_SCHEMA:
                        cur.execute(statement)
                result = work(cur)
        self._schema_ready = True
        return result

    def publish(self, event: Event) -> Event:
        def work(cur):
            pg_insert_event(cur, event)
            cur.execute("SELECT seq FROM event_outbox WHERE event_id = %s", (event.event_id,))
            row = cur.fetchone()
            return int(row[0]) if row else None

        return event.model_copy(update={"seq": self._run(work)})

    def poll(self, topic: str, group: str, limit: int = 100) -> list[Event]:
        def work(cur):
            cur.execute(
                """
                SELECT seq, event_id, topic, event_key, payload, created_at
                FROM event_outbox
                WHERE topic = %s
                  AND seq > COALESCE(
                      (SELECT last_seq FROM event_offsets WHERE topic = %s AND consumer_group = %s), 0)
                ORDER BY seq
                LIMIT %s
                """,
                (topic, topic, group, max(1, limit)),
            )
            return cur.fetchall()

        events: list[Event] = []
        for seq, event_id, row_topic, key, payload, created_at in self._run(work):
            events.append(
                Event(
                    event_id=event_id,
                    topic=row_topic,
                    key=key,
                    payload=payload if isinstance(payload, dict) else json.loads(payload),
                    created_at=created_at if isinstance(created_at, datetime) else datetime.fromisoformat(created_at),
                    seq=int(seq),
                )
            )
        return events

    def commit(self, group: str, event: Event) -> None:
        def work(cur):
            cur.execute(
                """
                INSERT INTO event_offsets (topic, consumer_group, last_seq)
                VALUES (%s, %s, %s)
                ON CONFLICT (topic, consumer_group)
                DO UPDATE SET last_seq = GREATEST(event_offsets.last_seq, EXCLUDED.last_seq)
                """,
                (event.topic, group, event.seq or 0),
            )

        self._run(work)
