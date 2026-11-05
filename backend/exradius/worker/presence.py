"""Worker liveness for crash recovery.

Each worker process gets a fresh id and, on Postgres, holds a session-level advisory
lock keyed by that id on a dedicated connection for as long as it lives. The lock goes
away with the connection when the process dies. Another worker tests liveness with
``pg_try_advisory_xact_lock``: if it can take the lock, the owner is gone.
"""

from __future__ import annotations

import hashlib
import logging
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

log = logging.getLogger(__name__)


def lock_key(worker_id: str) -> int:
    digest = hashlib.sha256(f"exradius-worker:{worker_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def new_worker_id() -> str:
    return uuid.uuid4().hex


class PostgresPresence:
    def __init__(self, engine: AsyncEngine, worker_id: str):
        self.engine = engine
        self.worker_id = worker_id
        self._connection: AsyncConnection | None = None

    async def start(self) -> None:
        self._connection = await self.engine.connect()
        await self._connection.execute(
            text("SELECT pg_advisory_lock(:key)"), {"key": lock_key(self.worker_id)}
        )
        await self._connection.commit()  # the session-level lock outlives the transaction

    async def is_alive(self, worker_id: str) -> bool:
        if worker_id == self.worker_id:
            return True
        async with self.engine.begin() as connection:
            free = await connection.scalar(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_key(worker_id)}
            )
        return not free

    async def stop(self) -> None:
        if self._connection is not None:
            await self._connection.close()


class LocalPresence:
    """Single-process fallback (e.g. SQLite): only this worker is alive."""

    def __init__(self, worker_id: str):
        self.worker_id = worker_id

    async def start(self) -> None:
        return None

    async def is_alive(self, worker_id: str) -> bool:
        return worker_id == self.worker_id

    async def stop(self) -> None:
        return None


def make_presence(engine: AsyncEngine, worker_id: str) -> PostgresPresence | LocalPresence:
    if engine.dialect.name == "postgresql":
        return PostgresPresence(engine, worker_id)
    log.warning("not on Postgres: assuming this is the only worker for crash recovery")
    return LocalPresence(worker_id)
