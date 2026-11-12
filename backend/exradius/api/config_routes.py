"""Admin API for configuration (docs/config-repo.md §11): projects as config repositories,
config repository access per git host, secret stores, the catalog, the resolved agent
view and project verification. Nothing about a project is edited here: its config file
is the only truth, and the API only reads it."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import uuid

import yaml
from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import delete, func, select

from exradius import catalog, config_repo, directory
from exradius.api import integration_routes
from exradius.api.deps import DB, Admin, AppState, State, get_or_404
from exradius.config_repo import ProjectConfigSnapshot
from exradius.crypto import new_hook_token
from exradius.environment import config_environment_text, workstation_label
from exradius.models import (
    AdminUser,
    GitCredential,
    Project,
    ProjectVerification,
    Provider,
    SecretStore,
    SecretStoreChange,
    SecretStoreEntry,
    SecretStoreProject,
    Task,
    utcnow,
)
from exradius.project_state import assess, cached, find_store, read_config, remember
from exradius.providers import provider_problem, resolve_provider
from exradius.resolve import connection_view
from exradius.schemas import api as s
from exradius.schemas.project_config import NAME_PATTERN, SECRET_REF
from exradius.secret_backends import StoreUnreachable, kinds

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")
NO_CONTENT = status.HTTP_204_NO_CONTENT
SECRET_KEY = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
OPEN_VERIFICATION = ("pending", "running")


# Projects ----------------------------------------------------------------------------


def freshness(latest: ProjectVerification | None, commit: str | None) -> str:
    """setup-and-verify.md §1.2."""
    if latest is None:
        return "never"
    if latest.state in OPEN_VERIFICATION:
        return "running"
    if latest.state == "failed":
        return "failed"
    return "current" if commit is not None and latest.commit == commit else "stale"


async def _latest_verification(session, project_id: uuid.UUID) -> ProjectVerification | None:
    return await session.scalar(
        select(ProjectVerification)
        .where(ProjectVerification.project_id == project_id)
        .order_by(ProjectVerification.created_at.desc())
        .limit(1)
    )


def hook_url(state: AppState, project: Project) -> str:
    """The config hook URL (docs/integration-api.md §2)."""
    return f"{state.settings.public_api_url.rstrip('/')}/api/hooks/config/{project.hook_token}"


async def check_directory(state: AppState) -> None:
    """The directory check after a write that can change it (docs/integration-api.md §3)."""
    await directory.check(state.sessions, state.broker.publish_directory, state.secrets)


async def _project_out(session, state: AppState, project: Project) -> s.ProjectOut:
    snapshot = cached(project) or ProjectConfigSnapshot.unreadable("the config has not been read yet")
    latest = await _latest_verification(session, project.id)
    health = await assess(session, project, snapshot, state.secrets, retry=False)
    config = snapshot.config
    store = await find_store(session, snapshot.store)
    return s.ProjectOut(
        id=project.id,
        slug=project.slug,
        name=config.project.display_name if config else project.slug,
        description=config.project.description if config else "",
        config_url=project.config_url,
        config_branch=project.config_branch,
        config_path=project.config_path,
        commit=snapshot.commit,
        read_at=snapshot.read_at if project.snapshot else None,
        branch=snapshot.branch,
        path=snapshot.path,
        health=health.status,
        runnable=health.runnable,
        reason=health.reason,
        errors=[*health.errors, *health.unsupported],
        warnings=[*snapshot.warnings, *await _provider_warnings(session, snapshot)],
        store=snapshot.store,
        store_id=store.id if store else None,
        store_exists=health.store_exists,
        store_allowed=health.store_allowed,
        store_error=health.store_error,
        required_keys=[s.RequiredKeyOut(name=k, present=None if health.store_error else k not in health.missing_keys)
                       for k in snapshot.required_keys],
        missing_keys=health.missing_keys,
        agents=list(config.agents) if config else [],
        config=config.model_dump(mode="json") if config else None,
        raw=snapshot.raw,
        created_at=project.created_at,
        verification=s.VerificationSummary.model_validate(latest) if latest else None,
        freshness=freshness(latest, project.snapshot_commit),
        hook_url=hook_url(state, project),
        hook_last_at=project.hook_last_at,
        bridges=[s.BridgeFileOut(name=name, config=file.get("config"), error=file.get("error"))
                 for name, file in sorted(snapshot.bridges.items())],
        bridge_problems=await integration_routes.bridge_problems(session, project.slug),
    )


async def _provider_warnings(session, snapshot: ProjectConfigSnapshot) -> list[str]:
    """Agents naming a provider that does not exist (the panel manages providers, the
    file only names them)."""
    if snapshot.config is None:
        return []
    named = {a.provider for a in snapshot.config.agents.values() if a.provider}
    known = set(await session.scalars(select(Provider.slug).where(Provider.slug.in_(named)))) if named else set()
    return [
        f"agents.{slug}.provider: no provider {agent.provider!r}; tasks for this agent are refused"
        for slug, agent in snapshot.config.agents.items()
        if agent.provider and agent.provider not in known
    ]


def _file_slug(snapshot: ProjectConfigSnapshot) -> str | None:
    """``project.slug`` of the file, also when the file has other errors."""
    if snapshot.config is not None:
        return snapshot.config.project.slug
    try:
        raw = yaml.safe_load(snapshot.raw or "")
    except yaml.YAMLError:
        return None
    slug = (raw.get("project") or {}).get("slug") if isinstance(raw, dict) and isinstance(raw.get("project"), dict) else None
    return slug if isinstance(slug, str) and re.match(NAME_PATTERN, slug) else None


def _remove_mirror(state, project_id: uuid.UUID) -> None:
    shutil.rmtree(config_repo.mirror_path(state.settings.cache_dir, project_id), ignore_errors=True)


@router.get("/projects", response_model=list[s.ProjectOut])
async def list_projects(session: DB, state: State, _: Admin):
    projects = (await session.scalars(select(Project).order_by(Project.slug))).all()
    return [await _project_out(session, state, p) for p in projects]


@router.post("/projects", response_model=s.ProjectOut, status_code=201)
async def create_project(body: s.ProjectIn, session: DB, state: State, _: Admin):
    """Read the config once. A repository, branch or file that cannot be read answers 422
    with the error; a file with validation errors still creates the project."""
    project = Project(
        id=uuid.uuid4(), slug="", config_url=body.config_url.strip(),
        config_branch=body.config_branch, config_path=body.config_path, created_at=utcnow(),
    )
    snapshot = await read_config(session, state.box, state.settings.cache_dir, project)
    if snapshot.commit is None:
        await asyncio.to_thread(_remove_mirror, state, project.id)
        raise HTTPException(422, snapshot.errors)
    slug = _file_slug(snapshot)
    if slug is None:
        await asyncio.to_thread(_remove_mirror, state, project.id)
        raise HTTPException(422, ["project.slug: the file must name the project's slug", *snapshot.errors])
    other = await session.scalar(select(Project.config_url).where(Project.slug == slug))
    if other is not None:
        await asyncio.to_thread(_remove_mirror, state, project.id)
        raise HTTPException(409, f"project {slug!r} already exists (config repository {other})")
    project.slug = slug
    remember(project, snapshot)
    session.add(project)
    await session.commit()
    await _request_verification(session, state, project.id, strict=False)
    await check_directory(state)
    return await _project_out(session, state, project)


@router.get("/projects/{project_id}", response_model=s.ProjectOut)
async def get_project(project_id: uuid.UUID, session: DB, state: State, _: Admin):
    return await _project_out(session, state, await get_or_404(session, Project, project_id))


@router.post("/projects/{project_id}/reread", response_model=s.ProjectOut)
async def reread_project(project_id: uuid.UUID, session: DB, state: State, _: Admin):
    project = await get_or_404(session, Project, project_id)
    await _reread(session, state, project)
    return await _project_out(session, state, project)


async def _reread(session, state, project: Project) -> None:
    """Read the config again; a new commit asks for a verification (§1.1), and the
    directory is checked (docs/integration-api.md §3). The panel's re-read, PATCH and the
    config hook all read this way."""
    before = project.snapshot_commit
    remember(project, await read_config(session, state.box, state.settings.cache_dir, project))
    await session.commit()
    if project.snapshot_commit is not None and project.snapshot_commit != before:
        await _request_verification(session, state, project.id, strict=False)
    await check_directory(state)


@router.post("/projects/{project_id}/hook-token", response_model=s.ProjectOut)
async def replace_hook_token(project_id: uuid.UUID, session: DB, state: State, _: Admin):
    """A new config hook URL; the old one stops working (docs/integration-api.md §2)."""
    project = await get_or_404(session, Project, project_id)
    project.hook_token = new_hook_token()
    await session.commit()
    return await _project_out(session, state, project)


@router.patch("/projects/{project_id}", response_model=s.ProjectOut)
async def update_project(project_id: uuid.UUID, body: s.ProjectUpdate, session: DB, state: State, _: Admin):
    """Change the branch or the file path, then read again."""
    project = await get_or_404(session, Project, project_id)
    data = body.model_dump(exclude_unset=True)
    if "config_branch" in data:
        project.config_branch = data["config_branch"]
    if "config_path" in data:
        project.config_path = data["config_path"]
    await _reread(session, state, project)
    return await _project_out(session, state, project)


@router.delete("/projects/{project_id}", status_code=NO_CONTENT)
async def delete_project(project_id: uuid.UUID, session: DB, state: State, admin: Admin):
    project = await get_or_404(session, Project, project_id)
    tasks = await session.scalar(select(func.count()).select_from(Task).where(Task.project_id == project.id))
    if tasks:
        raise HTTPException(409, f"the project has {tasks} task{'s' if tasks != 1 else ''}; it cannot be deleted")
    slug = project.slug
    # The project leaves every store that allowed it; the stores' logs say so.
    for store in (await session.scalars(
        select(SecretStore).join(SecretStoreProject, SecretStoreProject.store_id == SecretStore.id)
        .where(SecretStoreProject.project_id == project.id)
    )).all():
        log_change(session, admin, store, "project_removed", project=slug, detail={"reason": "project deleted"})
    await session.execute(delete(SecretStoreProject).where(SecretStoreProject.project_id == project.id))
    await session.delete(project)
    await session.commit()
    await check_directory(state)
    await asyncio.to_thread(_remove_mirror, state, project_id)
    try:
        # The worker deletes its project volume (mirrors, caches); its sweep does if this is lost.
        await state.broker.project_deleted(str(project_id), slug)
    except Exception:  # noqa: BLE001 - the worker's sweep removes the volume later
        log.warning("could not tell the worker that project %s was deleted", slug)
    return Response(status_code=NO_CONTENT)


# The resolved agent ----------------------------------------------------------------------


@router.get("/projects/{project_id}/agents/{agent}/resolved", response_model=s.ResolvedAgentOut)
async def resolved_agent(project_id: uuid.UUID, agent: str, session: DB, state: State, _: Admin):
    """The agent as a run of the latest read config gets it: environment section,
    connections with secrets as ``$NAME``, model, provider and identity."""
    project = await get_or_404(session, Project, project_id)
    snapshot = cached(project)
    if snapshot is None or snapshot.config is None:
        raise HTTPException(409, "the project's config has errors")
    config = snapshot.config
    if agent not in config.agents:
        raise HTTPException(404, f"no agent {agent!r} in the project's config")
    definition = config.agents[agent]
    provider = await resolve_provider(session, definition.provider)
    provider_error = provider_problem(definition.provider) if provider is None else None
    connections = []
    for name in definition.connections:
        raw = config.connection_definition(name)
        view = connection_view(raw)
        used = sorted({n for v in [*raw["args"], *raw["env"].values(), *raw["headers"].values()] for n in SECRET_REF.findall(v)})
        connections.append(s.ResolvedConnection(
            name=name, catalog=config.connections[name].catalog, transport=view["transport"],
            command=view["command"], args=view["args"], url=view["url"], env=view["env"],
            headers=view["headers"], secrets=used,
        ))
    identity_name, identity_email = config.identity(agent)
    default_image = state.settings.project_default_image
    return s.ResolvedAgentOut(
        project_id=project.id,
        commit=snapshot.commit,
        agent=agent,
        name=config.agent_name(agent),
        role=definition.role,
        model=definition.model,
        runtime=definition.runtime,
        provider=definition.provider,
        resolved_provider=s.ResolvedProvider.model_validate(provider, from_attributes=True) if provider else None,
        provider_error=provider_error,
        identity=s.IdentityOut(name=identity_name, email=identity_email),
        connections=connections,
        image=workstation_label(config, default_image),
        environment_text=config_environment_text(config, default_image, agent=agent,
                                                 install_allow=state.settings.egress_allow_hosts),
    )


# Catalog ---------------------------------------------------------------------------------


@router.get("/catalog", response_model=list[s.CatalogEntryOut])
async def list_catalog(_: Admin):
    return [catalog.describe(item) for item in catalog.CATALOG]


# Config repository access (§6) ------------------------------------------------------------


@router.get("/git-credentials", response_model=list[s.GitCredentialOut])
async def list_git_credentials(session: DB, _: Admin):
    return (await session.scalars(select(GitCredential).order_by(GitCredential.host))).all()


@router.post("/git-credentials", response_model=s.GitCredentialOut, status_code=201)
async def create_git_credential(body: s.GitCredentialIn, session: DB, state: State, _: Admin):
    if await session.scalar(select(GitCredential.id).where(GitCredential.host == body.host)):
        raise HTTPException(409, f"a credential for {body.host} already exists; delete it first")
    credential = GitCredential(host=body.host, kind=body.kind, value_encrypted=state.box.encrypt(body.value))
    session.add(credential)
    await session.commit()
    return credential


@router.delete("/git-credentials/{credential_id}", status_code=NO_CONTENT)
async def delete_git_credential(credential_id: uuid.UUID, session: DB, _: Admin):
    await session.delete(await get_or_404(session, GitCredential, credential_id))
    await session.commit()
    return Response(status_code=NO_CONTENT)


# Secret stores (§7) --------------------------------------------------------------------------
#
# The admin allows each store to the projects that may use it, and every change to a store is
# appended to its log (``secret_store_changes``): who, when, what, the key or project it
# concerns. Never a value. An external store (``kinds.KINDS``) lists its key names from its
# backend, and its values are set there only: key writes are refused with 409.


def log_change(session, admin: AdminUser, store: SecretStore, action: str, *, key: str | None = None,
               project: str | None = None, detail: dict | None = None) -> None:
    session.add(SecretStoreChange(
        actor=admin.username, store_id=store.id, store=store.name, action=action, key=key, project=project,
        detail=detail, at=utcnow(),
    ))


async def _store_users(session, name: str) -> list[str]:
    projects = (await session.scalars(select(Project).order_by(Project.slug))).all()
    return [p.slug for p in projects if (p.snapshot or {}).get("store") == name]


async def _allowed(session, store_id: uuid.UUID) -> list[Project]:
    return list((await session.scalars(
        select(Project).join(SecretStoreProject, SecretStoreProject.project_id == Project.id)
        .where(SecretStoreProject.store_id == store_id).order_by(Project.slug)
    )).all())


def _external(store: SecretStore) -> bool:
    found = kinds.KINDS.get(store.kind)
    return found.external if found else True


async def _store_out(session, state: AppState, store: SecretStore) -> s.StoreOut:
    keys: list[str] = []
    error = None
    try:
        keys = sorted(await state.secrets.of(session, store, retry=False).keys())
    except StoreUnreachable as exc:
        error = str(exc)
    return s.StoreOut(
        id=store.id, name=store.name, kind=store.kind, settings=store.settings or {},
        where=kinds.where(store.kind, store.settings or {}), external=_external(store),
        created_at=store.created_at, keys=keys, error=error, used_by=await _store_users(session, store.name),
        allowed_projects=[p.slug for p in await _allowed(session, store.id)],
    )


@router.get("/store-kinds", response_model=list[s.StoreKindOut])
async def list_store_kinds(state: State, _: Admin):
    """The kinds a store can have, whether this install can use each, and their settings."""
    out = []
    for kind in kinds.KINDS.values():
        reason = state.secrets.unavailable(kind.name)
        out.append(s.StoreKindOut(
            kind=kind.name, label=kind.label, external=kind.external, available=reason is None, reason=reason,
            fields=[s.StoreKindFieldOut(**f) for f in kinds.fields(kind.name)],
        ))
    return out


@router.get("/stores", response_model=list[s.StoreOut])
async def list_stores(session: DB, state: State, _: Admin):
    stores = (await session.scalars(select(SecretStore).order_by(SecretStore.name))).all()
    return [await _store_out(session, state, store) for store in stores]


async def _verify_after_keys(session, state, name: str) -> None:
    """Projects naming the store that can now run and whose verification is not current
    get one (docs/run-environment.md §8): a failure for a missing store or keys, or a store
    that did not allow the project, is not rerun by a new config commit, since there is none."""
    projects = (await session.scalars(select(Project).order_by(Project.slug))).all()
    for project in projects:
        snapshot = cached(project)
        if snapshot is None or snapshot.store != name or snapshot.commit is None:
            continue
        health = await assess(session, project, snapshot, state.secrets, retry=False)
        if not health.runnable:
            continue
        latest = await _latest_verification(session, project.id)
        if freshness(latest, project.snapshot_commit) in ("current", "running"):
            continue
        await _request_verification(session, state, project.id, strict=False)


async def _projects_by_slug(session, slugs: list[str]) -> list[Project]:
    """The projects named, in order; 422 for a slug that is no project."""
    wanted = list(dict.fromkeys(slugs))
    found = {p.slug: p for p in (await session.scalars(select(Project).where(Project.slug.in_(wanted)))).all()}
    unknown = [slug for slug in wanted if slug not in found]
    if unknown:
        raise HTTPException(422, f"no project {', '.join(map(repr, unknown))}")
    return [found[slug] for slug in wanted]


async def _set_allowed(session, admin: AdminUser, store: SecretStore, slugs: list[str]) -> None:
    """Make the store's allowed projects exactly ``slugs``, logging each one added or removed."""
    wanted = await _projects_by_slug(session, slugs)
    current = {p.id: p for p in await _allowed(session, store.id)}
    for project in wanted:
        if project.id not in current:
            session.add(SecretStoreProject(store_id=store.id, project_id=project.id, allowed_at=utcnow()))
            log_change(session, admin, store, "project_allowed", project=project.slug)
    keep = {p.id for p in wanted}
    for project_id, project in current.items():
        if project_id not in keep:
            await session.execute(delete(SecretStoreProject).where(
                SecretStoreProject.store_id == store.id, SecretStoreProject.project_id == project_id))
            log_change(session, admin, store, "project_removed", project=project.slug)


