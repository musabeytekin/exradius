"""Admin CRUD for configuration: projects, repositories, env vars, secrets, connections,
agents and agent-to-project attachments (architecture §8)."""

from __future__ import annotations

import asyncio
import uuid

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import ValidationError
from sqlalchemy import select

from exradius.api.connection_test import test_connection
from exradius.api.deps import DB, Admin, State, apply_update, get_or_404
from exradius.models import (
    Agent,
    Connection,
    EnvVar,
    Project,
    ProjectAgent,
    ProjectVerification,
    Provider,
    Repository,
    Secret,
    utcnow,
)
from exradius.runtimes import runtime_class
from exradius.resolve import (
    MissingSecret,
    SecretValues,
    connection_spec,
    load_secrets,
    merged_connection,
    secret_references,
)
from exradius.worker import prepare
from exradius.schemas import api as s

router = APIRouter(prefix="/api")
REPOSITORY_CHECK_TIMEOUT = 30
NO_CONTENT = status.HTTP_204_NO_CONTENT


# Projects ----------------------------------------------------------------------------


@router.get("/projects", response_model=list[s.ProjectOut])
async def list_projects(session: DB, _: Admin):
    return (await session.scalars(select(Project).order_by(Project.slug))).all()


@router.post("/projects", response_model=s.ProjectOut, status_code=201)
async def create_project(body: s.ProjectIn, session: DB, _: Admin):
    project = Project(**body.model_dump())
    session.add(project)
    await session.commit()
    return project


@router.get("/projects/{project_id}", response_model=s.ProjectOut)
async def get_project(project_id: uuid.UUID, session: DB, _: Admin):
    return await get_or_404(session, Project, project_id)


@router.patch("/projects/{project_id}", response_model=s.ProjectOut)
async def update_project(project_id: uuid.UUID, body: s.ProjectUpdate, session: DB, _: Admin):
    project = await get_or_404(session, Project, project_id)
    apply_update(project, body)
    await session.commit()
    return project


@router.delete("/projects/{project_id}", status_code=NO_CONTENT)
async def delete_project(project_id: uuid.UUID, session: DB, _: Admin):
    await session.delete(await get_or_404(session, Project, project_id))
    await session.commit()
    return Response(status_code=NO_CONTENT)


# Repositories ----------------------------------------------------------------------------


async def _repo(session, project_id: uuid.UUID, repo_id: uuid.UUID) -> Repository:
    repo = await get_or_404(session, Repository, repo_id)
    if repo.project_id != project_id:
        raise HTTPException(404, "Repository not found")
    return repo


def _check_push(repo_or_body) -> None:
    if repo_or_body.push_allowed and not repo_or_body.auth_secret_id:
        raise HTTPException(422, "push_allowed needs an auth secret (auth_secret_id)")


async def _check_auth_secret(session, project_id: uuid.UUID, secret_id: uuid.UUID | None) -> None:
    if secret_id is None:
        return
    secret = await get_or_404(session, Secret, secret_id)
    if secret.project_id not in (None, project_id):
        raise HTTPException(422, "auth secret belongs to another project")


@router.get("/projects/{project_id}/repositories", response_model=list[s.RepositoryOut])
async def list_repositories(project_id: uuid.UUID, session: DB, _: Admin):
    stmt = select(Repository).where(Repository.project_id == project_id).order_by(Repository.name)
    return (await session.scalars(stmt)).all()


@router.post("/projects/{project_id}/repositories", response_model=s.RepositoryOut, status_code=201)
async def create_repository(project_id: uuid.UUID, body: s.RepositoryIn, session: DB, _: Admin):
    await get_or_404(session, Project, project_id)
    await _check_auth_secret(session, project_id, body.auth_secret_id)
    _check_push(body)
    repo = Repository(project_id=project_id, **body.model_dump())
    session.add(repo)
    await session.commit()
    return repo


@router.patch("/projects/{project_id}/repositories/{repo_id}", response_model=s.RepositoryOut)
async def update_repository(
    project_id: uuid.UUID, repo_id: uuid.UUID, body: s.RepositoryUpdate, session: DB, _: Admin
):
    repo = await _repo(session, project_id, repo_id)
    await _check_auth_secret(session, project_id, body.auth_secret_id)
    before = (repo.url, repo.auth_secret_id)
    if body.push_allowed is None and "push_allowed" in body.model_fields_set:
        body.push_allowed = False
    apply_update(repo, body)
    _check_push(repo)
    if (repo.url, repo.auth_secret_id) != before:
        repo.last_check = None
    await session.commit()
    return repo


