"""Worker heartbeats into the ``workers`` table, for the status page (onboarding §4).

Recovery does not use this table; it uses the advisory-lock presence in ``presence.py``.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime

from sqlalchemy import delete

from exradius.db import SessionFactory
from exradius.models import Worker, utcnow
from exradius.version import package_version
from exradius.worker.containers import ContainerManager

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15
WORKER_TIMEOUT_SECONDS = 60


class Heartbeat:
    def __init__(
        self,
        sessions: SessionFactory,
        containers: ContainerManager,
        *,
        worker_id: str,
        concurrency: int,
        interval: float = HEARTBEAT_SECONDS,
    ):
        self.sessions = sessions
        self.containers = containers
        self.worker_id = worker_id
        self.concurrency = concurrency
        self.interval = interval
        self.started_at: datetime = utcnow()
        self.hostname = socket.gethostname()
        self._task: asyncio.Task | None = None

    async def beat(self) -> None:
        report = await self.containers.docker_report()
        async with self.sessions() as session:
            await session.merge(
                Worker(
                    id=self.worker_id,
                    hostname=self.hostname,
                    started_at=self.started_at,
                    last_seen_at=utcnow(),
                    concurrency=self.concurrency,
                    version=package_version(),
                    **report,
                )
            )
            await session.commit()

    async def _loop(self) -> None:
        while True:
            try:
                await self.beat()
            except Exception:  # a failed beat must never stop the worker
                log.exception("heartbeat failed")
            await asyncio.sleep(self.interval)

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
        try:
            async with self.sessions() as session:
                await session.execute(delete(Worker).where(Worker.id == self.worker_id))
                await session.commit()
        except Exception:  # noqa: BLE001 - best effort; the row ages out after 60 s
            log.warning("could not remove the heartbeat row")
