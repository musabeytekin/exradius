"""RabbitMQ topology and publishing helpers."""

from __future__ import annotations

import json
import logging
from typing import Any

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractRobustConnection

from exradius.schemas.messages import DirectoryEvent, EventEnvelope

log = logging.getLogger(__name__)

TASKS_QUEUE = "exradius.tasks"
ANSWERS_QUEUE = "exradius.answers"
CANCELS_QUEUE = "exradius.cancels"
"""Public: integrations stop a task (architecture §4.5, docs/integration-api.md §10)."""
EVENTS_EXCHANGE = "exradius.events"
# Internal: admin actions (cancel) from the API server to the workers. Not part of the
# public contract in §4.
CONTROL_QUEUE = "exradius.control"


async def connect(url: str) -> AbstractRobustConnection:
    return await aio_pika.connect_robust(url)


async def declare_topology(channel: AbstractChannel) -> AbstractExchange:
    """Declare every queue and exchange Exradius uses. Idempotent."""
    for name in (TASKS_QUEUE, ANSWERS_QUEUE, CANCELS_QUEUE, CONTROL_QUEUE):
        await channel.declare_queue(name, durable=True)
    return await channel.declare_exchange(
        EVENTS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
    )


class EventPublisher:
    def __init__(self, exchange: AbstractExchange):
        self._exchange = exchange

    async def publish(self, envelope: EventEnvelope, message_id: str | None = None) -> None:
        await self._exchange.publish(
            aio_pika.Message(
                body=envelope.to_json(),
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=message_id,
                type=envelope.type,
            ),
            routing_key=envelope.routing_key,
        )
        log.info("published %s task=%s %s", envelope.routing_key, envelope.task_id, envelope.payload.get("state", ""))

    async def publish_directory(self, event: DirectoryEvent) -> None:
        await publish_directory(self._exchange, event)


async def publish_directory(exchange: AbstractExchange, event: DirectoryEvent) -> None:
    """The directory notice (docs/integration-api.md §3), from the API or a worker."""
    await exchange.publish(
        aio_pika.Message(
            body=event.to_json(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            type=event.type,
        ),
        routing_key=event.routing_key,
    )
    log.info("published %s projects=%s", event.routing_key, ",".join(event.payload.projects) or "-")


async def publish_to_queue(channel: AbstractChannel, queue: str, body: dict[str, Any]) -> None:
    await channel.default_exchange.publish(
        aio_pika.Message(
            body=json.dumps(body).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        ),
        routing_key=queue,
    )
