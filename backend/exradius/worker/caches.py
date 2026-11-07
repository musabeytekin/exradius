"""Per-project caches (docs/run-environment.md §5).

Each ``workstation.cache`` entry is a directory inside the container, ``~``-relative or
absolute and outside ``/work``. It is bind-mounted from
``<data_dir>/caches/<project slug>/<n>-<sanitised path>/`` into the setup, run and resume
containers and into Verify. Every run of the project shares the directories, concurrent ones
included; nothing cleans them. The API reads their size for the project page, and deletes
them with the project.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

CACHES_DIR = "caches"


@dataclass(frozen=True)
class CacheMount:
    source: Path
    """On the worker's side of the data directory."""
    target: str
    """Absolute path inside the container."""


def target_path(path: str, home: str) -> str:
    """The container path of a ``workstation.cache`` entry (validated by the config)."""
    path = path.rstrip("/")
    return home + path[1:] if path.startswith("~") else path


def sanitise(path: str) -> str:
    """``~/.local/share/pnpm/store`` -> ``local_share_pnpm_store``."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.removeprefix("~")).strip("._-")
    return name[:80] or "cache"


def project_dir(data_dir: Path, slug: str) -> Path:
    return data_dir / CACHES_DIR / slug


def mounts(data_dir: Path, slug: str, entries: list[str], home: str) -> list[CacheMount]:
    base = project_dir(data_dir, slug)
    return [
        CacheMount(source=base / f"{index}-{sanitise(entry)}", target=target_path(entry, home))
        for index, entry in enumerate(entries)
    ]


def _chown(path: Path, uid: int, gid: int) -> None:
    if os.geteuid() == 0:
        os.lchown(path, uid, gid)


def ensure(caches: list[CacheMount], home_dir: Path, home: str, uid: int, gid: int) -> None:
    """Create the cache directories for the run user, and the mount points of those inside
    the home in the run's home directory: Docker would create missing ones as root, and the
    run user could not write next to them (``~/.cache/uv`` would leave ``~/.cache`` to root)."""
    for cache in caches:
        if not cache.source.is_dir():
            cache.source.mkdir(parents=True, exist_ok=True)
            _chown(cache.source, uid, gid)
        if not cache.target.startswith(home + "/"):
            continue
        current = home_dir
        for part in Path(cache.target[len(home) + 1:]).parts:
            current = current / part
            if current.is_symlink():
                break  # the run made it a link; Docker resolves it in the container
            if not current.exists():
                current.mkdir()
                _chown(current, uid, gid)


def size(path: Path) -> int:
    """Disk usage in bytes, as ``du`` counts it: allocated blocks, hard links once,
    symlinks not followed."""
    total = 0
    seen: set[tuple[int, int]] = set()
    for root, dirs, files in os.walk(path):
        for name in [*dirs, *files]:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            if info.st_nlink > 1:
                key = (info.st_dev, info.st_ino)
                if key in seen:
                    continue
                seen.add(key)
            total += getattr(info, "st_blocks", 0) * 512 or info.st_size
    return total


def project_size(data_dir: Path, slug: str) -> int | None:
    """The size of the project's caches; ``None`` when it has none on disk."""
    base = project_dir(data_dir, slug)
    return size(base) if base.is_dir() else None


def remove(data_dir: Path, slug: str) -> None:
    shutil.rmtree(project_dir(data_dir, slug), ignore_errors=True)


class SizeMemo:
    """The API's view of ``project_size``: one walk of a large cache takes seconds where the
    data directory is a Docker Desktop file share (75,000 files, 4 s). A size younger than
    ``ttl`` is returned as is; an older one is returned at once and refreshed in the
    background; only a project never measured waits for the walk."""

    def __init__(self, ttl: float = 60.0, clock=time.monotonic):
        self.ttl = ttl
        self.clock = clock
        self._values: dict[tuple[Path, str], tuple[int, float]] = {}
        self._walks: dict[tuple[Path, str], asyncio.Task] = {}

    def _walk(self, key: tuple[Path, str]) -> asyncio.Task:
        if key not in self._walks:
            async def measure() -> int | None:
                try:
                    value = await asyncio.to_thread(project_size, *key)
                    if value is None:  # no caches yet: cheap to ask again
                        self._values.pop(key, None)
                    else:
                        self._values[key] = (value, self.clock())
                    return value
                finally:
                    self._walks.pop(key, None)

            self._walks[key] = asyncio.create_task(measure())
        return self._walks[key]

    async def get(self, data_dir: Path, slug: str) -> int | None:
        key = (data_dir, slug)
        known = self._values.get(key)
        if known is None:
            return await self._walk(key)
        if self.clock() - known[1] >= self.ttl:
            self._walk(key)
        return known[0]

    def forget(self, data_dir: Path, slug: str) -> None:
        self._values.pop((data_dir, slug), None)


sizes = SizeMemo()
"""Shared by the API's requests."""
