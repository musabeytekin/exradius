"""Which GitHub repositories belong to which Exradius projects, from the directory (design §2)."""

from __future__ import annotations

import re
from collections import defaultdict

from exradius_github.exradius import Directory, Project

_URL = re.compile(r"^(?:https?|ssh|git)://(?:[^@/]+@)?(?P<host>[^/:]+)(?::\d+)?/(?P<path>.+?)/?$")
_SCP = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>[^/].*?)/?$")


def repo_key(url: str, host: str) -> str | None:
    """``owner/name`` (lower case) for a repository URL on ``host``, in any of the forms
    git takes: ``https://host/owner/name.git``, ``ssh://git@host/owner/name``,
    ``git@host:owner/name.git``. None for another host or another shape."""
    match = _URL.match(url.strip()) or _SCP.match(url.strip())
    if not match or match["host"].lower() != host.lower():
        return None
    path = match["path"].removesuffix(".git")
    parts = [p for p in path.split("/") if p]
    if len(parts) != 2:
        return None
    return "/".join(parts).lower()


def projects_by_repo(directory: Directory, host: str) -> dict[str, list[Project]]:
    """Runnable projects with agents, by the repositories on ``host`` they hold."""
    out: dict[str, list[Project]] = defaultdict(list)
    for project in directory.projects:
        if not project.runnable or not project.agents:
            continue
        for repository in project.repositories:
            key = repo_key(repository.url, host)
            if key and project not in out[key]:
                out[key].append(project)
    return dict(out)
