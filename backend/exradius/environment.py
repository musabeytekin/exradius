"""The generated environment section of the system prompt (docs/environment-and-services.md §1).

Built from the project's config (docs/config-repo.md §9): the workstation (its image, or the
Dockerfile it is built from), the repositories (URLs without credentials), the fixed "not
available" line with the hosts the agent may reach (``exradius.network``, the list the
egress proxy enforces), the services with their settings (secret values never: a secret
appears as the variable that carries it) and the notes. It never names Exradius, and it
does not list installed tools: the agent discovers those itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from exradius.network import allowed_hosts
from exradius.resolve import exported_secrets
from exradius.schemas.project_config import SECRET_REF, ProjectConfig

HEADING = "# Your environment"
NOT_AVAILABLE = "Not available: a Docker daemon, Kubernetes, sudo, installing packages."


def network_line(hosts: list[str], repositories: bool) -> str:
    """What the egress proxy lets the agent reach, besides its model and connections."""
    if hosts and repositories:
        return "Outbound network access is limited to the repositories above and to: " + ", ".join(hosts) + "."
    if hosts:
        return "Outbound network access is limited to: " + ", ".join(hosts) + "."
    if repositories:
        return "Outbound network access is limited to the repositories above."
    return "There is no outbound network access."
CLOSING = (
    "The working directory is deleted when the task ends. Only what you push, attach to the result,\n"
    "or write in the result survives."
)


def public_url(url: str) -> str:
    """The URL without credentials: no ``user:password@``, query or fragment."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return re.sub(r"(\w+://)[^/@\s]+@", r"\1", url)
    if not parts.scheme or not parts.netloc:
        return url  # scp-like ``git@host:path``: the user is not a credential
    host = parts.netloc.rpartition("@")[2]
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def can_push(url: str, push: bool, has_credential: bool) -> bool:
    """Whether runs get push credentials for this remote (docs/agent-identity.md §4:
    ``push``, a credential and an http(s) URL)."""
    return push and has_credential and url.split("://", 1)[0].lower() in ("http", "https")


@dataclass(frozen=True)
class RepoFact:
    name: str
    branch: str | None
    """``None``: the remote's default branch."""
    url: str
    push: bool


NOT_EXPORTED = "<a secret you do not have>"
SECRET_HINT = "A value written as $NAME is the value of your environment variable NAME."


@dataclass(frozen=True)
class ServiceFact:
    name: str
    image: str
    ports: tuple[int, ...] = ()
    settings: tuple[str, ...] = ()
    """``KEY=value`` lines; see ``service_settings``."""


def service_settings(env: dict[str, object], exported: set[str]) -> tuple[str, ...]:
    """A service's env as the agent may see it: plain values as they are, each
    ``${secret:NAME}`` as ``$NAME`` when the run environment carries ``NAME`` (see
    ``exradius.resolve.exported_secrets``), else as a placeholder. Plain settings
    come first, then those built from secrets, each group in stored order."""
    plain: list[str] = []
    secret: list[str] = []
    for key, raw in env.items():
        value = str(raw)
        if not SECRET_REF.search(value):
            plain.append(f"{key}={value}")
            continue
        shown = SECRET_REF.sub(lambda m: f"${m.group(1)}" if m.group(1) in exported else NOT_EXPORTED, value)
        secret.append(f"{key}={shown}")
    return tuple(plain + secret)


def build_environment_text(
    *, image: str, repositories: list[RepoFact], services: list[ServiceFact], notes: str, hosts: list[str] = ()
) -> str:
    """The section exactly as documented. ``image`` completes "built from ": ``the image
    x:1`` or a Dockerfile's path; ``hosts`` are the agent phase's allowed hosts."""
    intro = f"You run unattended in a Linux container built from {image}.\n"
    parts = [HEADING]
    if repositories:
        parts.append(intro + "Each folder in the working directory is a git clone of one repository:")
        parts.append(
            "\n".join(
                f"- {r.name}: {f'branch {r.branch}' if r.branch else 'the default branch'}, origin {public_url(r.url)}, "
                + ("pushing allowed" if r.push else "pushing NOT allowed")
                for r in repositories
            )
        )
    else:
        parts.append(intro + "The working directory contains no repositories.")
    parts.append(NOT_AVAILABLE + " " + network_line(list(hosts), bool(repositories)))

    if services:
        lines = ["Services running for this task, reachable by host name from this container:"]
        for service in services:
            line = f"- {service.name} ({service.image})"
            if len(service.ports) == 1:
                line += f" on port {service.ports[0]}"
            elif service.ports:
                line += " on ports " + ", ".join(str(p) for p in service.ports)
            if service.settings:
                line += ", " + ", ".join(service.settings)
            lines.append(line)
        if any("$" in setting for service in services for setting in service.settings):
            lines.append(SECRET_HINT)
        parts.append("\n".join(lines))

    parts.append(CLOSING)
    if notes.strip():
        parts.append("Notes from the project's maintainers:\n" + notes.strip())
    return "\n\n".join(parts)


def workstation_label(config: ProjectConfig, default_image: str) -> str:
    """The workstation as the panel shows it: its image name or ``built from <dockerfile>``."""
    station = config.workstation
    return f"built from {station.dockerfile}" if station.dockerfile else (station.image or default_image)


def config_environment_text(config: ProjectConfig, default_image: str, *, agent: str | None = None,
                            install_allow: list[str] = ()) -> str:
    """The section for a config, as the run of ``agent`` gets it; ``install_allow`` is the
    install's ``egress.allow``."""
    exported = set(exported_secrets(config))
    station = config.workstation
    return build_environment_text(
        image=station.dockerfile if station.dockerfile else f"the image {station.image or default_image}",
        repositories=[
            RepoFact(r.name, r.branch, r.url, can_push(r.url, r.push, bool(r.credential)))
            for r in config.repositories
        ],
        services=[
            ServiceFact(
                name,
                service.image or f"built from {service.dockerfile}",
                tuple(service.ports),
                service_settings(service.env, exported),
            )
            for name, service in config.services.items()
        ],
        notes=config.notes,
        hosts=allowed_hosts(config, agent, install_allow),
    )
