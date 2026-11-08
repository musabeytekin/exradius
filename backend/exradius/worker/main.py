"""``exradius-worker``: consume tasks and answers, run agents, publish events."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import uuid

from aio_pika.abc import AbstractIncomingMessage
from sqlalchemy import update

from exradius import directory, messaging
from exradius.config import Settings, get_settings
from exradius.routing import Router
from exradius.db import make_engine, make_session_factory
from exradius.models import Event, utcnow
from exradius.storage import make_store
from exradius.schemas.project_config import format_memory, memory_bytes
from exradius.worker.containers import ContainerManager
from exradius.worker.memory import HEADROOM
from exradius.worker.intake import accept_answer, accept_task
from exradius.worker.lifecycle import PendingEvent
from exradius.worker.heartbeat import Heartbeat
from exradius.worker.presence import make_presence, new_worker_id
from exradius.worker.provider_test import ProviderTester
from exradius.worker.verify import ProjectVerifier
from exradius.worker.runs import RunManager

log = logging.getLogger("exradius.worker")

TARGET_KEYS = {"cancel": "run_id", "verify": "verification_id", "test_provider": "test_id"}
"""Control actions and the key of the id each one carries."""


class Worker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = make_engine(settings.database_url)
        self.sessions = make_session_factory(self.engine)
        self.store = make_store(settings)
        self.publisher: messaging.EventPublisher | None = None
        # Concurrency is the prefetch count: a task message stays unacknowledged (and is
        # not persisted) until a slot is free, so the broker holds the backlog.
        self.slots = asyncio.Semaphore(settings.worker_concurrency)
        self.containers = ContainerManager(settings)
        self.runs = RunManager(settings, self.sessions, self.store, self.containers, self.emit)
        self.router = Router(settings.openai_base_url)
        self.verifier = ProjectVerifier(self.runs)
        self.provider_tester = ProviderTester(self.runs)
        self._background: set[asyncio.Task] = set()
        self.worker_id = new_worker_id()
        self.runs.worker_id = self.worker_id
        self.runs.check_directory = self.check_directory
        self.presence = make_presence(self.engine, self.worker_id)
        self.heartbeat = Heartbeat(
            self.sessions,
            self.containers,
            worker_id=self.worker_id,
            concurrency=settings.worker_concurrency,
        )

    async def emit(self, events: list[PendingEvent]) -> None:
        assert self.publisher is not None
        for pending in events:
            try:
                await self.publisher.publish(pending.envelope, message_id=str(pending.id))
            except Exception:
                # The event stays in Postgres with published_at = NULL; the run goes on.
                log.exception("could not publish %s for task %s", pending.envelope.routing_key, pending.envelope.task_id)
                continue
            async with self.sessions() as session:
                await session.execute(
                    update(Event).where(Event.id == pending.id).values(published_at=utcnow())
                )
                await session.commit()

    async def check_directory(self) -> None:
        """The directory check after this worker stored a config snapshot
        (docs/integration-api.md §3); a notice that cannot be published is retried by the
        next check, in this process or another."""
        if self.publisher is None:
            return
        await directory.check(self.sessions, self.publisher.publish_directory)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -- consumers ------------------------------------------------------------------------

    async def on_task(self, message: AbstractIncomingMessage) -> None:
        async with self.slots:
            try:
                intake = await accept_task(
                    self.sessions, self.store, message.body, box=self.runs.box, data_dir=self.settings.data_dir,
                    router=self.router,
                )
            except Exception:
                log.exception("task intake failed; message will be redelivered")
                await message.nack(requeue=True)
                await asyncio.sleep(5)
                return
            await message.ack()  # persisted: safe to acknowledge
            await self.emit(intake.events)
            if intake.config_read:
                await self.check_directory()
            if intake.run_id is not None:
                await self.runs.execute(intake.run_id, intake.memory)

    async def on_answer(self, message: AbstractIncomingMessage) -> None:
        try:
            intake = await accept_answer(self.sessions, self.store, message.body)
        except Exception:
            log.exception("answer intake failed; message will be redelivered")
            await message.nack(requeue=True)
            await asyncio.sleep(5)
            return
        await message.ack()
        if intake.run_id is not None:
            self._spawn(self._resume(intake.run_id))

    async def _resume(self, run_id: uuid.UUID) -> None:
        async with self.slots:
            await self.runs.resume(run_id)

    async def _execute_waiting(self, run_id: uuid.UUID, memory: int) -> None:
        async with self.slots:
            await self.runs.execute(run_id, memory)

    async def _reattach(self, run_id: uuid.UUID, container_id: str) -> None:
        async with self.slots:
            await self.runs.reattach(run_id, container_id)

    async def _verify(self, verification_id: uuid.UUID) -> None:
        async with self.slots:
            try:
                await self.verifier.run(verification_id)
            except Exception:
                log.exception("verification %s crashed", verification_id)

    async def _test_provider(self, test_id: uuid.UUID) -> None:
        async with self.slots:
            try:
                await self.provider_tester.run(test_id)
            except Exception:
                log.exception("provider test %s crashed", test_id)

    async def recover(self) -> None:
        """Crash recovery before consuming: re-attach to or fail runs that a dead
        worker left mid-flight, sweep orphaned run directories, and resume waiting runs
        whose answer arrived while no worker could act on it."""
        for run_id, container_id in await self.runs.reconcile(self.presence.is_alive):
            self._spawn(self._reattach(run_id, container_id))
        for run_id, memory in await self.runs.waiting_runs(self.presence.is_alive):
            self._spawn(self._execute_waiting(run_id, memory))
        await self.verifier.reconcile(self.presence.is_alive)
        await self.provider_tester.reconcile(self.presence.is_alive)
        removed = await self.runs.sweep_orphans()
        if removed:
            log.info("removed %d orphaned run directories", len(removed))
        for run_id in await self.runs.resumable_runs():
            self._spawn(self._resume(run_id))

    async def set_memory_budget(self) -> None:
        """``EXRADIUS_RUN_MEMORY_BUDGET``, else the Docker daemon's memory less 1 GiB. A
        daemon that does not answer leaves the budget unknown: nothing waits."""
        if self.settings.run_memory_budget:
            budget = memory_bytes(self.settings.run_memory_budget)
        else:
            try:
                total = int((await asyncio.to_thread(self.containers.client.info)).get("MemTotal") or 0)
            except Exception:  # noqa: BLE001 - reported, the worker still starts
                log.exception("could not read the Docker daemon's memory; reservations are not enforced")
                return
            budget = max(total - HEADROOM, 0) or None
        self.runs.memory.budget = budget
        log.info("memory budget for runs: %s", format_memory(budget) if budget else "unknown")

    async def prepare_runtimes(self) -> None:
        """Extract the runtime bundles now rather than at the first run, and remove the
        volumes of older bundles that no container uses."""
        await self.containers.bundles.ensure_all()
        try:
            await self.containers.bundles.remove_unused()
        except Exception:  # noqa: BLE001 - cleanup only; retried at the next start
            log.exception("could not remove old runtime bundle volumes")

    async def on_control(self, message: AbstractIncomingMessage) -> None:
        async with message.process(requeue=False):
            try:
                command = json.loads(message.body)
                action = command["action"]
                target = uuid.UUID(command[TARGET_KEYS[action]])
            except (ValueError, KeyError, TypeError):
                log.warning("dropping invalid control message")
                return
            if action == "cancel":
                log.info("cancelling run %s", target)
                self._spawn(self.runs.cancel(target))
            elif action == "verify":
                # Claimed before the ack, so a crash leaves a detectable owner.
                if await self.verifier.claim(target, self.worker_id):
                    log.info("verifying project (verification %s)", target)
                    self._spawn(self._verify(target))
            elif action == "test_provider":
                if await self.provider_tester.claim(target, self.worker_id):
                    log.info("testing provider (test %s)", target)
                    self._spawn(self._test_provider(target))

    # -- lifecycle ------------------------------------------------------------------------

    async def start(self) -> None:
        await self.store.ensure_bucket()
        connection = await messaging.connect(self.settings.broker_url)
        async with connection:
            tasks_channel = await connection.channel()
            await tasks_channel.set_qos(prefetch_count=self.settings.worker_concurrency)
            exchange = await messaging.declare_topology(tasks_channel)
            self.publisher = messaging.EventPublisher(exchange)

            other_channel = await connection.channel()
            await other_channel.set_qos(prefetch_count=20)
            tasks_queue = await tasks_channel.declare_queue(messaging.TASKS_QUEUE, durable=True)
            answers_queue = await other_channel.declare_queue(messaging.ANSWERS_QUEUE, durable=True)
            control_queue = await other_channel.declare_queue(messaging.CONTROL_QUEUE, durable=True)

            await self.presence.start()
            await self.set_memory_budget()
            await self.prepare_runtimes()
            self.heartbeat.start()
            await self.recover()

            await tasks_queue.consume(self.on_task)
            await answers_queue.consume(self.on_answer)
            await control_queue.consume(self.on_control)
            log.info(
                "worker %s ready (concurrency %s, data dir %s)",
                self.worker_id[:8],
                self.settings.worker_concurrency,
                self.settings.data_dir,
            )
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            await stop.wait()
            log.info("shutting down")
        await self.heartbeat.stop()
        await self.presence.stop()
        await self.engine.dispose()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aio_pika").setLevel(logging.WARNING)
    logging.getLogger("aiormq").setLevel(logging.WARNING)
    asyncio.run(Worker(get_settings()).start())


if __name__ == "__main__":
    run()
