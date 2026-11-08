"""The kit as a bridge uses it (design §5, §9, §10): feed it hand-overs and comments, run it.

```python
kit = Kit(tracker, KitSettings.from_env(), software="exradius-jira 0.2.0")
await kit.run()                    # starts the tracker, Exradius, the reconcile, the events
await kit.handed_over(ref)         # from the bridge's webhook listener
await kit.commented(ref)
```
"""

from __future__ import annotations

import asyncio
import logging
import time
import zlib
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from typing import Any

import httpx

from exradius_bridge_kit.exradius import Broker, ExradiusError, IntegrationApi
from exradius_bridge_kit.flow import Flow
from exradius_bridge_kit.projects import Problem, Projects
from exradius_bridge_kit.settings import KitSettings
from exradius_bridge_kit.tracker import ItemRef, Tracker, TrackerProblem

log = logging.getLogger(__name__)

STRIPES = 256
HEARTBEAT = 300.0
"""Seconds between status reports when nothing changes (integration-api.md §7)."""
RECENT_FAILURES = 5
FAILURES_FOR = 900.0
"""Seconds a failed hand-over, comment or event stays among the reported problems."""


def kit_version() -> str:
    try:
        return version("exradius-bridge-kit")
    except PackageNotFoundError:
        return "0+unknown"


class Kit:
    def __init__(
        self,
        tracker: Tracker,
        settings: KitSettings,
        *,
        software: str,
        broker: Any = None,
        api: IntegrationApi | None = None,
        downloads: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.tracker = tracker
        self.settings = settings
        self.software = f"{software} (exradius-bridge-kit {kit_version()})"
        self.broker = broker if broker is not None else Broker(settings.broker_url, tracker.bridge)
        self.api = api or IntegrationApi(settings.api_url, settings.integration_token)
        self.projects = Projects(tracker)
        self._downloads = downloads or httpx.AsyncClient(timeout=120.0, follow_redirects=True)
        self.flow = Flow(
            tracker=tracker,
            projects=self.projects,
            publisher=self.broker,
            max_attachment_bytes=settings.max_attachment_bytes,
            downloads=self._downloads,
        )
        self.started_at = datetime.now(UTC)
        self.tracker_problems: list[Problem] = []
        self._failures: deque[tuple[float, Problem]] = deque(maxlen=RECENT_FAILURES)
        self._clock = clock
        self._locks = [asyncio.Lock() for _ in range(STRIPES)]
        self._directory_lock = asyncio.Lock()
        self._work: set[asyncio.Task] = set()
        self._last_report: list[dict] | None = None
        self._heartbeat: asyncio.Task | None = None

    def lock(self, item_id: str) -> asyncio.Lock:
        return self._locks[zlib.crc32(item_id.encode()) % STRIPES]

    # -- what the bridge calls (§5) ----------------------------------------------------------

    async def handed_over(self, ref: ItemRef) -> None:
        """The item was assigned to the bot (or created assigned). Returns at once."""
        self._spawn(self._locked(ref, "hand-over", self.flow.handed_over))

    async def commented(self, ref: ItemRef) -> None:
        """A comment was written on the item. Returns at once."""
        self._spawn(self._locked(ref, "comment", self.flow.commented))

    async def idle(self) -> None:
        """Wait until every queued hand-over and comment is handled (tests, shutdown)."""
        while self._work:
            await asyncio.gather(*list(self._work), return_exceptions=True)

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._work.add(task)
        task.add_done_callback(self._work.discard)

    async def _locked(self, ref: ItemRef, what: str, action: Callable[[ItemRef], Awaitable[None]]) -> None:
        try:
            async with self.lock(ref.id):
                await action(ref)
        except Exception as exc:  # noqa: BLE001 - logged and reported; other items go on
            log.exception("%s of %s failed", what, ref.key)
            await self._failed(Problem(None, f"{ref.key}: the {what} failed: {exc or type(exc).__name__}"))

    async def _failed(self, problem: Problem) -> None:
        self._failures.append((self._clock(), problem))
        await self.report(only_if_changed=True)

    # -- Exradius (§10) ------------------------------------------------------------------------

    async def refresh_directory(self) -> None:
        async with self._directory_lock:
            self.projects.update(await self.api.directory())
        await self.report(only_if_changed=True)

    async def on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "directory_changed":
            bridges = (event.get("payload") or {}).get("bridges")
            if not bridges or self.tracker.bridge in bridges:
                await self.refresh_directory()
            return
        if kind not in ("status", "info_request", "result"):
            return
        ref = self.flow.matches(event)
        if ref is None:
            return  # another bridge's task, or another tracker's
        try:
            async with self.lock(ref.id):
                await self.flow.on_task_event(ref, event)
        except Exception as exc:
            await self._failed(Problem(None, f"{ref.key}: handling the {kind} event failed: {exc or type(exc).__name__}"))
            raise  # the broker retries once

    # -- status (§9) ---------------------------------------------------------------------------

    def problems(self) -> list[Problem]:
        now = self._clock()
        recent = [p for at, p in self._failures if now - at <= FAILURES_FOR]
        return [*self.tracker_problems, *self.projects.problems, *recent]

    async def report(self, *, only_if_changed: bool = False) -> None:
        problems = [{"project": p.project, "message": p.message[:1000]} for p in self.problems()[:100]]
        if only_if_changed and problems == self._last_report:
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
        self._last_report = problems

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT)
            await self.report()

    # -- running ----------------------------------------------------------------------------------

    async def start(self) -> None:
        """Start the tracker and Exradius, then catch up; ``run`` also consumes events.
        A tracker that cannot start is reported, then raised: the bridge stops."""
        try:
            await self.tracker.start()
        except TrackerProblem as exc:
            self.tracker_problems = [Problem(None, str(exc))]
            await self.report()
            raise
        self.tracker_problems = []
        await self.refresh_directory()  # reports the status for the first time
        await self.broker.connect()
        await self.reconcile()

    async def reconcile(self) -> None:
        for ref in await self.tracker.assigned_to_bot():
            await self._locked(ref, "reconcile", self.flow.reconcile)

    async def run(self) -> None:
        await self.start()
        await self.broker.consume(self.on_event)
        self._heartbeat = asyncio.create_task(self._beat())
        log.info("bridge %s is running for %s", self.tracker.bridge, self.tracker.instance)
        await asyncio.Event().wait()

    async def close(self) -> None:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
        await self.idle()
        await self.broker.close()
        await self.api.aclose()
        await self._downloads.aclose()
