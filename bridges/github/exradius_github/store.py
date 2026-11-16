"""The bridge's own small database (design §10): SQLite in one file on the pod's volume.

It remembers what GitHub cannot tell the bridge later, each row for a limited time (``ttl``,
renewed when the row is used again):

- ``tasks``: every task the bridge published, with the issue it came from, so a pull request
  whose branch starts with a task id can be traced back to it;
- ``pull_requests``: the pull requests traced that way, and who last requested changes.

The bridge runs as one replica, so one connection serves it; calls run in a worker thread
so the event loop never waits on the disk. The schema is versioned with ``PRAGMA
user_version``; each entry of ``MIGRATIONS`` moves it one version up.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

MIGRATIONS: list[str] = [
    # 1: tasks and the pull requests traced to them.
    """
    CREATE TABLE tasks (
        task_id    TEXT PRIMARY KEY,
        repo       TEXT NOT NULL,
        item_id    TEXT NOT NULL,
        item_key   TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE INDEX tasks_repo ON tasks (repo);
    CREATE INDEX tasks_expires ON tasks (expires_at);
    CREATE TABLE pull_requests (
        repo        TEXT NOT NULL,
        number      INTEGER NOT NULL,
        task_id     TEXT NOT NULL,
        item_id     TEXT NOT NULL,
        item_key    TEXT NOT NULL,
        head_branch TEXT NOT NULL,
        reviewer    TEXT,
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        PRIMARY KEY (repo, number)
    );
    CREATE INDEX pull_requests_expires ON pull_requests (expires_at);
    """,
]


@dataclass(frozen=True)
class TaskRow:
    task_id: str
    repo: str
    item_id: str
    item_key: str


@dataclass(frozen=True)
class PullRequestRow:
    repo: str
    number: int
    task_id: str
    item_id: str
    """The issue the task came from (``owner/repo#12``)."""
    item_key: str
    head_branch: str
    reviewer: str | None


def match_task(branch: str, task_ids: list[str]) -> str | None:
    """The task a branch belongs to: the longest task id the branch starts with, followed by
    nothing or by a character that cannot continue the id's run number (``plane-1`` owns
    ``plane-1-fix`` and ``plane-1/x`` but not ``plane-10``)."""
    best = None
    for task_id in task_ids:
        if branch == task_id or (branch.startswith(task_id) and not branch[len(task_id)].isdigit()):
            if best is None or len(task_id) > len(best):
                best = task_id
    return best


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path, ttl: timedelta, *, clock: Callable[[], datetime] | None = None):
        self.path = str(path)
        self.ttl = ttl
        self._clock = clock or (lambda: datetime.now(UTC))
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    # -- lifecycle ---------------------------------------------------------------------------

    async def open(self) -> None:
        await asyncio.to_thread(self._open)

    def _open(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA busy_timeout=5000")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version > len(MIGRATIONS):
            raise RuntimeError(f"{self.path} has schema version {version}, newer than this bridge knows "
                               f"({len(MIGRATIONS)})")
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            db.executescript(f"BEGIN; {script}; PRAGMA user_version = {number}; COMMIT;")
        self._db = db

    async def close(self) -> None:
        if self._db is not None:
            await asyncio.to_thread(self._db.close)
            self._db = None

    async def _run(self, work: Callable[[sqlite3.Connection], T]) -> T:
        def locked() -> T:
            assert self._db is not None, "open() first"
            with self._lock:
                return work(self._db)
        return await asyncio.to_thread(locked)

    def _times(self) -> tuple[str, str]:
        now = self._clock()
        return _iso(now), _iso(now + self.ttl)

    # -- tasks -------------------------------------------------------------------------------

    async def record_task(self, task_id: str, repo: str, item_id: str, item_key: str) -> None:
        now, expires = self._times()
        await self._run(lambda db: db.execute(
            "INSERT INTO tasks (task_id, repo, item_id, item_key, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (task_id) DO UPDATE SET expires_at = excluded.expires_at",
            (task_id, repo.lower(), item_id, item_key, now, expires)))

    async def tasks_in(self, repo: str) -> list[TaskRow]:
        now, _ = self._times()
        rows = await self._run(lambda db: db.execute(
            "SELECT task_id, repo, item_id, item_key FROM tasks WHERE repo = ? AND expires_at > ?",
            (repo.lower(), now)).fetchall())
        return [TaskRow(**dict(r)) for r in rows]

    # -- pull requests -----------------------------------------------------------------------

    async def record_pull_request(self, repo: str, number: int, task: TaskRow, head_branch: str) -> PullRequestRow:
        now, expires = self._times()
        await self._run(lambda db: db.execute(
            "INSERT INTO pull_requests (repo, number, task_id, item_id, item_key, head_branch, created_at, updated_at, "
            "expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT (repo, number) DO UPDATE SET "
            "task_id = excluded.task_id, item_id = excluded.item_id, item_key = excluded.item_key, "
            "head_branch = excluded.head_branch, updated_at = excluded.updated_at, expires_at = excluded.expires_at",
            (repo.lower(), number, task.task_id, task.item_id, task.item_key, head_branch, now, now, expires)))
        found = await self.pull_request(repo, number)
        assert found is not None
        return found

    async def pull_request(self, repo: str, number: int) -> PullRequestRow | None:
        now, _ = self._times()
        row = await self._run(lambda db: db.execute(
            "SELECT repo, number, task_id, item_id, item_key, head_branch, reviewer FROM pull_requests "
            "WHERE repo = ? AND number = ? AND expires_at > ?", (repo.lower(), number, now)).fetchone())
        return PullRequestRow(**dict(row)) if row else None

    async def reviewed(self, repo: str, number: int, reviewer: str) -> None:
        """Who requested changes last; the row lives ``ttl`` from now again."""
        now, expires = self._times()
        await self._run(lambda db: db.execute(
            "UPDATE pull_requests SET reviewer = ?, updated_at = ?, expires_at = ? WHERE repo = ? AND number = ?",
            (reviewer, now, expires, repo.lower(), number)))

    # -- expiry ------------------------------------------------------------------------------

    async def purge(self) -> int:
        """Delete the expired rows; how many went."""
        now, _ = self._times()

        def work(db: sqlite3.Connection) -> int:
            gone = db.execute("DELETE FROM tasks WHERE expires_at <= ?", (now,)).rowcount
            return gone + db.execute("DELETE FROM pull_requests WHERE expires_at <= ?", (now,)).rowcount
        return await self._run(work)

    async def stats(self) -> dict[str, Any]:
        def work(db: sqlite3.Connection) -> dict[str, Any]:
            return {"tasks": db.execute("SELECT count(*) FROM tasks").fetchone()[0],
                    "pull_requests": db.execute("SELECT count(*) FROM pull_requests").fetchone()[0]}
        return await self._run(work)
