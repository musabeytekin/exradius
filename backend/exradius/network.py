"""What a run may reach in the agent phase, and how its HTTP connections reach their
servers (docs/kubernetes.md §11).

The egress proxy refuses every host the agent phase does not allow. The list is built from
what the run declares: the install's ``egress.allow``, the config's ``network.allow``, the
catalog hosts of the agent's connections (the package registry a launcher downloads the
server from, the service's API, the host of a config key such as GitLab's API URL) and the
URL hosts of its HTTP connections. The model and the repositories are not on it: they have
their own routes on the proxy, as does an HTTP connection whose headers hold a secret.

The api builds the proxy's policy from it, and the environment section's network line from
the same list, so what the agent is told is what the proxy does.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from exradius import catalog
from exradius.schemas.project_config import SECRET_REF, ProjectConfig

MCP_ROUTE = "/mcp/"
"""The egress proxy's connection route (egress/internal/proxy/reverse.go)."""


def url_host(url: str | None) -> str | None:
    if not url or SECRET_REF.search(url):
        return None
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def proxied(definition: dict[str, Any]) -> bool:
    """An HTTP connection whose headers hold a secret: the pod gets the proxy's route
    without those headers, and the proxy adds them."""
    return definition.get("transport") == "http" and any(
        SECRET_REF.search(str(value)) for value in (definition.get("headers") or {}).values())


def connection_hosts(config: ProjectConfig, name: str) -> list[str]:
    """The hosts one connection reaches besides a proxy route."""
    conn = config.connections[name]
    definition = config.connection_definition(name)
    hosts: list[str] = []
    if conn.catalog:
        item = catalog.entry(conn.catalog)
        if item is not None:
            hosts += item.hosts
            for key in item.config:
                if key.host and (host := url_host((conn.config or {}).get(key.key, key.default))):
                    hosts.append(host)
    if definition["transport"] == "http" and not proxied(definition) and (host := url_host(definition.get("url"))):
        hosts.append(host)
    return hosts


def _unique(hosts: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for host in hosts:
        key = host.strip().lower().removesuffix(".")
        if key and key not in seen:
            seen.add(key)
            out.append(host.strip())
    return out


def agent_connections(config: ProjectConfig, agent: str | None) -> list[str]:
    """An agent's connections; without an agent, every connection some agent uses (a
    verify Run lists their tools)."""
    if agent is not None:
        return list(config.agents[agent].connections)
    return [name for name in config.connections if any(name in a.connections for a in config.agents.values())]


def allowed_hosts(config: ProjectConfig, agent: str | None, install: Iterable[str] = ()) -> list[str]:
    """The agent phase's host list, in that order: the install's, the config's, the
    connections'."""
    hosts = list(install) + list(config.network.allow)
    for name in agent_connections(config, agent):
        hosts += connection_hosts(config, name)
    return _unique(hosts)


def route_connection(spec: dict[str, Any], definition: dict[str, Any], egress_url: str) -> dict[str, Any]:
    """A resolved connection as the pod gets it: an HTTP connection with secret headers
    goes to the proxy's route and keeps only its plain headers."""
    if not proxied(definition):
        return spec
    plain = {key: value for key, value in definition["headers"].items() if not SECRET_REF.search(str(value))}
    return {**spec, "url": egress_url.rstrip("/") + MCP_ROUTE + spec["name"], "headers": plain}
