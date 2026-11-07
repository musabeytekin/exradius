"""A project's config as the API and the workers see it: reading it with the host
credential, its secret store, and whether it can run (docs/config-repo.md §3, §6, §7).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exradius import config_repo
from exradius.config_repo import ConfigReadError, ProjectConfigSnapshot
from exradius.crypto import SecretBox
from exradius.models import GitCredential, Project, SecretStore, SecretStoreEntry
from exradius.resolve import StoreValues


async def host_auth(session: AsyncSession, box: SecretBox, url: str) -> str | None:
    """The credential for the config repository's host, or ``None`` (anonymous)."""
    host = config_repo.git_host(url)
    if host is None:
        return None
    row = await session.scalar(select(GitCredential).where(GitCredential.host == host))
    return config_repo.host_auth(row.kind, box.decrypt(row.value_encrypted)) if row else None


async def read_config(session: AsyncSession, box: SecretBox, data_dir: Path, project: Project) -> ProjectConfigSnapshot:
    """Read the config afresh. A repository, branch or file that cannot be read gives a
    snapshot with that error and no commit."""
    auth = await host_auth(session, box, project.config_url)
    try:
        return await asyncio.to_thread(
            config_repo.read_snapshot,
            data_dir,
            project.id,
            project.config_url,
            branch=project.config_branch,
            path=project.config_path,
            auth=auth,
        )
    except ConfigReadError as exc:
        return ProjectConfigSnapshot.unreadable(str(exc))


def remember(project: Project, snapshot: ProjectConfigSnapshot) -> None:
    """Cache the snapshot on the row for the panel (the caller commits)."""
    project.snapshot = snapshot.to_json()
    project.snapshot_commit = snapshot.commit
    project.snapshot_read_at = snapshot.read_at


def cached(project: Project) -> ProjectConfigSnapshot | None:
    return ProjectConfigSnapshot.from_json(project.snapshot) if project.snapshot else None


async def store_keys(session: AsyncSession, name: str | None) -> set[str] | None:
    """Key names in the store, or ``None`` when it does not exist."""
    if not name:
        return None
    store = await session.scalar(select(SecretStore).where(SecretStore.name == name))
    if store is None:
        return None
    return set(await session.scalars(select(SecretStoreEntry.key).where(SecretStoreEntry.store_id == store.id)))


async def store_values(session: AsyncSession, box: SecretBox, name: str | None) -> StoreValues:
    """The store's values, decrypted now (an absent store has none)."""
    values = StoreValues(store=name)
    if not name:
        return values
    rows = await session.execute(
        select(SecretStoreEntry.key, SecretStoreEntry.value_encrypted)
        .join(SecretStore, SecretStore.id == SecretStoreEntry.store_id)
        .where(SecretStore.name == name)
    )
    values.values = {key: box.decrypt(value) for key, value in rows.all()}
    return values


@dataclass
class Health:
    errors: list[str]
    store: str | None
    store_exists: bool
    required_keys: list[str] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    """``required_keys`` minus the store's keys (all of them when it does not exist)."""

    @property
    def reason(self) -> str | None:
        """Why tasks for the project are refused, in the documented words (§3)."""
        if self.errors:
            return f"config error: {self.errors[0]}"
        if not self.required_keys:
            return None
        if not self.store_exists:
            return f"secret store {self.store} does not exist"
        if self.missing_keys:
            plural = "s" if len(self.missing_keys) > 1 else ""
            return f"missing secret{plural} {', '.join(self.missing_keys)} in store {self.store}"
        return None

    @property
    def runnable(self) -> bool:
        return self.reason is None

    @property
    def status(self) -> str:
        """``ok``, ``config_errors`` or ``missing_secrets``, for the panel."""
        if self.errors:
            return "config_errors"
        return "ok" if self.runnable else "missing_secrets"


def slug_errors(project: Project, snapshot: ProjectConfigSnapshot) -> list[str]:
    if snapshot.config is not None and snapshot.config.project.slug != project.slug:
        return [
            f"project.slug: the file says {snapshot.config.project.slug!r}, but this project was added as "
            f"{project.slug!r}; tasks keep naming {project.slug!r} until the file says it again"
        ]
    return []


async def assess(session: AsyncSession, project: Project, snapshot: ProjectConfigSnapshot) -> Health:
    keys = await store_keys(session, snapshot.store)
    return Health(
        errors=[*snapshot.errors, *slug_errors(project, snapshot)],
        store=snapshot.store,
        store_exists=keys is not None,
        required_keys=list(snapshot.required_keys),
        missing_keys=[k for k in snapshot.required_keys if k not in (keys or set())],
    )


@dataclass(frozen=True)
class AgentUse:
    project: str
    agent: str
    provider: str | None
    runtime: str

    @property
    def label(self) -> str:
        return f"{self.project}/{self.agent}"


async def agent_uses(session: AsyncSession) -> list[AgentUse]:
    """Every agent of every project's latest read config, for provider bookkeeping."""
    uses = []
    for project in (await session.scalars(select(Project).order_by(Project.slug))).all():
        snapshot = cached(project)
        if snapshot is None or snapshot.config is None:
            continue
        for slug, agent in snapshot.config.agents.items():
            uses.append(AgentUse(project.slug, slug, agent.provider, agent.runtime))
    return uses