@router.post("/projects/{project_id}/repositories/{repo_id}/check", response_model=s.RepositoryCheckOut)
async def check_repository(project_id: uuid.UUID, repo_id: uuid.UUID, session: DB, state: State, _: Admin):
    """``git ls-remote`` from the API with the repository's auth secret. The result is
    stored on the repository (``last_check``) for the setup checklist."""
    repo = await _repo(session, project_id, repo_id)
    auth = None
    if repo.auth_secret_id:
        secret = await session.get(Secret, repo.auth_secret_id)
        auth = state.box.decrypt(secret.value_encrypted) if secret else None
    checked_at = utcnow()
    try:
        refs = await asyncio.to_thread(prepare.ls_remote, repo.url, auth, REPOSITORY_CHECK_TIMEOUT)
        result = s.RepositoryCheckOut(
            ok=True, branches=refs.branches, default_branch=refs.default_branch, checked_at=checked_at
        )
        if repo.branch not in refs.branches:
            result.ok = False
            result.error = f"branch {repo.branch!r} does not exist on the remote"
    except prepare.PreparationError as exc:
        result = s.RepositoryCheckOut(ok=False, error=str(exc), checked_at=checked_at)
    repo.last_check = result.model_dump(mode="json")
    await session.commit()
    return result


@router.delete("/projects/{project_id}/repositories/{repo_id}", status_code=NO_CONTENT)
async def delete_repository(project_id: uuid.UUID, repo_id: uuid.UUID, session: DB, _: Admin):
    await session.delete(await _repo(session, project_id, repo_id))
    await session.commit()
    return Response(status_code=NO_CONTENT)


# Environment variables -----------------------------------------------------------------


async def _env_var(session, project_id: uuid.UUID, var_id: uuid.UUID) -> EnvVar:
    var = await get_or_404(session, EnvVar, var_id)
    if var.project_id != project_id:
        raise HTTPException(404, "EnvVar not found")
    return var


@router.get("/projects/{project_id}/env-vars", response_model=list[s.EnvVarOut])
async def list_env_vars(project_id: uuid.UUID, session: DB, _: Admin):
    stmt = select(EnvVar).where(EnvVar.project_id == project_id).order_by(EnvVar.key)
    return (await session.scalars(stmt)).all()


@router.post("/projects/{project_id}/env-vars", response_model=s.EnvVarOut, status_code=201)
async def create_env_var(project_id: uuid.UUID, body: s.EnvVarIn, session: DB, _: Admin):
    await get_or_404(session, Project, project_id)
    var = EnvVar(project_id=project_id, **body.model_dump())
    session.add(var)
    await session.commit()
    return var


@router.patch("/projects/{project_id}/env-vars/{var_id}", response_model=s.EnvVarOut)
async def update_env_var(
    project_id: uuid.UUID, var_id: uuid.UUID, body: s.EnvVarUpdate, session: DB, _: Admin
):
    var = await _env_var(session, project_id, var_id)
    apply_update(var, body)
    await session.commit()
    return var


@router.delete("/projects/{project_id}/env-vars/{var_id}", status_code=NO_CONTENT)
async def delete_env_var(project_id: uuid.UUID, var_id: uuid.UUID, session: DB, _: Admin):
    await session.delete(await _env_var(session, project_id, var_id))
    await session.commit()
    return Response(status_code=NO_CONTENT)


# Secrets (write-only) ------------------------------------------------------------------


async def _list_secrets(session, project_id: uuid.UUID | None):
    stmt = select(Secret).where(
        Secret.project_id.is_(None) if project_id is None else Secret.project_id == project_id
    )
    return (await session.scalars(stmt.order_by(Secret.key))).all()


async def _create_secret(session, state, project_id: uuid.UUID | None, body: s.SecretIn) -> Secret:
    existing = [x for x in await _list_secrets(session, project_id) if x.key == body.key]
    if existing:
        raise HTTPException(409, f"secret {body.key!r} already exists")
    secret = Secret(project_id=project_id, key=body.key, value_encrypted=state.box.encrypt(body.value))
    session.add(secret)
    await session.commit()
    return secret


async def _secret(session, project_id: uuid.UUID | None, secret_id: uuid.UUID) -> Secret:
    secret = await get_or_404(session, Secret, secret_id)
    if secret.project_id != project_id:
        raise HTTPException(404, "Secret not found")
    return secret


