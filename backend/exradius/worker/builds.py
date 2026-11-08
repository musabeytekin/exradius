"""Images built from Dockerfiles in a config repository (docs/config-repo.md §4).

The tag is ``exradius/<project>/<workstation|service>:<hash>``, where the hash is the git
tree hash of the context directory (combined with the Dockerfile's blob hash when the
Dockerfile lies outside the context), so an unchanged context is never rebuilt. The build
itself becomes a ``Build`` object in phase 2 (docs/kubernetes.md §12); until then a config
with a Dockerfile is not runnable (``exradius/capabilities.py``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from exradius import config_repo
from exradius.schemas.project_config import ProjectConfig, build_paths

HASH_LENGTH = 12
WORKSTATION = "workstation"


@dataclass(frozen=True)
class BuildJob:
    name: str
    """``workstation`` or the service's name."""
    dockerfile: str
    context: str
    """Both from the repository root."""

    @property
    def step(self) -> str:
        return "Build workstation" if self.name == WORKSTATION else f"Build service {self.name}"


def build_jobs(config: ProjectConfig) -> list[BuildJob]:
    jobs = []
    items = [(WORKSTATION, config.workstation), *config.services.items()]
    for name, item in items:
        if item.dockerfile:
            paths = build_paths("", item)
            jobs.append(BuildJob(name, paths.dockerfile, paths.context))
    return jobs


def content_hash(mirror: Path, commit: str, job: BuildJob) -> str:
    """The context's tree hash; with the Dockerfile outside the context, a hash of both."""
    tree = config_repo.tree_hash(mirror, commit, job.context)
    inside = job.context == "" or job.dockerfile.startswith(job.context + "/")
    if inside:
        return tree
    blob = config_repo.tree_hash(mirror, commit, job.dockerfile)
    return hashlib.sha1(f"{tree}:{blob}".encode()).hexdigest()


def image_tag(project_slug: str, name: str, digest: str) -> str:
    return f"exradius/{project_slug}/{name}:{digest[:HASH_LENGTH]}"
