"""KafkaBus against an in-memory stand-in for the Kafka client. This tests our mapping onto Kafka's model
(keys, offsets, commits, redelivery); it says nothing about a real broker."""
import sys
from collections import namedtuple

import pytest

from app.config import ConfigError, load_settings, validate_settings
from app.events.bus import INCIDENT_EVENTS_TOPIC, drain, make_event
from app.events.inprocess import InProcessBus
from app.events.kafka import KafkaBus, KafkaUnavailable, relay

TP = namedtuple("TP", "topic partition offset")


class FakeMessage:
    def __init__(self, partition, offset, value):
        self._partition, self._offset, self._value = partition, offset, value

    def error(self):
        return None

    def partition(self):
        return self._partition

    def offset(self):
        return self._offset

    def value(self):
        return self._value


class FakeBroker:
    def __init__(self, partitions=3):
        self.log = {p: [] for p in range(partitions)}  # partition -> list of (key, value)
        self.committed: dict[tuple[str, int], int] = {}  # (group, partition) -> next offset
        self.fail_delivery: Exception | None = None
        self.unconfirmed = 0


class FakeProducer:
    def __init__(self, broker):
        self.broker = broker

    def produce(self, topic, key, value, on_delivery):
        partition = sum(key) % len(self.broker.log)
        if self.broker.fail_delivery is None:
            self.broker.log[partition].append((key, value))
        on_delivery(self.broker.fail_delivery, None)

    def flush(self, timeout):
        return self.broker.unconfirmed


class FakeConsumer:
    def __init__(self, broker, group):
        self.broker, self.group = broker, group
        self.position: dict[int, int] = {}
        self.commits = 0

    def subscribe(self, topics):
        for partition in self.broker.log:
            self.position[partition] = self.broker.committed.get((self.group, partition), 0)

    def seek(self, tp):
        self.position[tp.partition] = tp.offset

    def consume(self, num_messages, timeout):
        out = []
        for partition, entries in self.broker.log.items():
            while self.position[partition] < len(entries) and len(out) < num_messages:
                out.append(FakeMessage(partition, self.position[partition], entries[self.position[partition]][1]))
                self.position[partition] += 1
        return out

    def commit(self, offsets, asynchronous):
        for tp in offsets:
            self.broker.committed[(self.group, tp.partition)] = tp.offset
            self.commits += 1

    def close(self):
        pass


def make_bus(broker=None):
    broker = broker or FakeBroker()
    bus = KafkaBus(
        "broker:9092", producer=FakeProducer(broker), consumer_factory=lambda group: FakeConsumer(broker, group), partition_type=TP,
    )
    return bus, broker


def _event(service="payments", n=1):
    return make_event(INCIDENT_EVENTS_TOPIC, service, {"change_id": f"CHG-{n}", "to_state": "ready_for_prod"})


# --- publish and poll -----------------------------------------------------------------------


def test_an_event_survives_the_round_trip_and_carries_its_offset():
    bus, _ = make_bus()
    sent = _event(n=7)
    bus.publish(sent)

    [got] = bus.poll(INCIDENT_EVENTS_TOPIC, "g")

    assert got.event_id == sent.event_id and got.key == "payments" and got.payload == sent.payload
    assert got.seq == 0


def test_events_for_one_service_stay_in_order():
    bus, broker = make_bus()
    for n in range(5):
        bus.publish(_event("payments", n))
    bus.publish(_event("orders", 99))

    polled = [e.payload["change_id"] for e in bus.poll(INCIDENT_EVENTS_TOPIC, "g", limit=50) if e.key == "payments"]

    assert polled == [f"CHG-{n}" for n in range(5)]
    assert sum(1 for entries in broker.log.values() if any(k == b"payments" for k, _ in entries)) == 1  # one partition


def test_an_event_that_was_polled_but_not_committed_is_delivered_again():
    bus, _ = make_bus()
    bus.publish(_event(n=1))
    bus.publish(_event(n=2))

    first = bus.poll(INCIDENT_EVENTS_TOPIC, "g")
    again = bus.poll(INCIDENT_EVENTS_TOPIC, "g")
    assert [e.event_id for e in first] == [e.event_id for e in again] and len(first) == 2

    bus.commit("g", first[0])
    after = bus.poll(INCIDENT_EVENTS_TOPIC, "g")
    assert [e.payload["change_id"] for e in after] == ["CHG-2"]
    bus.commit("g", after[0])
    assert bus.poll(INCIDENT_EVENTS_TOPIC, "g") == []


def test_each_consumer_group_has_its_own_position():
    bus, _ = make_bus()
    bus.publish(_event())
    [event] = bus.poll(INCIDENT_EVENTS_TOPIC, "learning")
    bus.commit("learning", event)

    assert bus.poll(INCIDENT_EVENTS_TOPIC, "learning") == []
    assert len(bus.poll(INCIDENT_EVENTS_TOPIC, "other")) == 1


