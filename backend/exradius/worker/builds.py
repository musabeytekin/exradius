"""Images built from Dockerfiles in a config repository (docs/config-repo.md §4,
docs/kubernetes.md §12).

The tag is ``exradius/<project>/<workstation|service>:<hash>`` under the registry prefix
(``builds_registry``), where the hash is the git tree hash of the context directory (combined
with the Dockerfile's blob hash when the Dockerfile lies outside the context), so an
unchanged context is never rebuilt.

A build is a ``Build`` object named ``build-<project>-<name>-<hash>`` in the builds
namespace, which the operator runs in a single-use rootless BuildKit pod. The worker creates
it if absent, and every run or verification that needs the image waits on that same object:
creating it is atomic, which replaces v0.1.0's per-project file lock. A succeeded Build means
the image exists; a failed one is replaced by the next run that needs the image. The build
context is the ``git archive`` of the context directory at the commit, stored in the object
store and served to the build pod by the api (``/internal/builds/{id}/context``) with the
Build's own token, so build pods need no git and no config-repository credential. Each Build
has an ``image_builds`` row, written from its status.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from exradius import config_repo
from exradius.config import Settings
from exradius.crypto import hash_token, new_run_token
from exradius.db import SessionFactory
from exradius.models import ImageBuild, utcnow
from exradius.schemas.project_config import ProjectConfig, build_paths
from exradius.storage import ObjectStore
from exradius.worker.kube import API_VERSION, Cluster

log = logging.getLogger(__name__)

LOG_TAIL = 4000
HASH_LENGTH = 12
WORKSTATION = "workstation"
CACHE_TAG = "buildcache"
POLL_SECONDS = 2.0
LABEL_PROJECT = "exradius.dev/project"
LABEL_PURPOSE = "exradius.dev/purpose"
LABEL_IMAGE = "exradius.dev/image"
FINAL = ("Succeeded", "Failed")

Progress = Callable[[dict[str, Any]], Awaitable[None]]
"""Called with the Build's status at every look while it is waited on."""
Wanted = Callable[[], Awaitable[bool]]
"""Whether the image is still wanted; a wait that is no longer wanted raises ``BuildAbandoned``."""


class BuildFailed(Exception):
    """The message names the image and ends with the build's log tail."""


class BuildAbandoned(Exception):
    """The caller stopped waiting (its run was cancelled); the Build goes on for others."""


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
    """A build ran while this caller waited (it created the Build or joined it running)."""
    seconds: float
    log: str
    digest: str | None = None

    def describe(self) -> str:
        if not self.built:
            return f"{self.image} is up to date (context unchanged)"
        digest = f" ({self.digest})" if self.digest else ""
        # Last: step logs keep the tail.
        return f"{self.log}\nbuilt {self.image} in {self.seconds:g}s{digest}".lstrip("\n")


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


def image_ref(registry: str, tag: str) -> str:
    """The tag under the registry prefix, as build pods push it and nodes pull it."""
    return f"{registry.rstrip('/')}/{tag}" if registry else tag


def cache_ref(image: str) -> str:
    """The layer cache of an image: the same repository, tag ``buildcache``."""
    return f"{image.rsplit(':', 1)[0]}:{CACHE_TAG}"


def build_name(project_slug: str, name: str, digest: str) -> str:
    """``build-<project>-<name>-<hash>``: slugs and service names are DNS labels."""
    return f"build-{project_slug}-{name}-{digest[:HASH_LENGTH]}"


def build_object(*, name: str, namespace: str, build_id: uuid.UUID, project: str, job: BuildJob, image: str,
                 cache: str | None) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "id": str(build_id),
        "project": project,
        "name": job.name,
        "image": image,
        "dockerfile": config_repo.build_dockerfile_name(job.context, job.dockerfile),
        "secretName": name,
    }
    if cache:
        spec["cache"] = cache
    return {
        "apiVersion": API_VERSION,
        "kind": "Build",
        "metadata": {"name": name, "namespace": namespace,
                     "labels": {LABEL_PROJECT: project, LABEL_IMAGE: job.name, LABEL_PURPOSE: "build"}},
        "spec": spec,
    }


