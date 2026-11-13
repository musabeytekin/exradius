"""The bridge status report (design §9, integration-api.md §7): its shape, the
``only_if_changed`` check and the 5-minute heartbeat. ``Kit`` reports through it, and so
can a bridge that does not use the kit's flow (Exradius Desk)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime

from exradius_bridge_kit.exradius import ExradiusError, IntegrationApi
from exradius_bridge_kit.projects import Problem

log = logging.getLogger(__name__)

HEARTBEAT = 300.0
"""Seconds between status reports when nothing changes (integration-api.md §7)."""
MAX_PROBLEMS = 100
MAX_MESSAGE = 1000


class StatusReporter:
    def __init__(self, api: IntegrationApi, software: str, problems: Callable[[], list[Problem]],
                 *, heartbeat: float = HEARTBEAT):
        self.api = api
        self.software = software
        self.problems = problems
        self.heartbeat = heartbeat
        self.started_at = datetime.now(UTC)
        self._last: list[dict] | None = None
        self._task: asyncio.Task | None = None

    async def report(self, *, only_if_changed: bool = False) -> None:
        """Send the current problems; with ``only_if_changed``, only when they differ from
        the last report that went through. A failed report is logged, never raised."""
        problems = [{"project": p.project, "message": p.message[:MAX_MESSAGE]}
                    for p in self.problems()[:MAX_PROBLEMS]]
        if only_if_changed and problems == self._last:
            return
        report = {
            "version": 1,
            "software": self.software[:200],
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "problems": problems,
        }
        try:
            await self.api.report(report)
        except ExradiusError as exc:
            log.warning("could not report the bridge's status: %s", exc)
            return
        self._last = problems

    def start(self) -> None:
        """Report every ``heartbeat`` seconds until ``stop``."""
        if self._task is None:
            self._task = asyncio.create_task(self._beat())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat)
            await self.report()