@router.get("/secrets", response_model=list[s.SecretOut])
async def list_global_secrets(session: DB, _: Admin):
    return await _list_secrets(session, None)


@router.post("/secrets", response_model=s.SecretOut, status_code=201)
async def create_global_secret(body: s.SecretIn, session: DB, state: State, _: Admin):
    return await _create_secret(session, state, None, body)


@router.put("/secrets/{secret_id}", response_model=s.SecretOut)
async def replace_global_secret(
    secret_id: uuid.UUID, body: s.SecretUpdate, session: DB, state: State, _: Admin
):
    secret = await _secret(session, None, secret_id)
    secret.value_encrypted = state.box.encrypt(body.value)
    await session.commit()
    return secret


@router.delete("/secrets/{secret_id}", status_code=NO_CONTENT)
async def delete_global_secret(secret_id: uuid.UUID, session: DB, _: Admin):
    await session.delete(await _secret(session, None, secret_id))
    await session.commit()
    return Response(status_code=NO_CONTENT)


@router.get("/projects/{project_id}/secrets", response_model=list[s.SecretOut])
async def list_project_secrets(project_id: uuid.UUID, session: DB, _: Admin):
    return await _list_secrets(session, project_id)


@router.post("/projects/{project_id}/secrets", response_model=s.SecretOut, status_code=201)
async def create_project_secret(
    project_id: uuid.UUID, body: s.SecretIn, session: DB, state: State, _: Admin
):
    await get_or_404(session, Project, project_id)
    return await _create_secret(session, state, project_id, body)


@router.put("/projects/{project_id}/secrets/{secret_id}", response_model=s.SecretOut)
async def replace_project_secret(
    project_id: uuid.UUID, secret_id: uuid.UUID, body: s.SecretUpdate, session: DB, state: State, _: Admin
):
    secret = await _secret(session, project_id, secret_id)
    secret.value_encrypted = state.box.encrypt(body.value)
    await session.commit()
    return secret


@router.delete("/projects/{project_id}/secrets/{secret_id}", status_code=NO_CONTENT)
async def delete_project_secret(project_id: uuid.UUID, secret_id: uuid.UUID, session: DB, _: Admin):
    await session.delete(await _secret(session, project_id, secret_id))
    await session.commit()
    return Response(status_code=NO_CONTENT)


# Connections ---------------------------------------------------------------------------


@router.get("/connections", response_model=list[s.ConnectionOut])
async def list_connections(session: DB, _: Admin):
    return (await session.scalars(select(Connection).order_by(Connection.slug))).all()


@router.post("/connections", response_model=s.ConnectionOut, status_code=201)
async def create_connection(body: s.ConnectionIn, session: DB, _: Admin):
    connection = Connection(**body.model_dump())
    session.add(connection)
    await session.commit()
    return connection


@router.get("/connections/{connection_id}", response_model=s.ConnectionOut)
async def get_connection(connection_id: uuid.UUID, session: DB, _: Admin):
    return await get_or_404(session, Connection, connection_id)


@router.patch("/connections/{connection_id}", response_model=s.ConnectionOut)
async def update_connection(
    connection_id: uuid.UUID, body: s.ConnectionUpdate, session: DB, _: Admin
):
    connection = await get_or_404(session, Connection, connection_id)
    before = _test_relevant(connection)
    apply_update(connection, body)
    if _test_relevant(connection) != before:
        connection.last_test = None  # the stored test no longer describes this configuration
    try:
        s.ConnectionIn.model_validate({k: getattr(connection, k) for k in s.ConnectionIn.model_fields})
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False, include_input=False)) from exc
    await session.commit()
    return connection


def _test_relevant(connection: Connection) -> tuple:
    return (connection.transport, connection.command, list(connection.args or []), connection.url,
            dict(connection.env or {}), dict(connection.headers or {}))


@router.delete("/connections/{connection_id}", status_code=NO_CONTENT)
async def delete_connection(connection_id: uuid.UUID, session: DB, _: Admin):
    await session.delete(await get_or_404(session, Connection, connection_id))
    await session.commit()
    return Response(status_code=NO_CONTENT)


