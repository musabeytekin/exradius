"""The config hook (docs/integration-api.md §2): a git host calls a per-project URL on push,
and Exradius re-reads the config at once.

The hook ignores the body and the headers and checks no host-specific signatures: the
token in the URL identifies the project, and the hook does one thing, the panel's re-read,
with the config fetched by Exradius itself. So it works with any git host, and a forged or
replayed call can only cause an extra re-read.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Response, status
from sqlalchemy import select

from exradius.api.config_routes import _reread
from exradius.api.deps import DB, AppState, State
from exradius.models import Project, utcnow

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/hooks")


class Rereads:
    """Background re-reads, one per project at a time. A request while that project's read
    is running marks one more read to run after it, so a burst of calls causes at most two
    reads."""

    def __init__(self, read: Callable[[uuid.UUID], Awaitable[None]]):
        self._read = read
        self._again: dict[uuid.UUID, bool] = {}
        """Projects with a read running, and whether another one is due after it."""
        self._tasks: set[asyncio.Task] = set()

    def request(self, project_id: uuid.UUID) -> None:
        if project_id in self._again:
            self._again[project_id] = True
            return
        self._again[project_id] = False
        task = asyncio.create_task(self._loop(project_id))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def running(self, project_id: uuid.UUID) -> bool:
        return project_id in self._again

    async def _loop(self, project_id: uuid.UUID) -> None:
        try:
            while True:
                try:
                    await self._read(project_id)
                except Exception:  # noqa: BLE001 - logged; the next push reads again
                    log.exception("config hook: re-reading project %s failed", project_id)
                if not self._again.get(project_id):
                    return
                self._again[project_id] = False
        finally:
            self._again.pop(project_id, None)

    async def idle(self) -> None:
        """Wait for every read requested so far (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*list(self._tasks), return_exceptions=True)


def make_rereads(state: AppState) -> Rereads:
    async def read(project_id: uuid.UUID) -> None:
        async with state.sessions() as session:
            project = await session.get(Project, project_id)
            if project is None:  # deleted meanwhile
                return
            await _reread(session, state, project)  # the panel's re-read

    return Rereads(read)


@router.post("/config/{token}", status_code=status.HTTP_202_ACCEPTED, response_class=Response)
async def config_hook(token: str, session: DB, state: State):
    """Accept the call and re-read the project's config in the background: ``202`` before
    reading, ``404`` for an unknown token. Any body, any content type."""
    project = await session.scalar(select(Project).where(Project.hook_token == token))
    if project is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown hook")
    project.hook_last_at = utcnow()
    await session.commit()
    state.rereads.request(project.id)
    return Response(status_code=status.HTTP_202_ACCEPTED)