def test_a_failing_handler_leaves_its_event_to_be_redelivered_and_keeps_earlier_ones_committed():
    bus, _ = make_bus()
    for n in range(3):
        bus.publish(_event("payments", n))
    seen = []

    def handler(event):
        seen.append(event.payload["change_id"])
        if event.payload["change_id"] == "CHG-1" and seen.count("CHG-1") == 1:
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        drain(bus, INCIDENT_EVENTS_TOPIC, "g", handler)
    drain(bus, INCIDENT_EVENTS_TOPIC, "g", handler)

    assert seen == ["CHG-0", "CHG-1", "CHG-1", "CHG-2"]


def test_committing_an_event_this_bus_never_polled_is_refused():
    bus, _ = make_bus()

    with pytest.raises(ValueError, match="was not polled"):
        bus.commit("g", _event())


def test_a_message_that_cannot_be_read_is_skipped_not_fatal():
    bus, broker = make_bus()
    broker.log[0].append((b"x", b"not json"))
    bus.publish(_event(n=5))

    assert [e.payload["change_id"] for e in bus.poll(INCIDENT_EVENTS_TOPIC, "g")] == ["CHG-5"]


# --- publish failures ------------------------------------------------------------------------


def test_a_delivery_error_or_an_unconfirmed_message_is_reported_not_swallowed():
    bus, broker = make_bus()
    broker.fail_delivery = RuntimeError("broker rejected it")
    with pytest.raises(RuntimeError, match="rejected"):
        bus.publish(_event())

    broker.fail_delivery = None
    broker.unconfirmed = 1
    with pytest.raises(TimeoutError, match="did not confirm"):
        bus.publish(_event())


def test_without_the_kafka_package_the_error_says_what_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "confluent_kafka", None)
    bus = KafkaBus("broker:9092")

    with pytest.raises(KafkaUnavailable, match="confluent-kafka"):
        bus.publish(_event())


def test_bootstrap_servers_are_required():
    with pytest.raises(ValueError):
        KafkaBus("  ")


# --- the relay from the Postgres outbox --------------------------------------------------------------


def test_the_relay_forwards_each_outbox_event_to_kafka_once_and_resumes_where_it_stopped():
    outbox = InProcessBus()
    for n in range(3):
        outbox.publish(_event("payments", n))
    kafka, broker = make_bus()

    assert relay(outbox, kafka, INCIDENT_EVENTS_TOPIC) == 3
    assert relay(outbox, kafka, INCIDENT_EVENTS_TOPIC) == 0
    outbox.publish(_event("payments", 3))
    assert relay(outbox, kafka, INCIDENT_EVENTS_TOPIC) == 1

    assert sum(len(entries) for entries in broker.log.values()) == 4


def test_if_kafka_is_down_the_relay_keeps_the_event_in_the_outbox_for_the_next_attempt():
    outbox = InProcessBus()
    outbox.publish(_event(n=1))
    kafka, broker = make_bus()
    broker.fail_delivery = RuntimeError("down")

    with pytest.raises(RuntimeError):
        relay(outbox, kafka, INCIDENT_EVENTS_TOPIC)
    broker.fail_delivery = None

    assert relay(outbox, kafka, INCIDENT_EVENTS_TOPIC) == 1
    assert sum(len(entries) for entries in broker.log.values()) == 1


# --- settings and wiring -----------------------------------------------------------------------------


def test_kafka_mode_needs_servers_and_unknown_modes_are_rejected(monkeypatch):
    monkeypatch.setenv("EVENT_BUS_MODE", "kafka")
    with pytest.raises(ConfigError, match="KAFKA_BOOTSTRAP_SERVERS"):
        validate_settings(load_settings())

    monkeypatch.setenv("EVENT_BUS_MODE", "rabbit")
    with pytest.raises(ConfigError, match="EVENT_BUS_MODE"):
        validate_settings(load_settings())

    monkeypatch.setenv("EVENT_BUS_MODE", "kafka")
    monkeypatch.setenv("KAFKA_BOOTSTRAP_SERVERS", "broker:9092")
    validate_settings(load_settings())


def test_by_default_there_is_no_kafka(make_app):
    main_module, _ = make_app()

    assert main_module.kafka_bus is None and main_module.relay_loop is None
    assert isinstance(main_module.event_bus, InProcessBus) and main_module.outcome_learner.bus is main_module.event_bus


def test_with_json_storage_kafka_becomes_the_stores_bus_and_the_learners(make_app):
    main_module, _ = make_app(EVENT_BUS_MODE="kafka", KAFKA_BOOTSTRAP_SERVERS="broker:9092")

    assert isinstance(main_module.kafka_bus, KafkaBus)
    assert main_module.event_bus is main_module.kafka_bus and main_module.outcome_learner.bus is main_module.kafka_bus
    assert main_module.change_store.bus is main_module.kafka_bus
    assert main_module.relay_loop is None  # nothing to relay: there is no outbox without Postgres
