"""Exradius's public contract: the broker (architecture §4) and the directory
(integration-api.md §4). Nothing here imports Exradius. A copy of exradius-jira's, plus
each project's repositories."""

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
from pydantic import BaseModel, ConfigDict, Field, ValidationError

log = logging.getLogger(__name__)

TASKS_QUEUE = "exradius.tasks"
ANSWERS_QUEUE = "exradius.answers"
EVENTS_EXCHANGE = "exradius.events"
EVENTS_QUEUE = "exradius-github.events"
BINDINGS = ("task.*", "directory.changed")


class ExradiusError(Exception):
    pass


# -- the directory -----------------------------------------------------------------------


class Agent(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    slug: str
    name: str


class Repository(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    name: str
    url: str


class Project(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    slug: str
    name: str
    runnable: bool
    repositories: tuple[Repository, ...] = ()
    agents: tuple[Agent, ...] = ()


class Directory(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)
    projects: tuple[Project, ...] = Field(default=())


class DirectoryClient:
    def __init__(self, api_url: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self._http = httpx.AsyncClient(
            base_url=api_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=30.0,
            transport=transport,
        )
        self._etag: str | None = None
        self._cached: Directory | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch(self) -> Directory:
        headers = {"If-None-Match": self._etag} if self._etag and self._cached else {}
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
            directory = Directory.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ExradiusError(f"the Exradius directory is not what this bridge expects: {exc}") from exc
        self._etag = response.headers.get("ETag")
        self._cached = directory
        return directory


# -- the broker --------------------------------------------------------------------------


class Publisher(Protocol):
    async def publish_task(self, message: dict[str, Any]) -> None: ...

    async def publish_answer(self, message: dict[str, Any]) -> None: ...


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


class Broker:
    """Publishes tasks and answers; consumes the bridge's own queue of events."""

    def __init__(self, url: str):
        self.url = url
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._publish: aio_pika.abc.AbstractChannel | None = None
        self._queue: aio_pika.abc.AbstractQueue | None = None

    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self.url)
        self._publish = await self._connection.channel(publisher_confirms=True)
        consume = await self._connection.channel()
        # One event at a time keeps an issue's events in the order Exradius sent them.
        await consume.set_qos(prefetch_count=1)
        # The same declarations as Exradius's own (durable, no arguments), so either side
        # may come up first.
        for name in (TASKS_QUEUE, ANSWERS_QUEUE):
            await self._publish.declare_queue(name, durable=True)
        exchange = await consume.declare_exchange(EVENTS_EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True)
        self._queue = await consume.declare_queue(EVENTS_QUEUE, durable=True)
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
                # queue (prefetch 1) for every other issue.
                if message.redelivered:
                    log.exception("dropping event %s for task %s after a second failure", event.get("type"), event.get("task_id"))
                    await message.reject(requeue=False)
                else:
                    log.exception("handling event %s for task %s failed; it will be retried", event.get("type"), event.get("task_id"))
                    await asyncio.sleep(5)
                    await message.nack(requeue=True)
                return
            await message.ack()

        await self._queue.consume(on_message)
