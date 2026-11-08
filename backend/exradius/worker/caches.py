"""Per-project caches (docs/run-environment.md §5, docs/kubernetes.md §7.6).

Each ``workstation.cache`` entry is a directory inside the container, ``~``-relative or
absolute and outside ``/work``. With the project volume (phase 3) each becomes the subPath
``caches/<n>-<sanitised path>`` of that volume, mounted at its path in the setup and
workstation containers. Until then ``workstation.cache`` is accepted and ignored with a
warning (``exradius/capabilities.py``). Path validation and naming live here.
"""

from __future__ import annotations

import re


def target_path(path: str, home: str) -> str:
    """The container path of a ``workstation.cache`` entry (validated by the config)."""
    path = path.rstrip("/")
    return home + path[1:] if path.startswith("~") else path


def sanitise(path: str) -> str:
    """``~/.local/share/pnpm/store`` -> ``local_share_pnpm_store``."""
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", path.removeprefix("~")).strip("._-")
    return name[:80] or "cache"
