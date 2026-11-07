"""Resolve configuration into what a run needs: connections, secrets and environment.

Connection ``env`` and ``headers`` values may reference secrets by name with
``${secret:NAME}``. A reference resolves to the project's secret ``NAME`` if one exists,
otherwise to the global secret ``NAME``. ``project_agents.secret_overrides`` maps a
referenced name to another secret name for that agent on that project, and
``project_agents.connection_overrides`` maps a connection slug to fields that replace
(``command``, ``args``, ``url``) or extend (``env``, ``headers``) the connection.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exradius.crypto import SecretBox
from exradius.models import Agent, Connection, EnvVar, ProjectAgent, Secret

SECRET_REF = re.compile(r"\$\{secret:([A-Za-z0-9_.\-]+)\}")


class MissingSecret(Exception):
    def __init__(self, name: str):
        super().__init__(f"secret {name!r} is referenced but not defined")
        self.name = name


@dataclass
class SecretValues:
    project: dict[str, str] = field(default_factory=dict)
    global_: dict[str, str] = field(default_factory=dict)
    overrides: dict[str, str] = field(default_factory=dict)

    def lookup(self, name: str) -> str:
        name = self.overrides.get(name, name)
        if name in self.project:
            return self.project[name]
        if name in self.global_:
            return self.global_[name]
        raise MissingSecret(name)


def substitute(value: Any, secrets: SecretValues) -> Any:
    if not isinstance(value, str):
        return value
    return SECRET_REF.sub(lambda m: secrets.lookup(m.group(1)), value)


async def load_secrets(
    session: AsyncSession, box: SecretBox, project_id: uuid.UUID | None
) -> SecretValues:
    rows = (
        await session.scalars(
            select(Secret).where((Secret.project_id == project_id) | (Secret.project_id.is_(None)))
        )
    ).all()
    values = SecretValues()
    for row in rows:
        target = values.global_ if row.project_id is None else values.project
        target[row.key] = box.decrypt(row.value_encrypted)
    return values


def merged_connection(connection: Connection, override: dict[str, Any] | None) -> dict[str, Any]:
    """A connection with a project override applied; secret references left as written."""
    override = override or {}
    return {
        "name": connection.slug,
        "transport": connection.transport,
        "command": override.get("command", connection.command),
        "args": list(override.get("args", connection.args or [])),
        "url": override.get("url", connection.url),
        "env": {k: str(v) for k, v in {**(connection.env or {}), **(override.get("env") or {})}.items()},
        "headers": {
            k: str(v) for k, v in {**(connection.headers or {}), **(override.get("headers") or {})}.items()
        },
    }


def secret_references(spec: dict[str, Any]) -> list[str]:
    """Secret names referenced in a connection's env and header values, in order."""
    names: list[str] = []
    for value in [*spec["env"].values(), *spec["headers"].values()]:
        for name in SECRET_REF.findall(value):
            if name not in names:
                names.append(name)
    return names


def connection_spec(
    connection: Connection, override: dict[str, Any] | None, secrets: SecretValues
) -> dict[str, Any]:
    """The runner-facing description of one MCP server, with secrets substituted."""
    spec = merged_connection(connection, override)
    spec["env"] = {k: str(substitute(v, secrets)) for k, v in spec["env"].items()}
    spec["headers"] = {k: str(substitute(v, secrets)) for k, v in spec["headers"].items()}
    return spec


def agent_connection_specs(
    agent: Agent, project_agent: ProjectAgent | None, secrets: SecretValues
) -> list[dict[str, Any]]:
    overrides = (project_agent.connection_overrides if project_agent else None) or {}
    secrets.overrides = dict((project_agent.secret_overrides if project_agent else None) or {})
    return [connection_spec(c, overrides.get(c.slug), secrets) for c in agent.connections]


async def project_environment(
    session: AsyncSession, project_id: uuid.UUID, secrets: SecretValues
) -> dict[str, str]:
    """The project's variables and the project's secrets, for the run container.

    Variable values may reference secrets with ``${secret:NAME}`` (project, then global),
    like connection values; a missing one raises ``MissingSecret``."""
    env_vars = (await session.scalars(select(EnvVar).where(EnvVar.project_id == project_id))).all()
    env = {v.key: str(substitute(v.value, secrets)) for v in env_vars}
    env.update(secrets.project)
    return env
