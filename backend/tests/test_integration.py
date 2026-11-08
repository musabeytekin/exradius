"""Integration tests against real infrastructure, skipped unless it is available.

RabbitMQ: set ``EXRADIUS_TEST_BROKER_URL`` (e.g. ``amqp://user:password@localhost:5673/`` with
the kind install's port-forward). Runs end to end on a cluster are ``make e2e`` on kind
(docs/kubernetes.md §17), not pytest.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

from exradius.schemas.messages import EventEnvelope, status_payload
from exradius.states import State

BROKER_URL = os.environ.get("EXRADIUS_TEST_BROKER_URL")


@pytest.mark.skipif(not BROKER_URL, reason="EXRADIUS_TEST_BROKER_URL not set")
async def test_events_reach_bound_queues():
    from exradius import messaging

    connection = await messaging.connect(BROKER_URL)
    async with connection:
        channel = await connection.channel()
        exchange = await messaging.declare_topology(channel)
        queue = await channel.declare_queue(exclusive=True, auto_delete=True)
        await queue.bind(exchange, routing_key="task.#")

        envelope = EventEnvelope(
            task_id="IT-1", run_id="r", project="p", agent="a", correlation={"x": 1},
            type="status", payload=status_payload(State.RUNNING, State.PREPARING),
        )
        await messaging.EventPublisher(exchange).publish(envelope, message_id="m-1")

        message = await _get(queue)
        assert message.routing_key == "task.status"
        assert message.message_id == "m-1"
        body = json.loads(message.body)
        assert body["version"] == 1 and body["payload"]["state"] == "running"
        assert body["correlation"] == {"x": 1}
        await message.ack()


async def _get(queue, timeout: float = 5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        message = await queue.get(fail=False)
        if message is not None:
            return message
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("no message received")
        await asyncio.sleep(0.1)