async def _settings(state: AppState, name: str, kind: str, settings: dict) -> dict:
    """The settings as stored; 422 with every problem, when the install cannot use the kind,
    or when the backend says they cannot work (a Vault mount that is not KV version 2). A
    backend that cannot be reached now does not stop the save: the store then shows why."""
    try:
        normalized = kinds.normalize(kind, settings)
    except kinds.SettingsInvalid as exc:
        raise HTTPException(422, exc.errors) from None
    reason = state.secrets.unavailable(kind)
    if reason is not None:
        raise HTTPException(422, reason)
    problem = await state.secrets.settings_problem(name, kind, normalized)
    if problem is not None:
        raise HTTPException(422, [problem])
    return normalized


@router.post("/stores", response_model=s.StoreOut, status_code=201)
async def create_store(body: s.StoreIn, session: DB, state: State, admin: Admin):
    """A new store of ``kind`` with its ``settings``; it allows the projects in
    ``allowed_projects`` and no other."""
    if await session.scalar(select(SecretStore.id).where(SecretStore.name == body.name)):
        raise HTTPException(409, f"store {body.name!r} already exists")
    settings = await _settings(state, body.name, body.kind, body.settings)
    store = SecretStore(id=uuid.uuid4(), name=body.name, kind=body.kind, settings=settings, created_at=utcnow())
    session.add(store)
    log_change(session, admin, store, "store_created", detail={"kind": store.kind, "settings": settings})
    await _set_allowed(session, admin, store, body.allowed_projects)
    await session.commit()
    await _verify_after_keys(session, state, store.name)
    await check_directory(state)
    return await _store_out(session, state, store)