@router.post("/connections/{connection_id}/test", response_model=s.ConnectionTestOut)
async def test_connection_endpoint(
    connection_id: uuid.UUID,
    session: DB,
    state: State,
    _: Admin,
    project_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
):
    """Start the connection and list its tools. With ``project_id`` (and ``agent_id``) the
    project's secrets and that attachment's overrides are applied."""
    connection = await get_or_404(session, Connection, connection_id)
    secrets: SecretValues = await load_secrets(session, state.box, project_id)
    override = None
    if project_id and agent_id:
        link = await session.get(ProjectAgent, (project_id, agent_id))
        if link:
            secrets.overrides = dict(link.secret_overrides or {})
            override = (link.connection_overrides or {}).get(connection.slug)
    try:
        spec = connection_spec(connection, override, secrets)
    except MissingSecret as exc:
        result = s.ConnectionTestOut(ok=False, error=str(exc))
    else:
        result = await test_connection(spec)
    connection.last_test = {
        "ok": result.ok,
        "tested_at": utcnow().isoformat(),
        "tools": [{"name": t["name"], "description": t.get("description", "")} for t in result.tools],
        "error": result.error,
    }
    await session.commit()
    return result


# Agents ----------------------------------------------------------------------------------


def _agent_out(agent: Agent) -> s.AgentOut:
    return s.AgentOut(
        id=agent.id,
        slug=agent.slug,
        name=agent.name,
        prompt=agent.prompt,
        runtime=agent.runtime,
        created_at=agent.created_at,
        connection_ids=[c.id for c in agent.connections],
        provider_id=agent.provider_id,
        identity_name=agent.identity_name,
        identity_email=agent.identity_email,
    )


async def _check_provider(session, provider_id: uuid.UUID | None, runtime: str) -> None:
    """An agent's own provider must exist and be one its runtime can use."""
    if provider_id is None:
        return
    provider = await session.get(Provider, provider_id)
    if provider is None:
        raise HTTPException(422, f"unknown provider id {provider_id}")
    if not runtime_class(runtime).capabilities.accepts(provider.kind, provider.auth_method):
        raise HTTPException(422, f"runtime {runtime} cannot use a {provider.kind}/{provider.auth_method} provider")


async def _connections(session, ids: list[uuid.UUID]) -> list[Connection]:
    found = (await session.scalars(select(Connection).where(Connection.id.in_(ids)))).all()
    missing = set(ids) - {c.id for c in found}
    if missing:
        raise HTTPException(422, f"unknown connection ids: {sorted(map(str, missing))}")
    return list(found)


@router.get("/agents", response_model=list[s.AgentOut])
async def list_agents(session: DB, _: Admin):
    return [_agent_out(a) for a in (await session.scalars(select(Agent).order_by(Agent.slug))).all()]


@router.post("/agents", response_model=s.AgentOut, status_code=201)
async def create_agent(body: s.AgentIn, session: DB, _: Admin):
    await _check_provider(session, body.provider_id, body.runtime)
    agent = Agent(**body.model_dump(exclude={"connection_ids", "identity_name"}))
    agent.identity_name = body.identity_name or body.name
    agent.connections = await _connections(session, body.connection_ids)
    session.add(agent)
    await session.commit()
    return _agent_out(agent)


@router.get("/agents/{agent_id}", response_model=s.AgentOut)
async def get_agent(agent_id: uuid.UUID, session: DB, _: Admin):
    return _agent_out(await get_or_404(session, Agent, agent_id))


@router.patch("/agents/{agent_id}", response_model=s.AgentOut)
async def update_agent(agent_id: uuid.UUID, body: s.AgentUpdate, session: DB, _: Admin):
    agent = await get_or_404(session, Agent, agent_id)
    data = body.model_dump(exclude_unset=True)
    if "connection_ids" in data:
        agent.connections = await _connections(session, data.pop("connection_ids") or [])
    for key in ("identity_name", "identity_email"):
        if key in data and data[key] is None:
            data.pop(key)  # required columns: null means "unchanged"
    if "provider_id" in data or "runtime" in data:
        await _check_provider(session, data.get("provider_id", agent.provider_id), data.get("runtime") or agent.runtime)
    for key, value in data.items():
        setattr(agent, key, value)
    await session.commit()
    return _agent_out(agent)


@router.delete("/agents/{agent_id}", status_code=NO_CONTENT)
async def delete_agent(agent_id: uuid.UUID, session: DB, _: Admin):
    await session.delete(await get_or_404(session, Agent, agent_id))
    await session.commit()
    return Response(status_code=NO_CONTENT)


