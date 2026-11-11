"""The directory of projects for bridges, and the notice sent when it changes
(docs/integration-api.md §3, §4).

The directory is built from the snapshots cached on ``projects.snapshot``, so it is only
for offering choices: intake still reads the config afresh. ``check`` runs after every
write that can change it (a config read, a project added or deleted, store keys set or
deleted, a store's allowed projects changed, a store deleted), in whichever process made the
write, and publishes
``directory.changed`` only when the directory differs from the one last published.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from exradius.config_repo import ProjectConfigSnapshot
from exradius.db import SessionFactory
from exradius.models import DirectoryState, Project, utcnow
from exradius.project_state import assess, cached
from exradius.schemas.messages import DirectoryChangedPayload, DirectoryEvent
from exradius.secret_backends import SecretBackends

log = logging.getLogger(__name__)

STATE_ID = 1
EMPTY: dict[str, Any] = {"projects": []}

Publish = Callable[[DirectoryEvent], Awaitable[None]]


def without_userinfo(url: str) -> str:
    """``url`` with any ``user:password@`` removed from its authority
    (``https://user:token@host/x`` gives ``https://host/x``), otherwise as written. URLs
    without a scheme, such as the scp-like ``git@host:path``, cannot carry a password and
    stay as they are."""
    scheme, sep, rest = url.partition("://")
    if not sep or not scheme:
        return url
    end = min((i for i in (rest.find(c) for c in "/?#") if i != -1), default=len(rest))
    authority = rest[:end]
    if "@" not in authority:
        return url
    return f"{scheme}://{authority.rsplit('@', 1)[1]}{rest[end:]}"


async def build(session: AsyncSession, backends: SecretBackends | None = None) -> dict[str, Any]:
    """The full directory: projects by slug, each with its name, ``runnable`` (the project
    health's), its repositories in config order, its agents by slug (names and repository
    URLs only) and every bridge file of its config. A bridge sees its ``view`` of it.

    A store whose backend cannot be reached leaves ``runnable`` true: an outage is not the
    project's configuration, and nothing would publish the directory again once it is over
    (intake rejects tasks with the reason while it lasts)."""
    backends = backends or SecretBackends()
    entries = []
    for project in (await session.scalars(select(Project))).all():
        snapshot = cached(project) or ProjectConfigSnapshot.unreadable("the config has not been read yet")
        health = await assess(session, project, snapshot, backends, retry=False)
        config = snapshot.config
        entries.append({
            "slug": project.slug,
            "name": config.project.display_name if config is not None else project.slug,
            "runnable": health.runnable or health.store_unreachable,
            # A config with errors has no repositories or agents to list (§4).
            "repositories": [] if config is None else [
                {"name": repo.name, "url": without_userinfo(repo.url)} for repo in config.repositories
            ],
            "agents": [] if config is None else [
                {"slug": slug, "name": config.agent_name(slug)} for slug in sorted(config.agents)
            ],
            # Read even when the config has errors (config-repo.md §2.1).
            "bridges": {name: snapshot.bridges[name] for name in sorted(snapshot.bridges)},
        })
    return {"projects": sorted(entries, key=lambda e: e["slug"])}


def view(directory: dict[str, Any], bridge: str) -> dict[str, Any]:
    """What a token bound to ``bridge`` reads (integration-api.md §4): the projects whose
    config has ``bridges/<bridge>.yaml``, each with that file only."""
    projects = []
    for entry in directory.get("projects", []):
        file = (entry.get("bridges") or {}).get(bridge)
        if file is None:
            continue
        projects.append({
            **{k: v for k, v in entry.items() if k != "bridges"},
            "bridge_config": file.get("config"),
            "bridge_config_error": file.get("error"),
        })
    return {"bridge": bridge, "projects": projects}


def bridge_names(directory: dict[str, Any]) -> list[str]:
    """Every bridge that some project's config has a file for."""
    return sorted({name for entry in directory.get("projects", []) for name in entry.get("bridges") or {}})


def changed_bridges(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Bridges whose view differs."""
    names = set(bridge_names(previous)) | set(bridge_names(current))
    return sorted(name for name in names if view(previous, name) != view(current, name))


def encode(directory: dict[str, Any]) -> bytes:
    """The response body; equal directories give equal bytes (and so equal ETags)."""
    return json.dumps(directory, separators=(",", ":"), ensure_ascii=False).encode()


def etag(body: bytes) -> str:
    return f'"{hashlib.sha256(body).hexdigest()}"'


def changed_projects(previous: dict[str, Any], current: dict[str, Any]) -> list[str]:
    """Slugs whose entry was added, changed or removed."""
    before = {p["slug"]: p for p in previous.get("projects", [])}
    after = {p["slug"]: p for p in current.get("projects", [])}
    return sorted(slug for slug in before.keys() | after.keys() if before.get(slug) != after.get(slug))


async def _locked_state(session: AsyncSession) -> DirectoryState:
    """The single row, locked; created when missing (the migration inserts it, tests that
    create the schema from the models do not)."""
    state = await session.get(DirectoryState, STATE_ID, with_for_update=True, populate_existing=True)
    if state is not None:
        return state
    session.add(DirectoryState(id=STATE_ID, directory=None))
    try:
        await session.commit()
    except IntegrityError:  # another process created it first
        await session.rollback()
    state = await session.get(DirectoryState, STATE_ID, with_for_update=True, populate_existing=True)
    assert state is not None
    return state


async def check(sessions: SessionFactory, publish: Publish, backends: SecretBackends | None = None) -> bool:
    """Compare the directory with the one last published, under the row lock, and when it
    differs publish the notice, then store the new directory. A failed publish stores
    nothing, so the next check publishes again. Never raises: the write that led here is
    already committed. Returns whether a notice was published.

    The directory is built once the lock is held, so concurrent checks see each other's
    stored result and a slower one cannot store an older directory over a newer one."""
    try:
        async with sessions() as session:
            state = await _locked_state(session)
            current = await build(session, backends)
            previous = state.directory or EMPTY
            if previous == current:
                await session.commit()
                return False
            event = DirectoryEvent(payload=DirectoryChangedPayload(
                projects=changed_projects(previous, current), bridges=changed_bridges(previous, current),
            ))
            try:
                await publish(event)
            except Exception as exc:  # noqa: BLE001 - logged; the next check publishes again
                log.warning("could not publish directory.changed (%s: %s); the next check retries",
                            type(exc).__name__, exc)
                await session.rollback()
                return False
            state.directory = current
            state.published_at = utcnow()
            await session.commit()
            return True
    except Exception:  # noqa: BLE001 - logged, the caller's work stands
        log.exception("directory check failed")
        return False