@router.get("/stores/{store_id}", response_model=s.StoreOut)
async def get_store(store_id: uuid.UUID, session: DB, state: State, _: Admin):
    return await _store_out(session, state, await get_or_404(session, SecretStore, store_id))


async def _move(session, state: AppState, admin: AdminUser, store: SecretStore, kind: str, settings: dict) -> None:
    """Give the store a new kind or new settings. Projects keep naming it: moving a project's
    values to a vault needs no commit in its config repository. Leaving ``internal`` deletes
    the values kept here (each one logged); the backend is then their only source."""
    normalized = await _settings(state, store.name, kind, settings)
    if kind == store.kind and normalized == (store.settings or {}):
        return
    before = {"kind": store.kind, "settings": store.settings or {}}
    if store.kind == "internal" and kind != "internal":
        for entry in await _entries(session, store.id):
            await session.delete(entry)
            log_change(session, admin, store, "key_deleted", key=entry.key,
                       detail={"reason": f"the store moved to {kinds.kind(kind).label}"})
    store.kind, store.settings = kind, normalized
    log_change(session, admin, store, "settings_changed",
               detail={"before": before, "after": {"kind": kind, "settings": normalized}})


@router.patch("/stores/{store_id}", response_model=s.StoreOut)
async def update_store(store_id: uuid.UUID, body: s.StoreUpdate, session: DB, state: State, admin: Admin):
    """Change the projects allowed to use the store, or its kind and settings. A project
    removed from it is not runnable from then on; runs it already started keep what they
    resolved."""
    store = await get_or_404(session, SecretStore, store_id)
    if body.kind is not None or body.settings is not None:
        kind = body.kind or store.kind
        settings = body.settings if body.settings is not None else ({} if kind != store.kind else store.settings or {})
        await _move(session, state, admin, store, kind, settings)
    if body.allowed_projects is not None:
        await _set_allowed(session, admin, store, body.allowed_projects)
    await session.commit()
    await _verify_after_keys(session, state, store.name)
    await check_directory(state)
    return await _store_out(session, state, store)