# Agent-to-project attachments ------------------------------------------------------------


@router.get("/projects/{project_id}/agents", response_model=list[s.ProjectAgentOut])
async def list_project_agents(project_id: uuid.UUID, session: DB, _: Admin):
    stmt = select(ProjectAgent).where(ProjectAgent.project_id == project_id)
    return (await session.scalars(stmt)).all()


@router.post("/projects/{project_id}/agents", response_model=s.ProjectAgentOut, status_code=201)
async def attach_agent(project_id: uuid.UUID, body: s.ProjectAgentIn, session: DB, _: Admin):
    await get_or_404(session, Project, project_id)
    await get_or_404(session, Agent, body.agent_id)
    if await session.get(ProjectAgent, (project_id, body.agent_id)):
        raise HTTPException(409, "agent is already attached to this project")
    link = ProjectAgent(project_id=project_id, **body.model_dump())
    session.add(link)
    await session.commit()
    return link


@router.patch("/projects/{project_id}/agents/{agent_id}", response_model=s.ProjectAgentOut)
async def update_attachment(
    project_id: uuid.UUID, agent_id: uuid.UUID, body: s.ProjectAgentUpdate, session: DB, _: Admin
):
    link = await get_or_404(session, ProjectAgent, (project_id, agent_id))
    apply_update(link, body)
    await session.commit()
    return link


@router.delete("/projects/{project_id}/agents/{agent_id}", status_code=NO_CONTENT)
async def detach_agent(project_id: uuid.UUID, agent_id: uuid.UUID, session: DB, _: Admin):
    await session.delete(await get_or_404(session, ProjectAgent, (project_id, agent_id)))
    await session.commit()
    return Response(status_code=NO_CONTENT)


# Project verification ---------------------------------------------------------------------


@router.post("/projects/{project_id}/verify", response_model=s.VerificationCreated, status_code=202)
async def verify_project(project_id: uuid.UUID, session: DB, state: State, _: Admin):
    """Ask a worker to prepare the project as a run would, in a throwaway directory."""
    await get_or_404(session, Project, project_id)
    verification = ProjectVerification(project_id=project_id, state="pending", steps=[])
    session.add(verification)
    await session.commit()
    try:
        await state.broker.verify(str(verification.id))
    except Exception as exc:
        verification.state, verification.error, verification.ended_at = "failed", f"broker: {exc}", utcnow()
        await session.commit()
        raise HTTPException(503, f"could not reach the broker: {exc}") from exc
    return s.VerificationCreated(verification_id=verification.id)


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


# Resolved configuration of an attachment ----------------------------------------------------


@router.get("/projects/{project_id}/agents/{agent_id}/resolved", response_model=s.ResolvedAgentOut)
async def resolved_attachment(project_id: uuid.UUID, agent_id: uuid.UUID, session: DB, _: Admin):
    """The agent's connections as this project will run them: overrides applied, secret
    references shown as written (never their values), unresolved references listed."""
    link = await get_or_404(session, ProjectAgent, (project_id, agent_id))
    agent = await get_or_404(session, Agent, agent_id)
    rows = (
        await session.execute(
            select(Secret.key, Secret.project_id).where(
                (Secret.project_id == project_id) | (Secret.project_id.is_(None))
            )
        )
    ).all()
    project_keys = {key for key, pid in rows if pid is not None}
    global_keys = {key for key, pid in rows if pid is None}
    secret_overrides = dict(link.secret_overrides or {})
    overrides = dict(link.connection_overrides or {})

    connections = []
    for connection in agent.connections:
        spec = merged_connection(connection, overrides.get(connection.slug))
        references = []
        for name in secret_references(spec):
            target = secret_overrides.get(name, name)
            scope = "project" if target in project_keys else ("global" if target in global_keys else None)
            references.append(s.SecretReference(name=name, resolves_to=target, scope=scope))
        connections.append(
            s.ResolvedConnection(
                slug=connection.slug,
                name=connection.name,
                transport=spec["transport"],
                command=spec["command"],
                args=spec["args"],
                url=spec["url"],
                env=spec["env"],
                headers=spec["headers"],
                overridden=connection.slug in overrides,
                references=references,
                unresolved=[r.name for r in references if r.scope is None],
            )
        )
    return s.ResolvedAgentOut(
        project_id=project_id, agent_id=agent_id, secret_overrides=secret_overrides, connections=connections
    )
