"""Resolve a project's config into what a run needs (docs/config-repo.md §5, §7).

Every ``${secret:NAME}`` in the config resolves from the secret store the file names.
Values are decrypted only here, at Run creation (worker) or for the run spec (API);
the views for the panel show references as ``$NAME`` placeholders instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from exradius.schemas.project_config import SECRET_REF, ProjectConfig, ServiceDef
from exradius.git import RepoSpec

ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MissingSecret(Exception):
    def __init__(self, name: str, store: str | None):
        where = f"in store {store}" if store else "(the config names no secret store)"
        super().__init__(f"missing secret {name} {where}")
        self.name = name
        self.store = store


@dataclass
class StoreValues:
    store: str | None
    values: dict[str, str] = field(default_factory=dict)

    def lookup(self, name: str) -> str:
        if name not in self.values:
            raise MissingSecret(name, self.store)
        return self.values[name]


def substitute(value: str, secrets: StoreValues) -> str:
    return SECRET_REF.sub(lambda m: secrets.lookup(m.group(1)), value)


def placeholder(value: str) -> str:
    """A value as the panel and the agent may see it: each secret as ``$NAME``."""
    return SECRET_REF.sub(lambda m: f"${m.group(1)}", value)


def connection_spec(definition: dict[str, Any], secrets: StoreValues) -> dict[str, Any]:
    """The runner-facing description of one MCP server, with secrets substituted."""
    return {
        **definition,
        "args": [substitute(a, secrets) for a in definition["args"]],
        "env": {k: substitute(v, secrets) for k, v in definition["env"].items()},
        "headers": {k: substitute(v, secrets) for k, v in definition["headers"].items()},
    }


def connection_view(definition: dict[str, Any]) -> dict[str, Any]:
    """The same with ``$NAME`` placeholders, for the resolved agent view."""
    return {
        **definition,
        "args": [placeholder(a) for a in definition["args"]],
        "env": {k: placeholder(v) for k, v in definition["env"].items()},
        "headers": {k: placeholder(v) for k, v in definition["headers"].items()},
    }


def agent_connection_specs(config: ProjectConfig, agent: str, secrets: StoreValues) -> list[dict[str, Any]]:
    return [connection_spec(config.connection_definition(name), secrets) for name in config.agents[agent].connections]


def exported_secrets(config: ProjectConfig) -> list[str]:
    """Secrets the workstation carries under their own names: those its services' settings
    use, so the environment section can show them as ``$NAME``. Repository credentials
    and connection values are used only by reference."""
    names: set[str] = set()
    for service in config.services.values():
        for value in service.env.values():
            names.update(n for n in SECRET_REF.findall(value) if ENV_NAME.match(n))
    return sorted(names)


def workstation_environment(config: ProjectConfig, secrets: StoreValues) -> dict[str, str]:
    """The project's ``env`` with secrets substituted, plus ``exported_secrets``; for setup
    commands and the run container alike."""
    env = {name: secrets.lookup(name) for name in exported_secrets(config)}
    env.update({k: substitute(v, secrets) for k, v in config.env.items()})
    return env


def service_environment(service: ServiceDef, secrets: StoreValues) -> dict[str, str]:
    return {k: substitute(v, secrets) for k, v in service.env.items()}


def repo_specs(config: ProjectConfig, secrets: StoreValues | None) -> list[RepoSpec]:
    """The repositories with their credentials (``secrets=None`` leaves them out)."""
    return [
        RepoSpec(
            name=repo.name,
            url=repo.url,
            branch=repo.branch,
            auth=substitute(repo.credential, secrets) if repo.credential and secrets is not None else None,
            push_allowed=repo.push,
        )
        for repo in config.repositories
    ]