@router.get("/stores/{store_id}/changes", response_model=list[s.StoreChangeOut])
async def list_store_changes(store_id: uuid.UUID, session: DB, _: Admin, limit: int = Query(200, ge=1, le=1000)):
    """The store's change log, newest first."""
    await get_or_404(session, SecretStore, store_id)
    return (await session.scalars(
        select(SecretStoreChange).where(SecretStoreChange.store_id == store_id)
        .order_by(SecretStoreChange.at.desc(), SecretStoreChange.id.desc()).limit(limit)
    )).all()


@router.delete("/stores/{store_id}", status_code=NO_CONTENT)
async def delete_store(store_id: uuid.UUID, session: DB, state: State, admin: Admin):
    store = await get_or_404(session, SecretStore, store_id)
    users = await _store_users(session, store.name)
    if users:
        raise HTTPException(409, f"the config of {', '.join(users)} names this store")
    log_change(session, admin, store, "store_deleted")
    await session.execute(delete(SecretStoreProject).where(SecretStoreProject.store_id == store.id))
    await session.delete(store)
    await session.commit()
    await check_directory(state)
    return Response(status_code=NO_CONTENT)


@router.get("/stores/{store_id}/keys", response_model=list[s.StoreKeyOut])
async def list_store_keys(store_id: uuid.UUID, session: DB, state: State, _: Admin):
    """Key names, never values; an external store's as its backend lists them (503 when it
    cannot be reached)."""
    store = await get_or_404(session, SecretStore, store_id)
    if not _external(store):
        return await _entries(session, store_id)
    try:
        names = await state.secrets.of(session, store, retry=False).keys()
    except StoreUnreachable as exc:
        raise HTTPException(503, str(exc)) from exc
    return [s.StoreKeyOut(key=name) for name in sorted(names)]


