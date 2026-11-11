"""A project's config as the API and the workers see it: reading it with the host
credential, its secret store, and whether it can run (docs/config-repo.md §3, §6, §7).

A store hands its keys and values only to the projects the admin allowed for it
(``secret_store_projects``): ``assess`` reports a config naming another store as not
runnable, and ``store_values``, the one way to resolve values, takes the project and refuses.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exradius import capabilities, config_repo
from exradius.config_repo import ConfigReadError, ProjectConfigSnapshot
from exradius.crypto import SecretBox
from exradius.models import GitCredential, Project, SecretStore, SecretStoreEntry, SecretStoreProject
from exradius.resolve import StoreError, StoreValues
from exradius.schemas.project_config import ProjectConfig


async def host_auth(session: AsyncSession, box: SecretBox, url: str) -> str | None:
    """The credential for the config repository's host, or ``None`` (anonymous)."""
    host = config_repo.git_host(url)
    if host is None:
        return None
    row = await session.scalar(select(GitCredential).where(GitCredential.host == host))
    return config_repo.host_auth(row.kind, box.decrypt(row.value_encrypted)) if row else None


async def read_config(session: AsyncSession, box: SecretBox, cache_dir: Path, project: Project) -> ProjectConfigSnapshot:
    """Read the config afresh. A repository, branch or file that cannot be read gives a
    snapshot with that error and no commit."""
    auth = await host_auth(session, box, project.config_url)
    try:
        return await asyncio.to_thread(
            config_repo.read_snapshot,
            cache_dir,
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


async def find_store(session: AsyncSession, name: str | None) -> SecretStore | None:
    return await session.scalar(select(SecretStore).where(SecretStore.name == name)) if name else None


async def store_allows(session: AsyncSession, store_id: uuid.UUID, project_id: uuid.UUID) -> bool:
    """Whether the admin allowed the project to use the store (§7)."""
    return await session.scalar(
        select(SecretStoreProject.store_id)
        .where(SecretStoreProject.store_id == store_id, SecretStoreProject.project_id == project_id)
    ) is not None


def not_allowed(store: str) -> str:
    return f"secret store {store} is not allowed for this project"


def no_store(store: str) -> str:
    return f"secret store {store} does not exist"


async def store_keys(session: AsyncSession, store: SecretStore) -> set[str]:
    """Key names in the store."""
    return set(await session.scalars(select(SecretStoreEntry.key).where(SecretStoreEntry.store_id == store.id)))


async def store_values(session: AsyncSession, box: SecretBox, project_id: uuid.UUID,
                       config: ProjectConfig) -> StoreValues:
    """The values of the store the config names, decrypted now, for ``project_id``: the one
    way a run, a verification or the egress proxy gets secret values. A config that names
    no store gets none; a store that does not exist or does not allow the project raises
    ``StoreError`` with the reason. A config that references no secret needs no store."""
    name = config.secret_store
    values = StoreValues(store=name)
    if not name or not config.secret_names():
        return values
    store = await find_store(session, name)
    if store is None:
        raise StoreError(no_store(name))
    if not await store_allows(session, store.id, project_id):
        raise StoreError(not_allowed(name))
    rows = await session.execute(
        select(SecretStoreEntry.key, SecretStoreEntry.value_encrypted).where(SecretStoreEntry.store_id == store.id)
    )
    values.values = {key: box.decrypt(value) for key, value in rows.all()}
    return values


@dataclass
class Health:
    errors: list[str]
    store: str | None
    store_exists: bool
    store_allowed: bool = True
    """Whether the store allows the project (§7); ``False`` too when it does not exist."""
    required_keys: list[str] = field(default_factory=list)
    missing_keys: list[str] = field(default_factory=list)
    """``required_keys`` minus the store's keys (all of them when the store does not exist
    or does not allow the project)."""
    unsupported: list[str] = field(default_factory=list)
    """What the config needs that this version cannot run yet (``capabilities.py``)."""

    @property
    def reason(self) -> str | None:
        """Why tasks for the project are refused, in the documented words (§3)."""
        if self.errors:
            return f"config error: {self.errors[0]}"
        if self.unsupported:
            return self.unsupported[0]
        if not self.required_keys:
            return None
        if not self.store_exists:
            return no_store(self.store or "")
        if not self.store_allowed:
            return not_allowed(self.store or "")
        if self.missing_keys:
            plural = "s" if len(self.missing_keys) > 1 else ""
            return f"missing secret{plural} {', '.join(self.missing_keys)} in store {self.store}"
        return None

    @property
    def runnable(self) -> bool:
        return self.reason is None

    @property
    def status(self) -> str:
        """``ok``, ``config_errors``, ``store_not_allowed`` or ``missing_secrets``, for the
        panel; a config this version cannot run counts as ``config_errors``."""
        if self.errors or self.unsupported:
            return "config_errors"
        if self.runnable:
            return "ok"
        if self.store_exists and not self.store_allowed:
            return "store_not_allowed"
        return "missing_secrets"


def slug_errors(project: Project, snapshot: ProjectConfigSnapshot) -> list[str]:
    if snapshot.config is not None and snapshot.config.project.slug != project.slug:
        return [
            f"project.slug: the file says {snapshot.config.project.slug!r}, but this project was added as "
            f"{project.slug!r}; tasks keep naming {project.slug!r} until the file says it again"
        ]
    return []


async def assess(session: AsyncSession, project: Project, snapshot: ProjectConfigSnapshot) -> Health:
    store = await find_store(session, snapshot.store)
    allowed = store is not None and await store_allows(session, store.id, project.id)
    # A store that does not allow the project tells it nothing about its keys.
    keys = await store_keys(session, store) if store is not None and allowed else set()
    return Health(
        errors=[*snapshot.errors, *slug_errors(project, snapshot)],
        store=snapshot.store,
        store_exists=store is not None,
        store_allowed=allowed,
        required_keys=list(snapshot.required_keys),
        missing_keys=[k for k in snapshot.required_keys if k not in keys],
        unsupported=capabilities.unsupported(snapshot.config),
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
