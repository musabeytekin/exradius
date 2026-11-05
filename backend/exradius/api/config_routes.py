"""Admin CRUD for configuration: projects, repositories, env vars, secrets, connections,
agents and agent-to-project attachments (architecture §8)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Response, status
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
    Repository,
    Secret,
)
from exradius.resolve import MissingSecret, SecretValues, connection_spec, load_secrets
from exradius.schemas import api as s

router = APIRouter(prefix="/api")
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
    apply_update(repo, body)
    await session.commit()
    return repo


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
    apply_update(connection, body)
    try:
        s.ConnectionIn.model_validate({k: getattr(connection, k) for k in s.ConnectionIn.model_fields})
    except ValidationError as exc:
        raise HTTPException(422, exc.errors(include_url=False, include_context=False)) from exc
    await session.commit()
    return connection


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
        return s.ConnectionTestOut(ok=False, error=str(exc))
    return await test_connection(spec)


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
    )


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
    agent = Agent(**body.model_dump(exclude={"connection_ids"}))
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