def _writable(store: SecretStore) -> None:
    """409 for an external store: its backend is the only source of its values."""
    if _external(store):
        label = kinds.KINDS[store.kind].label if store.kind in kinds.KINDS else store.kind
        raise HTTPException(409, f"store {store.name} reads its values from {label}; set and delete them there")


async def _entries(session, store_id: uuid.UUID) -> list[SecretStoreEntry]:
    return list((await session.scalars(
        select(SecretStoreEntry).where(SecretStoreEntry.store_id == store_id).order_by(SecretStoreEntry.key)
    )).all())


KEY_RULE = "a key consists of letters, digits, '_', '.' and '-'"


@router.put("/stores/{store_id}/keys", response_model=list[s.StoreKeyOut])
async def set_store_keys(store_id: uuid.UUID, body: s.StoreValuesIn, session: DB, state: State, admin: Admin):
    """Set or replace several values at once; nothing is written if any key name is invalid."""
    store = await get_or_404(session, SecretStore, store_id)
    _writable(store)
    bad = sorted(k for k in body.values if not SECRET_KEY.match(k))
    if bad:
        raise HTTPException(422, f"{KEY_RULE}: {', '.join(map(repr, bad))}")
    existing = {e.key: e for e in (await session.scalars(
        select(SecretStoreEntry).where(SecretStoreEntry.store_id == store_id, SecretStoreEntry.key.in_(body.values))
    )).all()}
    now = utcnow()
    for key, value in body.values.items():
        entry = existing.get(key)
        if entry is None:
            entry = SecretStoreEntry(store_id=store_id, key=key, value_encrypted="")
            session.add(entry)
        entry.value_encrypted = state.box.encrypt(value)
        entry.updated_at = now
        log_change(session, admin, store, "key_set", key=key)
    name = store.name
    await session.commit()
    await _verify_after_keys(session, state, name)
    await check_directory(state)
    return await _entries(session, store_id)


