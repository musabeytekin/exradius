"""The bridge's database (design §10)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from exradius_github.store import MIGRATIONS, Store, TaskRow, match_task


def test_match_task_takes_the_longest_id_at_a_boundary():
    ids = ["plane-1", "plane-1-2", "plane-10", "ops-1"]
    assert match_task("plane-1", ids) == "plane-1"
    assert match_task("plane-1-fix-route", ids) == "plane-1"
    assert match_task("plane-1-2-fix", ids) == "plane-1-2"
    assert match_task("plane-10/x", ids) == "plane-10"
    assert match_task("plane-100", ids) is None
    assert match_task("feature/plane-1", ids) is None


async def test_rows_expire_and_are_purged(tmp_path):
    now = [datetime(2026, 11, 17, tzinfo=UTC)]
    store = Store(tmp_path / "b.db", timedelta(days=30), clock=lambda: now[0])
    await store.open()
    await store.record_task("plane-1", "Acme/Plane", "acme/plane#1", "plane-1")
    [task] = await store.tasks_in("acme/plane")
    await store.record_pull_request("acme/plane", 5, task, "plane-1-x")
    now[0] += timedelta(days=20)
    await store.reviewed("acme/plane", 5, "mehmet")  # used again: lives 30 days from now
    now[0] += timedelta(days=15)
    assert await store.tasks_in("acme/plane") == []  # 35 days old: gone for reads at once
    row = await store.pull_request("acme/plane", 5)
    assert (row.task_id, row.reviewer) == ("plane-1", "mehmet")
    assert await store.purge() == 1
    now[0] += timedelta(days=30)
    assert await store.pull_request("acme/plane", 5) is None
    assert await store.purge() == 1
    assert await store.stats() == {"tasks": 0, "pull_requests": 0}
    await store.close()


async def test_the_schema_is_versioned_and_survives_a_restart(tmp_path):
    path = tmp_path / "b.db"
    store = Store(path, timedelta(days=1))
    await store.open()
    await store.record_task("plane-1", "acme/plane", "acme/plane#1", "plane-1")
    await store.close()
    again = Store(path, timedelta(days=1))
    await again.open()
    assert await again.tasks_in("acme/plane") == [TaskRow("plane-1", "acme/plane", "acme/plane#1", "plane-1")]
    await again.close()
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    db.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    db.close()
    with pytest.raises(RuntimeError, match="newer than this bridge knows"):
        await Store(path, timedelta(days=1)).open()
