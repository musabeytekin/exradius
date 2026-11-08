"""Admin API for configuration (docs/config-repo.md §11): projects as config repositories,
config repository access per git host, secret stores, the catalog, the resolved agent
view and project verification. Nothing about a project is edited here: its config file
is the only truth, and the API only reads it."""

from __future__ import annotations

import asyncio
import re
import shutil
import uuid

import yaml
from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import func, select

from exradius import catalog, config_repo, directory
from exradius.api import integration_routes
from exradius.api.deps import DB, Admin, AppState, State, get_or_404
from exradius.config_repo import ProjectConfigSnapshot
from exradius.crypto import new_hook_token
from exradius.environment import config_environment_text, workstation_label
from exradius.models import (
    GitCredential,
    Project,
    ProjectVerification,
    Provider,
    SecretStore,
    SecretStoreEntry,
    Task,
    utcnow,
)
from exradius.project_state import assess, cached, read_config, remember, store_keys
from exradius.providers import provider_problem, resolve_provider
from exradius.resolve import connection_view
from exradius.schemas import api as s
from exradius.schemas.project_config import NAME_PATTERN, SECRET_REF

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
    await directory.check(state.sessions, state.broker.publish_directory)


async def _project_out(session, state: AppState, project: Project) -> s.ProjectOut:
    snapshot = cached(project) or ProjectConfigSnapshot.unreadable("the config has not been read yet")
    latest = await _latest_verification(session, project.id)
    health = await assess(session, project, snapshot)
    config = snapshot.config
    keys = await store_keys(session, snapshot.store) or set()
    store_id = (
        await session.scalar(select(SecretStore.id).where(SecretStore.name == snapshot.store))
        if snapshot.store else None
    )
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
        store_id=store_id,
        store_exists=health.store_exists,
        required_keys=[s.RequiredKeyOut(name=k, present=k in keys) for k in snapshot.required_keys],
        missing_keys=health.missing_keys,
        agents=list(config.agents) if config else [],
        config=config.model_dump(mode="json") if config else None,
        raw=snapshot.raw,
        created_at=project.created_at,
        verification=s.VerificationSummary.model_validate(latest) if latest else None,
        freshness=freshness(latest, project.snapshot_commit),
        cache_bytes=None,
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
async def delete_project(project_id: uuid.UUID, session: DB, state: State, _: Admin):
    project = await get_or_404(session, Project, project_id)
    tasks = await session.scalar(select(func.count()).select_from(Task).where(Task.project_id == project.id))
    if tasks:
        raise HTTPException(409, f"the project has {tasks} task{'s' if tasks != 1 else ''}; it cannot be deleted")
    await session.delete(project)
    await session.commit()
    await check_directory(state)
    await asyncio.to_thread(_remove_mirror, state, project_id)
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
        environment_text=config_environment_text(config, default_image),
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


async def _store_users(session, name: str) -> list[str]:
    projects = (await session.scalars(select(Project).order_by(Project.slug))).all()
    return [p.slug for p in projects if (p.snapshot or {}).get("store") == name]


async def _store_out(session, store: SecretStore) -> s.StoreOut:
    keys = (await session.scalars(
        select(SecretStoreEntry.key).where(SecretStoreEntry.store_id == store.id).order_by(SecretStoreEntry.key)
    )).all()
    return s.StoreOut(
        id=store.id, name=store.name, kind=store.kind, created_at=store.created_at,
        keys=list(keys), used_by=await _store_users(session, store.name),
    )


@router.get("/stores", response_model=list[s.StoreOut])
async def list_stores(session: DB, _: Admin):
    stores = (await session.scalars(select(SecretStore).order_by(SecretStore.name))).all()
    return [await _store_out(session, store) for store in stores]