@router.put("/stores/{store_id}/keys/{key}", response_model=s.StoreKeyOut)
async def set_store_key(store_id: uuid.UUID, key: str, body: s.StoreValueIn, session: DB, state: State,
                        admin: Admin):
    """Set or replace a value; keys are the ``NAME`` of ``${secret:NAME}``."""
    store = await get_or_404(session, SecretStore, store_id)
    _writable(store)
    if not SECRET_KEY.match(key):
        raise HTTPException(422, KEY_RULE)
    entry = await session.scalar(
        select(SecretStoreEntry).where(SecretStoreEntry.store_id == store_id, SecretStoreEntry.key == key)
    )
    if entry is None:
        entry = SecretStoreEntry(store_id=store_id, key=key, value_encrypted="")
        session.add(entry)
    entry.value_encrypted = state.box.encrypt(body.value)
    entry.updated_at = utcnow()
    log_change(session, admin, store, "key_set", key=key)
    name = store.name
    await session.commit()
    await _verify_after_keys(session, state, name)
    await check_directory(state)
    await session.refresh(entry)
    return entry


@router.delete("/stores/{store_id}/keys/{key}", status_code=NO_CONTENT)
async def delete_store_key(store_id: uuid.UUID, key: str, session: DB, state: State, admin: Admin):
    store = await get_or_404(session, SecretStore, store_id)
    _writable(store)
    entry = await session.scalar(
        select(SecretStoreEntry).where(SecretStoreEntry.store_id == store_id, SecretStoreEntry.key == key)
    )
    if entry is None:
        raise HTTPException(404, "key not found")
    await session.delete(entry)
    log_change(session, admin, store, "key_deleted", key=key)
    await session.commit()
    await check_directory(state)
    return Response(status_code=NO_CONTENT)


