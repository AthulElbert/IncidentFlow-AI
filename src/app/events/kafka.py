"""Kafka implementation of the EventBus interface (optional; needs the `confluent-kafka` package).

`publish` produces to the topic keyed by the event key (the service), so one service's events stay ordered within a
partition. `poll` and `commit` map onto a consumer group with auto-commit off: an event is committed only after its
handler succeeds, and one that was polled but not committed is delivered again on the next poll (the bus contract),
which needs an explicit seek back because a Kafka consumer otherwise moves on.

`event.seq` carries the partition offset, which orders events within a partition only.

Not exercised against a real broker in this repository's tests: they use an in-memory stand-in for the client, so the
mapping is tested but broker behaviour (rebalances, auth, TLS) is not.

With Postgres storage the change store writes its events to the Postgres outbox in the same transaction as the state
change. Use `relay` to forward them to Kafka; publishing straight to Kafka after the commit could lose an event.
"""
import json
import logging
import threading
from typing import Any, Callable

from app.events.bus import Event, EventBus, drain

logger = logging.getLogger("app.kafka_bus")

RELAY_GROUP = "kafka-relay"


class KafkaUnavailable(RuntimeError):
    pass


def _confluent():
    try:
        import confluent_kafka
    except ImportError as exc:
        raise KafkaUnavailable("EVENT_BUS_MODE=kafka needs the 'confluent-kafka' package (pip install confluent-kafka)") from exc
    return confluent_kafka


class KafkaBus:
    def __init__(
        self,
        bootstrap_servers: str,
        client_id: str = "incidentflow",
        producer: Any = None,
        consumer_factory: Callable[[str], Any] | None = None,
        partition_type: Callable[[str, int, int], Any] | None = None,
        poll_timeout_seconds: float = 1.0,
        flush_timeout_seconds: float = 10.0,
    ) -> None:
        if not bootstrap_servers.strip():
            raise ValueError("Kafka needs bootstrap servers")
        self.bootstrap_servers = bootstrap_servers.strip()
        self.client_id = client_id
        self._producer = producer
        self._consumer_factory = consumer_factory
        self._partition_type = partition_type
        self.poll_timeout = poll_timeout_seconds
        self.flush_timeout = flush_timeout_seconds
        self._consumers: dict[str, Any] = {}
        self._subscribed: set[str] = set()
        self._pending: dict[str, dict[int, int]] = {}  # group -> partition -> lowest offset polled but not committed
        self._positions: dict[str, tuple[int, int]] = {}  # event id -> (partition, offset)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ clients, built on first use

    def _get_producer(self):
        if self._producer is None:
            self._producer = _confluent().Producer({"bootstrap.servers": self.bootstrap_servers, "client.id": self.client_id})
        return self._producer

    def _get_consumer(self, group: str):
        if group not in self._consumers:
            if self._consumer_factory is not None:
                self._consumers[group] = self._consumer_factory(group)
            else:
                self._consumers[group] = _confluent().Consumer(
                    {
                        "bootstrap.servers": self.bootstrap_servers,
                        "group.id": group,
                        "client.id": self.client_id,
                        "enable.auto.commit": False,
                        "auto.offset.reset": "earliest",
                    }
                )
        return self._consumers[group]

    def _tp(self, topic: str, partition: int, offset: int):
        return (self._partition_type or _confluent().TopicPartition)(topic, partition, offset)

    # ------------------------------------------------------------------------------ the bus interface

    def publish(self, event: Event) -> Event:
        producer = self._get_producer()
        failure: list[Exception] = []

        def delivered(error, _message):
            if error is not None:
                failure.append(RuntimeError(str(error)))

        producer.produce(
            event.topic,
            key=event.key.encode("utf-8"),
            value=json.dumps(event.model_dump(mode="json")).encode("utf-8"),
            on_delivery=delivered,
        )
        remaining = producer.flush(self.flush_timeout)
        if failure:
            raise failure[0]
        if remaining:
            raise TimeoutError(f"Kafka did not confirm {remaining} message(s) within {self.flush_timeout}s")
        return event

    def poll(self, topic: str, group: str, limit: int = 100) -> list[Event]:
        with self._lock:
            consumer = self._get_consumer(group)
            if group not in self._subscribed:
                consumer.subscribe([topic])
                self._subscribed.add(group)
            # Anything polled earlier and never committed comes back: go back to it before reading on.
            for partition, offset in list(self._pending.get(group, {}).items()):
                consumer.seek(self._tp(topic, partition, offset))

            events: list[Event] = []
            for message in consumer.consume(num_messages=max(1, limit), timeout=self.poll_timeout) or []:
                if message.error():
                    logger.warning("kafka-message-error", extra={"error": str(message.error())})
                    continue
                try:
                    event = Event(**json.loads(message.value())).model_copy(update={"seq": message.offset()})
                except Exception:
                    logger.exception("kafka-message-unreadable", extra={"partition": message.partition(), "offset": message.offset()})
                    continue
                self._positions[event.event_id] = (message.partition(), message.offset())
                lowest = self._pending.setdefault(group, {})
                lowest[message.partition()] = min(lowest.get(message.partition(), message.offset()), message.offset())
                events.append(event)
            return events

    def commit(self, group: str, event: Event) -> None:
        with self._lock:
            position = self._positions.get(event.event_id)
            if position is None:
                raise ValueError(f"Event {event.event_id} was not polled by this bus instance")
            partition, offset = position
            self._get_consumer(group).commit(offsets=[self._tp(event.topic, partition, offset + 1)], asynchronous=False)
            pending = self._pending.get(group, {})
            if pending.get(partition) == offset:  # commits arrive in order (see `drain`); only the next one moves the mark
                pending[partition] = offset + 1
            self._positions.pop(event.event_id, None)

    def close(self) -> None:
        for consumer in self._consumers.values():
            try:
                consumer.close()
            except Exception:
                logger.exception("kafka-consumer-close-failed")


def relay(outbox: EventBus, kafka: EventBus, topic: str, limit: int = 100) -> int:
    """Forward events from the Postgres outbox to Kafka. At-least-once: an event is committed only once Kafka has it."""
    return drain(outbox, topic, RELAY_GROUP, lambda event: kafka.publish(event), limit)
