"""The generated environment section of the system prompt (docs/environment-and-services.md §1).

Built from facts Exradius holds: the project's image and repositories (URLs without
credentials), the fixed "not available" line, the project's services and the maintainers'
notes. It never names Exradius, and it does not list installed tools: the agent discovers
those itself.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

HEADING = "# Your environment"
NOT_AVAILABLE = (
    "Not available: a Docker daemon, Kubernetes, sudo, installing packages. "
    "Outbound network access is available."
)
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


def can_push(url: str, push_allowed: bool, has_secret: bool) -> bool:
    """Whether runs get push credentials for this remote (docs/agent-identity.md §4:
    ``push_allowed``, an auth secret and an http(s) URL)."""
    return push_allowed and has_secret and url.split("://", 1)[0].lower() in ("http", "https")


@dataclass(frozen=True)
class RepoFact:
    name: str
    branch: str
    url: str
    push: bool


@dataclass(frozen=True)
class ServiceFact:
    name: str
    image: str
    ports: tuple[int, ...] = ()


def build_environment_text(
    *, image: str, repositories: list[RepoFact], services: list[ServiceFact], notes: str
) -> str:
    """The section exactly as documented."""
    intro = f"You run unattended in a Linux container built from the image {image}.\n"
    parts = [HEADING]
    if repositories:
        parts.append(intro + "Each folder in the working directory is a git clone of one repository:")
        parts.append(
            "\n".join(
                f"- {r.name}: branch {r.branch}, origin {public_url(r.url)}, "
                + ("pushing allowed" if r.push else "pushing NOT allowed")
                for r in repositories
            )
        )
    else:
        parts.append(intro + "The working directory contains no repositories.")
    parts.append(NOT_AVAILABLE)

    if services:
        lines = ["Services running for this task, reachable by host name from this container:"]
        for service in services:
            line = f"- {service.name} ({service.image})"
            if len(service.ports) == 1:
                line += f" on port {service.ports[0]}"
            elif service.ports:
                line += " on ports " + ", ".join(str(p) for p in service.ports)
            lines.append(line)
        parts.append("\n".join(lines))

    parts.append(CLOSING)
    if notes.strip():
        parts.append("Notes from the project's maintainers:\n" + notes.strip())
    return "\n\n".join(parts)


async def project_environment_text(session: AsyncSession, project_id: uuid.UUID, default_image: str) -> str:
    """The section for a project as the worker would build it now."""
    from exradius.models import Project, ProjectService, Repository

    project = await session.get(Project, project_id)
    assert project is not None
    repos = (
        await session.scalars(select(Repository).where(Repository.project_id == project_id).order_by(Repository.name))
    ).all()
    services = (
        await session.scalars(
            select(ProjectService)
            .where(ProjectService.project_id == project_id)
            .order_by(ProjectService.position, ProjectService.name)
        )
    ).all()
    return build_environment_text(
        image=project.image or default_image,
        repositories=[
            RepoFact(r.name, r.branch, r.url, can_push(r.url, r.push_allowed, r.auth_secret_id is not None))
            for r in repos
        ],
        services=[ServiceFact(s.name, s.image, tuple(int(p) for p in s.ports or [])) for s in services],
        notes=project.notes or "",
    )