# Project verification ---------------------------------------------------------------------


@router.post("/projects/{project_id}/verify", response_model=s.VerificationCreated, status_code=202)
async def verify_project(project_id: uuid.UUID, session: DB, state: State, _: Admin):
    """Ask a worker to prepare the project as a run would, in a throwaway directory. While
    a verification is pending or running, that one is the answer."""
    await get_or_404(session, Project, project_id)
    verification = await _request_verification(session, state, project_id, strict=True)
    return s.VerificationCreated(verification_id=verification.id)


async def _request_verification(session, state, project_id: uuid.UUID, *, strict: bool) -> ProjectVerification:
    """The open verification of the project, or a new one handed to a worker. Without the
    broker the new one fails at once; ``strict`` then answers 503 (the manual Verify),
    otherwise the caller's own work stands (adding or re-reading a project)."""
    await session.get(Project, project_id, with_for_update=True, populate_existing=True)  # one at a time
    open_one = await session.scalar(
        select(ProjectVerification)
        .where(ProjectVerification.project_id == project_id, ProjectVerification.state.in_(OPEN_VERIFICATION))
        .order_by(ProjectVerification.created_at.desc())
        .limit(1)
    )
    if open_one is not None:
        await session.commit()
        return open_one
    verification = ProjectVerification(project_id=project_id, state="pending", steps=[])
    session.add(verification)
    await session.commit()
    try:
        await state.broker.verify(str(verification.id))
    except Exception as exc:
        verification.state, verification.error, verification.ended_at = "failed", f"broker: {exc}", utcnow()
        await session.commit()
        if strict:
            raise HTTPException(503, f"could not reach the broker: {exc}") from exc
    return verification


@router.get("/projects/{project_id}/verifications", response_model=list[s.VerificationOut])
async def list_verifications(
    project_id: uuid.UUID, session: DB, _: Admin, limit: int = Query(5, ge=1, le=50)
):
    stmt = (
        select(ProjectVerification)
        .where(ProjectVerification.project_id == project_id)
        .order_by(ProjectVerification.created_at.desc())
        .limit(limit)
    )
    return (await session.scalars(stmt)).all()


@router.get("/projects/{project_id}/verifications/{verification_id}", response_model=s.VerificationOut)
async def get_verification(project_id: uuid.UUID, verification_id: uuid.UUID, session: DB, _: Admin):
    verification = await get_or_404(session, ProjectVerification, verification_id)
    if verification.project_id != project_id:
        raise HTTPException(404, "ProjectVerification not found")
    return verification