async def _verify_after_keys(session, state, name: str) -> None:
    """Projects naming the store whose required keys are now all present and whose
    verification is not current get one (docs/run-environment.md §8): a failure for a
    missing store or keys is not rerun by a new config commit, since there is none."""
    projects = (await session.scalars(select(Project).order_by(Project.slug))).all()
    for project in projects:
        snapshot = cached(project)
        if snapshot is None or snapshot.store != name or snapshot.commit is None:
            continue
        health = await assess(session, project, snapshot)
        if not health.runnable:
            continue
        latest = await _latest_verification(session, project.id)
        if freshness(latest, project.snapshot_commit) in ("current", "running"):
            continue
        await _request_verification(session, state, project.id, strict=False)


@router.post("/stores", response_model=s.StoreOut, status_code=201)
async def create_store(body: s.StoreIn, session: DB, state: State, _: Admin):
    if await session.scalar(select(SecretStore.id).where(SecretStore.name == body.name)):
        raise HTTPException(409, f"store {body.name!r} already exists")
    store = SecretStore(name=body.name, kind="internal")
    session.add(store)
    await session.commit()
    await _verify_after_keys(session, state, store.name)
    await check_directory(state)
    return await _store_out(session, store)


@router.get("/stores/{store_id}", response_model=s.StoreOut)
async def get_store(store_id: uuid.UUID, session: DB, _: Admin):
    return await _store_out(session, await get_or_404(session, SecretStore, store_id))


@router.delete("/stores/{store_id}", status_code=NO_CONTENT)
async def delete_store(store_id: uuid.UUID, session: DB, state: State, _: Admin):
    store = await get_or_404(session, SecretStore, store_id)
    users = await _store_users(session, store.name)
    if users:
        raise HTTPException(409, f"the config of {', '.join(users)} names this store")
    await session.delete(store)
    await session.commit()
    await check_directory(state)
    return Response(status_code=NO_CONTENT)


@router.get("/stores/{store_id}/keys", response_model=list[s.StoreKeyOut])
async def list_store_keys(store_id: uuid.UUID, session: DB, _: Admin):
    await get_or_404(session, SecretStore, store_id)
    return await _entries(session, store_id)


async def _entries(session, store_id: uuid.UUID) -> list[SecretStoreEntry]:
    return list((await session.scalars(
        select(SecretStoreEntry).where(SecretStoreEntry.store_id == store_id).order_by(SecretStoreEntry.key)
    )).all())


KEY_RULE = "a key consists of letters, digits, '_', '.' and '-'"


@router.put("/stores/{store_id}/keys", response_model=list[s.StoreKeyOut])
async def set_store_keys(store_id: uuid.UUID, body: s.StoreValuesIn, session: DB, state: State, _: Admin):
    """Set or replace several values at once; nothing is written if any key name is invalid."""
    await get_or_404(session, SecretStore, store_id)
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
    await session.commit()
    store = await session.get(SecretStore, store_id)
    await _verify_after_keys(session, state, store.name)
    await check_directory(state)
    return await _entries(session, store_id)


@router.put("/stores/{store_id}/keys/{key}", response_model=s.StoreKeyOut)
async def set_store_key(store_id: uuid.UUID, key: str, body: s.StoreValueIn, session: DB, state: State, _: Admin):
    """Set or replace a value; keys are the ``NAME`` of ``${secret:NAME}``."""
    await get_or_404(session, SecretStore, store_id)
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
    await session.commit()
    store = await session.get(SecretStore, store_id)
    await _verify_after_keys(session, state, store.name)
    await check_directory(state)
    await session.refresh(entry)
    return entry


@router.delete("/stores/{store_id}/keys/{key}", status_code=NO_CONTENT)
async def delete_store_key(store_id: uuid.UUID, key: str, session: DB, state: State, _: Admin):
    entry = await session.scalar(
        select(SecretStoreEntry).where(SecretStoreEntry.store_id == store_id, SecretStoreEntry.key == key)
    )
    if entry is None:
        raise HTTPException(404, "key not found")
    await session.delete(entry)
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
