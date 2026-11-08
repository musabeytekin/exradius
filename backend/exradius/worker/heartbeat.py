"""Worker heartbeats into the ``workers`` table, for the status page (onboarding §4).

Each beat records the runtime bundles this worker is configured with and the operator as
its leader Lease shows it (docs/kubernetes.md §15). Recovery of verifications and provider
tests uses the advisory-lock presence in ``presence.py``, not this table.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from exradius.config import Settings
from exradius.db import SessionFactory
from exradius.models import Worker, utcnow
from exradius.runtimes import RuntimeName
from exradius.version import package_version
from exradius.worker.kube import Cluster

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 15
WORKER_TIMEOUT_SECONDS = 60
LEASE_SLACK_SECONDS = 10
"""How long past its duration a Lease still counts as held (clock skew, a slow renewal)."""


def runtimes(settings: Settings) -> list[dict]:
    """The configured bundle image per runtime; pulls are the nodes' business."""
    return [
        {"name": name.value, "version": package_version(), "ready": True, "image": settings.runtime_image(name.value),
         "error": None}
        for name in RuntimeName
    ]


def _time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def lease_status(lease: dict | None, now: datetime | None = None) -> dict:
    """``alive`` when the Lease was renewed within its duration."""
    if lease is None:
        return {"alive": False, "holder": None, "renewed_at": None, "detail": "no leader Lease: the operator is not running"}
    spec = lease.get("spec") or {}
    renewed = _time(spec.get("renewTime")) or _time(spec.get("acquireTime"))
    duration = int(spec.get("leaseDurationSeconds") or 15)
    now = now or utcnow()
    alive = renewed is not None and now - renewed <= timedelta(seconds=duration + LEASE_SLACK_SECONDS)
    holder = spec.get("holderIdentity") or None
    return {
        "alive": alive and bool(holder),
        "holder": holder,
        "renewed_at": renewed.isoformat() if renewed else None,
        "detail": None if alive and holder else "the leader Lease has not been renewed",
    }


async def operator_status(cluster: Cluster, settings: Settings) -> dict:
    try:
        return lease_status(await cluster.get_lease(settings.namespace, settings.operator_lease))
    except Exception as exc:  # noqa: BLE001 - reported on the status page
        return {"alive": False, "holder": None, "renewed_at": None, "detail": f"{type(exc).__name__}: {exc}"}


class Heartbeat:
    def __init__(
        self,
        sessions: SessionFactory,
        *,
        worker_id: str,
        concurrency: int,
        report: Callable[[], Awaitable[dict]],
        interval: float = HEARTBEAT_SECONDS,
    ):
        self.sessions = sessions
        self.worker_id = worker_id
        self.concurrency = concurrency
        self.report = report
        """``runtimes`` and ``operator`` for the row."""
        self.interval = interval
        self.started_at: datetime = utcnow()
        self.hostname = socket.gethostname()
        self._task: asyncio.Task | None = None

    async def beat(self) -> None:
        report = await self.report()
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

    async def others_alive(self) -> bool:
        """Whether another worker beat within the timeout."""
        cutoff = utcnow() - timedelta(seconds=WORKER_TIMEOUT_SECONDS)
        async with self.sessions() as session:
            found = await session.scalar(
                select(Worker.id).where(Worker.id != self.worker_id, Worker.last_seen_at >= cutoff).limit(1)
            )
        return found is not None

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
