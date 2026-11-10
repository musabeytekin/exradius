"""Agent labels: one per agent of a repository's project, kept in sync (design §3.1).

A label is the bridge's when its description reads ``Exradius <project>/<agent>``; that is
also how a label maps back to slugs. Labels without it are never touched.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Literal

from exradius_github.exradius import Directory, Project
from exradius_github.github import GitHubApp, Repo
from exradius_github.repos import projects_by_repo

log = logging.getLogger(__name__)

PREFIX = "agent: "
COLOR = "5319e7"
MAX_NAME = 50  # GitHub's limit on a label's name
_MARKER = re.compile(r"^Exradius ([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*)$")


def marker(description: str | None) -> tuple[str, str] | None:
    match = _MARKER.match((description or "").strip())
    return (match[1], match[2]) if match else None


def description(project: str, agent: str) -> str:
    return f"Exradius {project}/{agent}"


def _labels(items: list[tuple[str, str]]) -> dict[str, str]:
    """slug -> display text: the name, or ``Name (slug)`` when another item shares it."""
    counts = Counter(name.casefold() for _, name in items)
    return {slug: name if counts[name.casefold()] == 1 else f"{name} ({slug})" for slug, name in items}


def _fit(name: str) -> str:
    return name if len(name) <= MAX_NAME else name[: MAX_NAME - 1] + "…"


@dataclass(frozen=True)
class Wanted:
    name: str
    project: str
    agent: str
    shown: str
    """``Plane · Developer``, how comments name the agent."""

    @property
    def description(self) -> str:
        return description(self.project, self.agent)


def wanted(directory: Directory, host: str) -> dict[str, list[Wanted]]:
    """repo -> the agent labels it should carry."""
    project_names = _labels([(p.slug, p.name) for p in directory.projects])
    out: dict[str, list[Wanted]] = {}
    for repo, projects in projects_by_repo(directory, host).items():
        several = len(projects) > 1
        labels: list[Wanted] = []
        used: set[str] = set()
        for project in projects:
            agent_names = _labels([(a.slug, a.name) for a in project.agents])
            for agent in project.agents:
                base = f"{project_names[project.slug]} · {agent_names[agent.slug]}" if several else agent_names[agent.slug]
                name = _fit(PREFIX + base)
                if name.casefold() in used:
                    log.warning("label %r for %s/%s would repeat another in %s; skipped", name, project.slug, agent.slug, repo)
                    continue
                used.add(name.casefold())
                labels.append(Wanted(name, project.slug, agent.slug, f"{project.name} · {agent.name}"))
        out[repo] = labels
    return out


@dataclass(frozen=True)
class Choice:
    project: str
    agent: str
    shown: str
    label: str
    """The label that started the run, removed when it ends."""


@dataclass(frozen=True)
class Resolution:
    kind: Literal["ok", "unavailable"]
    choice: Choice | None = None


def resolve(label: dict, repo: str, directory: Directory, host: str) -> Resolution | None:
    """Map a label on an issue in ``repo`` to the agent to run. None: not the bridge's label."""
    slugs = marker(label.get("description"))
    if slugs is None:
        return None
    projects = projects_by_repo(directory, host).get(repo.lower(), [])
    project: Project | None = next((p for p in projects if p.slug == slugs[0]), None)
    agent = next((a for a in project.agents if a.slug == slugs[1]), None) if project else None
    if project is None or agent is None:
        return Resolution("unavailable")
    return Resolution("ok", Choice(project.slug, agent.slug, f"{project.name} · {agent.name}", str(label.get("name") or "")))


class Labels:
    def __init__(self, app: GitHubApp, host: str):
        self.app = app
        self.host = host
        self._lock = asyncio.Lock()
        self._synced: set[str] = set()

    async def sync(self, directory: Directory) -> None:
        """Every repo in scope gets its labels. Repos that left the directory since this
        process last synced them lose theirs."""
        async with self._lock:
            targets = wanted(directory, self.host)
            for repo in self._synced - set(targets):
                targets[repo] = []
            for repo, labels in targets.items():
                installation = await self.app.installation_for(repo)
                if installation is None:
                    log.info("the App is not installed on %s; no agent labels there", repo)
                    continue
                await self._sync_repo(self.app.repo(repo, installation), labels)
                if labels:
                    self._synced.add(repo)
                else:
                    self._synced.discard(repo)

    async def _sync_repo(self, repo: Repo, labels: list[Wanted]) -> None:
        existing = await repo.labels()
        ours = {marker(l.get("description")): l for l in existing if marker(l.get("description"))}
        others = {str(l["name"]).casefold() for l in existing if not marker(l.get("description"))}
        keep = {(w.project, w.agent) for w in labels}
        for key, label in ours.items():
            if key not in keep:
                await repo.delete_label(label["name"])
        for w in labels:
            label = ours.get((w.project, w.agent))
            if label is None:
                if w.name.casefold() in others:
                    log.warning("%s already has a label %r of its own; not adding the agent label", repo.name, w.name)
                    continue
                await repo.create_label(w.name, COLOR, w.description)
            elif label["name"] != w.name or str(label.get("color", "")).lower() != COLOR:
                await repo.update_label(label["name"], w.name, COLOR, w.description)