def build_secret(owner: dict[str, Any], token: str) -> dict[str, Any]:
    """The Build's token, owned by the Build so it goes with it."""
    metadata = owner["metadata"]
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": metadata["name"],
            "namespace": metadata["namespace"],
            "labels": dict(metadata.get("labels") or {}),
            "ownerReferences": [{"apiVersion": API_VERSION, "kind": "Build", "name": metadata["name"],
                                 "uid": metadata["uid"]}],
        },
        "type": "Opaque",
        "data": {"token": base64.b64encode(token.encode()).decode()},
    }


def _tail(text: str) -> str:
    text = (text or "").strip()
    return text if len(text) <= LOG_TAIL else "…" + text[-LOG_TAIL:]


def _phase(obj: dict[str, Any] | None) -> str:
    return ((obj or {}).get("status") or {}).get("phase") or ""


def _seconds(status: dict[str, Any], fallback: float) -> float:
    try:
        started = datetime.fromisoformat(status["startedAt"].replace("Z", "+00:00"))
        finished = datetime.fromisoformat(status["finishedAt"].replace("Z", "+00:00"))
        return round((finished - started).total_seconds(), 1)
    except (KeyError, AttributeError, ValueError):
        return round(fallback, 1)


def failure(job: BuildJob, status: dict[str, Any]) -> str:
    """A failed Build as the run's error: the image, what ended it and the log tail."""
    lines = [status.get("message") or status.get("reason") or "the build failed", _tail(status.get("log") or "")]
    return f"building {job.name} from {job.dockerfile} failed:\n" + "\n".join(line for line in lines if line)


