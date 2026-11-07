"""Images built from Dockerfiles in a config repository (docs/config-repo.md §4).

The context is read from the config repository's mirror at the run's commit (``git
archive``), so the build sees exactly the declared directory. The tag is
``exradius/<project>/<workstation|service>:<hash>``, where the hash is the git tree hash of
the context directory (combined with the Dockerfile's blob hash when the Dockerfile lies
outside the context), so an unchanged context is never rebuilt. Builds of one project are
serialised with a file lock under the data directory and recorded in ``image_builds``.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from exradius import config_repo
from exradius.config import Settings
from exradius.db import SessionFactory
from exradius.models import ImageBuild, utcnow
from exradius.schemas.project_config import ProjectConfig, build_paths
from exradius.worker.containers import ContainerManager

log = logging.getLogger(__name__)

LOG_TAIL = 4000
HASH_LENGTH = 12
WORKSTATION = "workstation"


class BuildFailed(Exception):
    """The message names the image and ends with the build's log tail."""


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


@dataclass
class BuildResult:
    image: str
    built: bool
    seconds: float
    log: str

    def describe(self) -> str:
        if not self.built:
            return f"{self.image} is up to date (context unchanged)"
        return f"{self.log}\nbuilt {self.image} in {self.seconds:g}s"  # last: step logs keep the tail


@dataclass
class Images:
    workstation: str
    services: dict[str, str] = field(default_factory=dict)


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


def _tail(text: str) -> str:
    text = text.strip()
    return text if len(text) <= LOG_TAIL else "…" + text[-LOG_TAIL:]


class ImageBuilder:
    def __init__(self, settings: Settings, sessions: SessionFactory, containers: ContainerManager):
        self.settings = settings
        self.sessions = sessions
        self.containers = containers

    def _lock_path(self, project_id: uuid.UUID) -> Path:
        return self.settings.data_dir / "locks" / f"build-{project_id}.lock"

    async def ensure(self, project_id: uuid.UUID, project_slug: str, commit: str, job: BuildJob) -> BuildResult:
        """The job's image, built unless an image with its tag exists. Raises
        ``BuildFailed``."""
        mirror = config_repo.mirror_path(self.settings.data_dir, project_id)
        digest = await asyncio.to_thread(content_hash, mirror, commit, job)
        tag = image_tag(project_slug, job.name, digest)
        lock_path = self._lock_path(project_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = await asyncio.to_thread(self._acquire, lock_path)
        try:
            if await self.containers.image_exists(tag):
                return BuildResult(tag, built=False, seconds=0.0, log="")
            return await self._build(project_id, mirror, commit, job, digest, tag)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
            handle.close()

    @staticmethod
    def _acquire(path: Path):
        handle = open(path, "w")  # noqa: SIM115 - released by ensure()
        fcntl.flock(handle, fcntl.LOCK_EX)
        return handle

    async def _build(self, project_id, mirror, commit, job: BuildJob, digest: str, tag: str) -> BuildResult:
        async with self.sessions() as session:
            row = ImageBuild(project_id=project_id, name=job.name, tree_hash=digest, image=tag, state="running")
            session.add(row)
            await session.commit()
            build_id = row.id
        started = time.monotonic()
        try:
            context, dockerfile = await asyncio.to_thread(
                config_repo.build_context, mirror, commit, job.context, job.dockerfile
            )
            ok, output = await self.containers.build_image(
                context, dockerfile=dockerfile, tag=tag, labels={"exradius.build": job.name}
            )
        except Exception as exc:  # noqa: BLE001 - recorded on the build and raised as BuildFailed
            ok, output = False, f"{type(exc).__name__}: {exc}"
        seconds = round(time.monotonic() - started, 1)
        tail = _tail(output)
        async with self.sessions() as session:
            row = await session.get(ImageBuild, build_id)
            if row is not None:
                row.state = "succeeded" if ok else "failed"
                row.ended_at = utcnow()
                row.log_tail = tail
                await session.commit()
        if not ok:
            raise BuildFailed(f"building {job.name} from {job.dockerfile} failed:\n{tail}")
        log.info("built %s in %ss", tag, seconds)
        return BuildResult(tag, built=True, seconds=seconds, log=tail)

    async def images(self, project_id: uuid.UUID, project_slug: str, commit: str, config: ProjectConfig) -> Images:
        """Every image a run of this config needs; builds what is missing."""
        built = {
            job.name: (await self.ensure(project_id, project_slug, commit, job)).image
            for job in build_jobs(config)
        }
        return Images(
            workstation=built.get(WORKSTATION) or config.workstation.image or self.settings.project_default_image,
            services={name: built.get(name) or service.image or "" for name, service in config.services.items()},
        )
