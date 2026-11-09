"""``exradius-worker``: consume tasks, answers and control messages, drive Runs, publish events.

The worker holds the master key, Postgres and the broker, and in Kubernetes it may only ask
for Runs and write their Secrets (docs/kubernetes.md §3). It never creates pods: the
operator does. It watches the Runs in the runs namespace and settles each run from its
Run's status and from what the runner reported, which the api announces with a
``run_reported`` control message; a sweep every 15 s catches anything missed.
"""

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
from exradius.db import make_engine, make_session_factory
from exradius.models import Event, utcnow
from exradius.routing import Router
from exradius.storage import make_store
from exradius.worker import run_objects
from exradius.worker.heartbeat import Heartbeat, operator_status, runtimes
from exradius.worker.intake import accept_answer, accept_task
from exradius.worker.kube import Cluster, Gone, KubeCluster
from exradius.worker.lifecycle import PendingEvent
from exradius.worker.presence import make_presence, new_worker_id
from exradius.worker.provider_test import ProviderTester
from exradius.worker.runs import RunManager
from exradius.worker.verify import ProjectVerifier

log = logging.getLogger("exradius.worker")

TARGET_KEYS = {"cancel": "run_id", "verify": "verification_id", "test_provider": "test_id", "run_reported": "run_id"}
"""Control actions and the key of the id each one carries."""
SWEEP_SECONDS = 15


class Worker:
    def __init__(self, settings: Settings, cluster: Cluster | None = None):
        self.settings = settings
        self.engine = make_engine(settings.database_url)
        self.sessions = make_session_factory(self.engine)
        self.store = make_store(settings)
        self.publisher: messaging.EventPublisher | None = None
        # Concurrency is the prefetch count: a task message stays unacknowledged (and is
        # not persisted) until a slot is free, so the broker holds the backlog. A slot is
        # held while a run prepares, runs or resumes; a waiting run gives it back.
        self.slots = asyncio.Semaphore(settings.worker_concurrency)
        self.cluster = cluster or KubeCluster(settings.runs_namespace, settings.kube_api_url,
                                              builds_namespace=settings.builds_namespace)
        self.runs = RunManager(settings, self.sessions, self.store, self.cluster, self.emit)
        self.router = Router(settings.openai_base_url)
        self.verifier = ProjectVerifier(self.runs)
        self.provider_tester = ProviderTester(self.runs)
        self._background: set[asyncio.Task] = set()
        self.worker_id = new_worker_id()
        self.runs.check_directory = self.check_directory
        self.runs.spawn = self._spawn
        self.runs.resume_later = lambda run_id: self._spawn(self._resume(run_id))
        self.presence = make_presence(self.engine, self.worker_id)
        self.heartbeat = Heartbeat(
            self.sessions, worker_id=self.worker_id, concurrency=settings.worker_concurrency, report=self.report,
        )
        self.runs.other_workers_alive = self.heartbeat.others_alive

    async def report(self) -> dict:
        return {"runtimes": runtimes(self.settings), "operator": await operator_status(self.cluster, self.settings)}

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
                await session.execute(update(Event).where(Event.id == pending.id).values(published_at=utcnow()))
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
                    self.sessions, self.store, message.body, box=self.runs.box, cache_dir=self.settings.cache_dir,
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
                await self.runs.execute(intake.run_id)

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

    async def _hold(self, run_id: uuid.UUID) -> None:
        """A run this worker took over at start: its slot until it waits or ends."""
        async with self.slots:
            await self.runs.hold(run_id)

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
            elif action == "run_reported":
                self._spawn(self.runs.settle(target))
            elif action == "verify":
                # Claimed before the ack, so a crash leaves a detectable owner.
                if await self.verifier.claim(target, self.worker_id):
                    log.info("verifying project (verification %s)", target)
                    self._spawn(self._verify(target))
            elif action == "test_provider":
                if await self.provider_tester.claim(target, self.worker_id):
                    log.info("testing provider (test %s)", target)
                    self._spawn(self._test_provider(target))

    # -- Runs -------------------------------------------------------------------------------

    async def recover(self) -> None:
        """Before consuming (§4): settle every unfinished run from its Run, fail the ones
        whose Run is gone, delete Runs nothing owns, fail verifications and provider tests
        a dead worker held, and resume waiting runs whose answer arrived meanwhile."""
        for run_id in await self.runs.recover():
            self._spawn(self._hold(run_id))
        await self.verifier.reconcile(self.presence.is_alive, self.worker_id)
        await self.provider_tester.reconcile(self.presence.is_alive, self.worker_id)
        await self._delete_stray_runs()
        for run_id in await self.runs.resumable_runs():
            self._spawn(self._resume(run_id))

    async def _delete_stray_runs(self) -> None:
        """Verify and probe Runs whose verification or test is no longer open."""
        items, _ = await self.cluster.list_runs()
        open_verifications = await self.verifier.open_ids()
        open_tests = await self.provider_tester.open_ids()
        for obj in items:
            name = obj.get("metadata", {}).get("name", "")
            parsed = run_objects.parse_name(name)
            if parsed is None:
                continue
            purpose, object_id = parsed
            if (purpose == "verify" and object_id not in open_verifications) or \
                    (purpose == "probe" and object_id not in open_tests):
                log.warning("deleting Run %s: nothing owns it", name)
                await self.runs._delete_run(name)

    async def watch(self) -> None:
        """List, then watch the Runs, forever; every event settles its run."""
        while True:
            try:
                items, version = await self.cluster.list_runs()
                for obj in items:
                    await self.runs.on_event("ADDED", obj)
                async for kind, obj in self.cluster.watch_runs(version):
                    if kind == "BOOKMARK":
                        continue
                    self._spawn(self.runs.on_event(kind, obj))
            except Gone:
                continue  # list again
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the API server restarted, a network blip
                log.exception("watching Runs failed; listing again in 2 s")
                await asyncio.sleep(2)

    async def sweep(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            try:
                await self.runs.sweep()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next sweep retries
                log.exception("sweep failed")

    # -- lifecycle ------------------------------------------------------------------------

    async def start(self) -> None:
        await self.store.ensure_bucket()
        connection = await messaging.connect(self.settings.effective_broker_url)
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
            self.heartbeat.start()
            await self.recover()
            watcher = asyncio.create_task(self.watch())
            sweeper = asyncio.create_task(self.sweep())

            await tasks_queue.consume(self.on_task)
            await answers_queue.consume(self.on_answer)
            await control_queue.consume(self.on_control)
            log.info(
                "worker %s ready (concurrency %s, Runs in %s)",
                self.worker_id[:8], self.settings.worker_concurrency, self.settings.runs_namespace,
            )
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            await stop.wait()
            log.info("shutting down")
            watcher.cancel()
            sweeper.cancel()
        await self.heartbeat.stop()
        await self.presence.stop()
        await self.engine.dispose()


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("aio_pika").setLevel(logging.WARNING)
    logging.getLogger("aiormq").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(Worker(get_settings()).start())


if __name__ == "__main__":
    run()