class ImageBuilder:
    def __init__(self, settings: Settings, sessions: SessionFactory, store: ObjectStore, cluster: Cluster):
        self.settings = settings
        self.sessions = sessions
        self.store = store
        self.cluster = cluster
        self.poll = POLL_SECONDS

    async def ensure(self, project_id: uuid.UUID, project_slug: str, commit: str, job: BuildJob, *,
                     progress: Progress | None = None, wanted: Wanted | None = None) -> BuildResult:
        """The job's image: up to date when a succeeded Build of its tag exists, else built
        (or joined while another caller builds it). Raises ``BuildFailed``."""
        mirror = config_repo.mirror_path(self.settings.cache_dir, project_id)
        digest = await asyncio.to_thread(content_hash, mirror, commit, job)
        image = image_ref(self.settings.builds_registry, image_tag(project_slug, job.name, digest))
        name = build_name(project_slug, job.name, digest)
        started = time.monotonic()
        obj = await self.cluster.get_build(name)
        if _phase(obj) == "Succeeded":
            await self._record(obj)
            return BuildResult(image, built=False, seconds=0.0, log="", digest=obj["status"].get("digest"))
        for _ in range(3):  # a Build deleted or replaced while waited on is looked up again
            if _phase(obj) == "Failed":
                # The next caller after a failure builds again (a fault may be transient);
                # only the failed object is deleted, never one another caller just made.
                log.info("replacing failed Build %s", name)
                await self._record(obj)
                await self.cluster.delete_build(name, uid=obj["metadata"]["uid"])
                obj = None
            if obj is None:
                obj = await self._create(project_id, project_slug, commit, job, mirror, digest, image, name)
            final = await self._wait(obj, progress, wanted)
            if final is None:
                obj = await self.cluster.get_build(name)
                continue
            status = final.get("status") or {}
            if status.get("phase") == "Failed":
                raise BuildFailed(failure(job, status))
            seconds = _seconds(status, time.monotonic() - started)
            log.info("built %s in %ss", image, seconds)
            return BuildResult(image, built=True, seconds=seconds, log=_tail(status.get("log") or ""),
                               digest=status.get("digest"))
        raise BuildFailed(f"building {job.name} from {job.dockerfile} failed:\nthe Build {name} kept disappearing")

    async def _create(self, project_id: uuid.UUID, slug: str, commit: str, job: BuildJob, mirror: Path, digest: str,
                      image: str, name: str) -> dict[str, Any]:
        """Create the Build, or return the one another caller created first. The creator
        then stores the context and the row, and gives the pod its token last: the pod
        cannot start before its Secret exists."""
        build_id = uuid.uuid4()
        cache = cache_ref(image) if self.settings.builds_cache else None
        body = build_object(name=name, namespace=self.settings.builds_namespace, build_id=build_id, project=slug,
                            job=job, image=image, cache=cache)
        created, fresh = await self.cluster.create_build(body)
        if not fresh:
            return created
        log.info("building %s (Build %s)", image, name)
        try:
            context, _ = await asyncio.to_thread(config_repo.build_context, mirror, commit, job.context, job.dockerfile)
            key = f"builds/{build_id}/context.tar"
            await self.store.put(key, context, "application/x-tar")
            token = new_run_token()
            async with self.sessions() as session:
                session.add(ImageBuild(
                    id=build_id, project_id=project_id, name=job.name, tree_hash=digest, image=image, state="running",
                    build_object=name, context_key=key, token_hash=hash_token(token),
                ))
                await session.commit()
            await self.cluster.create_secret(build_secret(created, token))
        except Exception:
            # Nobody else can finish this Build: remove it, so the next caller makes a new one.
            await self.cluster.delete_build(name, uid=created["metadata"]["uid"])
            raise
        return created

    async def _wait(self, obj: dict[str, Any], progress: Progress | None, wanted: Wanted | None) -> dict[str, Any] | None:
        """The Build once final; ``None`` when it was deleted or replaced meanwhile."""
        name, uid = obj["metadata"]["name"], obj["metadata"].get("uid")
        while True:
            if progress is not None:
                await progress(obj.get("status") or {})
            if _phase(obj) in FINAL:
                await self._record(obj)
                return obj
            if wanted is not None and not await wanted():
                raise BuildAbandoned(name)
            await asyncio.sleep(self.poll)
            latest = await self.cluster.get_build(name)
            if latest is None or latest["metadata"].get("uid") != uid:
                return None
            obj = latest

    async def _record(self, obj: dict[str, Any]) -> None:
        """The Build's outcome on its ``image_builds`` row (any caller may write it)."""
        status = obj.get("status") or {}
        try:
            build_id = uuid.UUID((obj.get("spec") or {}).get("id") or "")
        except ValueError:
            return
        async with self.sessions() as session:
            row = await session.get(ImageBuild, build_id, with_for_update=True)
            if row is None or row.state != "running":
                return
            row.state = "succeeded" if status.get("phase") == "Succeeded" else "failed"
            row.ended_at = utcnow()
            row.digest = status.get("digest") or None
            row.log_tail = _tail("\n".join(p for p in (status.get("message") or "", status.get("log") or "") if p))
            row.token_hash = None
            await session.commit()
        # The revoked token's Secret has done its job (the Build keeps the outcome).
        try:
            await self.cluster.delete_secret(obj["metadata"]["name"], obj["metadata"].get("namespace"))
        except Exception:  # noqa: BLE001 - it goes with the Build anyway
            log.exception("could not delete the Secret of Build %s", obj["metadata"]["name"])

    async def images(self, project_id: uuid.UUID, project_slug: str, commit: str, config: ProjectConfig, *,
                     wanted: Wanted | None = None) -> Images:
        """Every image a run of this config needs; builds what is missing."""
        built = {
            job.name: (await self.ensure(project_id, project_slug, commit, job, wanted=wanted)).image
            for job in build_jobs(config)
        }
        return Images(
            workstation=built.get(WORKSTATION) or config.workstation.image or self.settings.project_default_image,
            services={name: built.get(name) or service.image or "" for name, service in config.services.items()},
        )
