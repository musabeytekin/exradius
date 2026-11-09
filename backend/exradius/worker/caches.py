"""Per-project caches (docs/run-environment.md §5, docs/kubernetes.md §7.6).

Each ``workstation.cache`` entry is a directory inside the container, ``~``-relative or
absolute and outside ``/work`` (validated by the config). The worker names each one, and the
operator mounts ``caches/<name>`` of the project volume at its path in the setup commands
and the workstation; without a project volume it is ``caches/<name>`` of the run's own
workspace, an empty directory per run. ``prepare`` creates the directories as the run user.
"""

from __future__ import annotations

import re

HOME = "/home/runner"


def target_path(path: str, home: str = HOME) -> str:
    """The container path of a ``workstation.cache`` entry."""
    path = path.rstrip("/")
    return home + path[1:] if path.startswith("~") else path


def sanitise(path: str) -> str:
    """``~/.local/share/pnpm/store`` -> ``local_share_pnpm_store``."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.removeprefix("~")).strip("._-")
    return name[:80] or "cache"


def cache_specs(entries: list[str], home: str = HOME) -> list[dict[str, str]]:
    """The Run spec's ``workstation.caches``: each entry's path and its directory's name,
    ``<n>-<sanitised path>``, so a reordered list never mixes two caches up."""
    return [{"path": target_path(entry, home), "name": f"{index}-{sanitise(entry)}"}
            for index, entry in enumerate(entries)]
