"""The broker side against a real RabbitMQ. Skipped unless EXRADIUS_GITHUB_TEST_BROKER_URL
names a throwaway broker: the test declares the bridge's queue and publishes tasks."""

from __future__ import annotations

import asyncio
import json
import os

import aio_pika
import pytest

from exradius_github.exradius import EVENTS_EXCHANGE, TASKS_QUEUE, Broker

URL = os.environ.get("EXRADIUS_GITHUB_TEST_BROKER_URL")
pytestmark = pytest.mark.skipif(not URL, reason="EXRADIUS_GITHUB_TEST_BROKER_URL is not set")


async def test_publish_and_consume():
    assert URL
    broker = Broker(URL)
    await broker.connect()
    received: list[dict] = []
    failures: list[dict] = []

    async def handler(event: dict) -> None:
        if event.get("fail"):
            failures.append(event)
            raise RuntimeError("boom")
        received.append(event)

    await broker.consume(handler)
    connection = await aio_pika.connect_robust(URL)
    channel = await connection.channel()
    try:
        tasks = await channel.declare_queue(TASKS_QUEUE, durable=True)
        await tasks.purge()
        await broker.publish_task({"version": 1, "task_id": "T-1"})
        message = await tasks.get(timeout=5)
        assert json.loads(message.body) == {"version": 1, "task_id": "T-1"}
        await message.ack()

        exchange = await channel.get_exchange(EVENTS_EXCHANGE)
        for key, body in [("task.status", {"type": "status"}), ("directory.changed", {"type": "directory_changed"}), ("other.key", {"type": "x"})]:
            await exchange.publish(aio_pika.Message(json.dumps(body).encode()), routing_key=key)
        await exchange.publish(aio_pika.Message(json.dumps({"fail": True}).encode()), routing_key="task.result")
        for _ in range(100):
            if len(received) == 2 and len(failures) == 2:
                break
            await asyncio.sleep(0.1)
        assert [e["type"] for e in received] == ["status", "directory_changed"]
        assert len(failures) == 2  # tried, retried once, then dropped
    finally:
        await connection.close()
        await broker.close()
