import os
import uuid
from datetime import datetime, timezone

import pytest

from app.events.bus import drain, make_event
from app.events.inprocess import InProcessBus
from app.events.postgres import PG_EVENT_SCHEMA, PostgresBus
from pg_fakes import FakeDatabase


def _publish(bus, n, topic="incident.events"):
    return [bus.publish(make_event(topic, "payments-service", {"n": i})) for i in range(n)]


def test_publish_assigns_increasing_sequence_numbers_per_topic():
    bus = InProcessBus()
    first = bus.publish(make_event("a", "k"))
    second = bus.publish(make_event("a", "k"))
    other = bus.publish(make_event("b", "k"))

    assert (first.seq, second.seq, other.seq) == (1, 2, 1)


def test_each_consumer_group_gets_every_event_independently():
    bus = InProcessBus()
    _publish(bus, 3)

    assert [e.payload["n"] for e in bus.poll("incident.events", "dashboard")] == [0, 1, 2]
    assert [e.payload["n"] for e in bus.poll("incident.events", "audit")] == [0, 1, 2]


def test_commit_advances_only_that_groups_offset():
    bus = InProcessBus()
    events = _publish(bus, 3)

    bus.commit("dashboard", events[1])

    assert [e.payload["n"] for e in bus.poll("incident.events", "dashboard")] == [2]
    assert len(bus.poll("incident.events", "audit")) == 3
    # Committing an older event never moves the offset backwards.
    bus.commit("dashboard", events[0])
    assert [e.payload["n"] for e in bus.poll("incident.events", "dashboard")] == [2]


def test_uncommitted_events_are_redelivered():
    bus = InProcessBus()
    _publish(bus, 2)

    assert len(bus.poll("incident.events", "g")) == 2
    assert len(bus.poll("incident.events", "g")) == 2


def test_poll_respects_limit_and_topic():
    bus = InProcessBus()
    _publish(bus, 5)
    _publish(bus, 2, topic="other")

    assert len(bus.poll("incident.events", "g", limit=3)) == 3
    assert len(bus.poll("other", "g")) == 2
    assert bus.poll("empty", "g") == []


def test_drain_commits_after_each_successful_handler():
    bus = InProcessBus()
    _publish(bus, 3)
    seen: list[int] = []

    assert drain(bus, "incident.events", "g", lambda e: seen.append(e.payload["n"])) == 3

    assert seen == [0, 1, 2]
    assert bus.poll("incident.events", "g") == []


def test_drain_leaves_a_failed_event_uncommitted_for_redelivery():
    bus = InProcessBus()
    _publish(bus, 3)
    handled: list[int] = []

    def flaky(event):
        if event.payload["n"] == 1:
            raise RuntimeError("downstream unavailable")
        handled.append(event.payload["n"])

    with pytest.raises(RuntimeError):
        drain(bus, "incident.events", "g", flaky)

    assert handled == [0]
    # Event 0 is committed; event 1 (the failure) and 2 are still pending.
    assert [e.payload["n"] for e in bus.poll("incident.events", "g")] == [1, 2]


def test_retention_cap_drops_the_oldest_events():
    bus = InProcessBus(max_events_per_topic=3)
    _publish(bus, 5)

    assert [e.payload["n"] for e in bus.poll("incident.events", "g")] == [2, 3, 4]


def test_events_are_timestamped_and_keyed():
    event = make_event("incident.events", "payments-service", {"x": 1})

    assert event.event_id.startswith("EVT-")
    assert event.key == "payments-service"
    assert event.created_at.tzinfo is not None


def test_postgres_bus_publish_poll_and_commit_sql(monkeypatch):
    db = FakeDatabase()
    monkeypatch.setattr(PostgresBus, "_connect", lambda self: db.connect())
    bus = PostgresBus("postgresql://fake")

    db.results["SELECT seq FROM event_outbox"] = (7,)
    published = bus.publish(make_event("incident.events", "svc", {"a": 1}))
    assert published.seq == 7
    assert len(db.sql("INSERT INTO event_outbox")) == 1

    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    db.results["SELECT seq, event_id"] = [(8, "EVT-X", "incident.events", "svc", {"a": 2}, created)]
    polled = bus.poll("incident.events", "dashboard", limit=10)
    assert [(e.seq, e.event_id, e.payload) for e in polled] == [(8, "EVT-X", {"a": 2})]
    poll_sql, poll_params = db.sql("SELECT seq, event_id")[0]
    assert "seq > COALESCE" in poll_sql
    assert poll_params == ("incident.events", "incident.events", "dashboard", 10)

    bus.commit("dashboard", polled[0])
    commit_sql, commit_params = db.sql("INSERT INTO event_offsets")[0]
    assert "GREATEST" in commit_sql  # offsets never move backwards
    assert commit_params == ("incident.events", "dashboard", 8)


def test_postgres_outbox_schema_has_offsets_per_group():
    ddl = " ".join(" ".join(PG_EVENT_SCHEMA).split())
    assert "PRIMARY KEY (topic, consumer_group)" in ddl
    assert "seq BIGSERIAL PRIMARY KEY" in ddl


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
def test_real_postgres_bus_round_trip():
    bus = PostgresBus(os.environ["TEST_DATABASE_URL"])
    topic = f"test.{uuid.uuid4().hex[:8]}"
    published = [bus.publish(make_event(topic, "k", {"n": i})) for i in range(3)]
    assert [e.seq for e in published] == sorted(e.seq for e in published)

    group = "g1"
    polled = bus.poll(topic, group)
    assert [e.payload["n"] for e in polled] == [0, 1, 2]
    bus.commit(group, polled[1])
    assert [e.payload["n"] for e in bus.poll(topic, group)] == [2]
    assert len(bus.poll(topic, "other-group")) == 3
