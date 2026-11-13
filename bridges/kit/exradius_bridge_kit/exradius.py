"""Exradius's public contract (design §10): the directory and the status report over the
integration API, tasks and answers out and events in over the broker. Nothing here imports
Exradius."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import aio_pika
import httpx
from aio_pika.abc import AbstractIncomingMessage

log = logging.getLogger(__name__)

TASKS_QUEUE = "exradius.tasks"
ANSWERS_QUEUE = "exradius.answers"
CANCELS_QUEUE = "exradius.cancels"
"""The public cancel (integration-api.md §10)."""
EVENTS_EXCHANGE = "exradius.events"
BINDINGS = ("task.*", "directory.changed")


def events_queue(bridge: str) -> str:
    return f"exradius-bridge.{bridge}.events"


class ExradiusError(Exception):
    pass


class IntegrationApi:
    """The directory (with ``If-None-Match``) and the status report, with the bridge's
    integration token."""

    def __init__(self, api_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )
        self._etag: str | None = None
        self._cached: dict[str, Any] | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def directory(self) -> dict[str, Any]:
        headers = {"If-None-Match": self._etag} if self._etag and self._cached is not None else {}
        try:
            response = await self._http.get("/api/integration/v1/directory", headers=headers)
        except httpx.HTTPError as exc:
            raise ExradiusError(f"cannot reach the Exradius API: {exc or type(exc).__name__}") from exc
        if response.status_code == 304 and self._cached is not None:
            return self._cached
        if response.status_code == 401:
            raise ExradiusError("the Exradius API refused EXRADIUS_INTEGRATION_TOKEN")
        if response.status_code != 200:
            raise ExradiusError(f"the Exradius directory answered {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise ExradiusError("the Exradius directory is not JSON") from exc
        if not isinstance(body, dict) or not isinstance(body.get("projects"), list):
            raise ExradiusError("the Exradius directory is not what this kit expects")
        self._etag = response.headers.get("ETag")
        self._cached = body
        return body

    async def report(self, report: dict[str, Any]) -> None:
        try:
            response = await self._http.put("/api/integration/v1/status", json=report)
        except httpx.HTTPError as exc:
            raise ExradiusError(f"cannot reach the Exradius API: {exc or type(exc).__name__}") from exc
        if response.status_code != 204:
            raise ExradiusError(f"the Exradius status report answered {response.status_code}: {response.text[:200]}")


class Publisher(Protocol):
    async def publish_task(self, message: dict[str, Any]) -> None: ...

    async def publish_answer(self, message: dict[str, Any]) -> None: ...

    async def publish_cancel(self, message: dict[str, Any]) -> None: ...


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class Broker:
    """Publishes tasks, answers and cancels; consumes the bridge's own queue of events."""

    def __init__(self, url: str, bridge: str, *, retry_after: float = 5.0):
        self.url = url
        self.queue_name = events_queue(bridge)
        self.retry_after = retry_after
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._publish: aio_pika.abc.AbstractChannel | None = None
        self._queue: aio_pika.abc.AbstractQueue | None = None

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self.url)
        self._publish = await self._connection.channel(publisher_confirms=True)
        consume = await self._connection.channel()
        # One event at a time keeps an item's events in the order Exradius sent them.
        await consume.set_qos(prefetch_count=1)
        # The same declarations as Exradius's own (durable, no arguments), so either side
        # may come up first.
        for name in (TASKS_QUEUE, ANSWERS_QUEUE, CANCELS_QUEUE):
            await self._publish.declare_queue(name, durable=True)
        exchange = await consume.declare_exchange(EVENTS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
        self._queue = await consume.declare_queue(self.queue_name, durable=True)
        for key in BINDINGS:
            await self._queue.bind(exchange, routing_key=key)

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()

    async def _send(self, queue: str, message: dict[str, Any]) -> None:
        assert self._publish is not None, "connect() first"
        body = aio_pika.Message(
            body=json.dumps(message).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=str(uuid.uuid4()),
        )
        # mandatory: a message no queue takes is an error, not silently dropped.
        await self._publish.default_exchange.publish(body, routing_key=queue, mandatory=True)

    async def publish_task(self, message: dict[str, Any]) -> None:
        await self._send(TASKS_QUEUE, message)

    async def publish_answer(self, message: dict[str, Any]) -> None:
        await self._send(ANSWERS_QUEUE, message)

    async def publish_cancel(self, message: dict[str, Any]) -> None:
        """``{"version": 1, "task_id": ..., "reason": ...}``: Exradius cancels the task's run
        if it has not ended, and drops the message otherwise (integration-api.md §10)."""
        await self._send(CANCELS_QUEUE, message)

    async def consume(self, handler: EventHandler) -> None:
        assert self._queue is not None, "connect() first"

        async def on_message(message: AbstractIncomingMessage) -> None:
            try:
                event = json.loads(message.body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                log.warning("dropping an event that is not JSON")
                await message.reject(requeue=False)
                return
            try:
                await handler(event)
            except Exception:
                # One more try, then drop it: an event that fails twice would block the
                # queue (prefetch 1) for every other item.
                if message.redelivered:
                    log.exception("dropping event %s for task %s after a second failure",
                                  event.get("type"), event.get("task_id"))
                    await message.reject(requeue=False)
                else:
                    log.exception("handling event %s for task %s failed; it will be retried",
                                  event.get("type"), event.get("task_id"))
                    await asyncio.sleep(self.retry_after)
                    await message.nack(requeue=True)
                return
            await message.ack()

        await self._queue.consume(on_message)
