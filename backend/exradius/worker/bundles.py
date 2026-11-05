"""Runtime bundles (docs/runtime-bundles.md §2.2).

Each runtime ships its bundle (``bin/exradius-runner``, the runtime's own binaries,
``VERSION``) as an image whose only content is ``/opt/exradius/runtime``. The worker copies
it into a Docker volume per bundle build, ``exradius-runtime-<runtime>-<version>-<image id>``,
and mounts that volume read-only into every run container of the runtime. Docker fills a
new, empty volume from the image's content when a container from that image mounts it, so
extraction is creating (not starting) one container.

A volume never changes once filled, so a worker on a newer bundle never alters what a
running container sees. Volumes of other builds are removed when no container uses them.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import tarfile
from collections.abc import Callable
from dataclasses import dataclass

import docker
from docker.errors import APIError, ImageNotFound, NotFound
from docker.types import Mount

from exradius.config import Settings
from exradius.runtimes import RuntimeName

log = logging.getLogger(__name__)

BUNDLE_PATH = "/opt/exradius/runtime"
"""Where the bundle image keeps the bundle (and where run containers mount it)."""
ENTRYPOINT = f"{BUNDLE_PATH}/bin/exradius-runner"
LABEL_RUNTIME = "exradius.runtime"
LABEL_VERSION = "exradius.version"
LABEL_IMAGE = "exradius.runtime_image_id"
LABEL_ROLE = "exradius.role"
ROLE_BUNDLE = "runtime-bundle"


@dataclass(frozen=True)
class RuntimeMount:
    """A ready bundle: the volume to mount for this runtime."""

    name: str
    version: str
    volume: str


@dataclass
class BundleStatus:
    name: str
    version: str | None = None
    ready: bool = False
    volume: str | None = None
    image: str | None = None
    error: str | None = None

    def report(self) -> dict:
        return {"name": self.name, "version": self.version, "ready": self.ready, "image": self.image, "error": self.error}


class BundleUnavailable(Exception):
    pass


def volume_name(runtime: str, version: str, image_id: str) -> str:
    """``exradius-runtime-claude-code-0.1.0-1a2b3c4d5e6f``: one volume per bundle build."""
    short = image_id.split(":", 1)[-1][:12]
    safe_version = re.sub(r"[^A-Za-z0-9_.-]", "_", version)
    return f"exradius-runtime-{runtime.replace('_', '-')}-{safe_version}-{short}"


def _read_member(archive: bytes, name: str) -> str:
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        member = tar.extractfile(name)
        return member.read().decode() if member else ""


class RuntimeBundles:
    def __init__(self, settings: Settings, client: Callable[[], docker.DockerClient]):
        self.settings = settings
        self._client = client
        self.status: dict[str, BundleStatus] = {name.value: BundleStatus(name.value) for name in RuntimeName}
        self._ready: dict[str, RuntimeMount] = {}  # image id -> mount
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def client(self) -> docker.DockerClient:
        return self._client()

    async def ensure(self, runtime: str) -> RuntimeMount:
        """The bundle volume for the runtime's current bundle image, extracted if needed."""
        lock = self._locks.setdefault(runtime, asyncio.Lock())
        async with lock:
            status = self.status.setdefault(runtime, BundleStatus(runtime))
            status.image = self.settings.runtime_image(runtime)
            try:
                mount = await asyncio.to_thread(self._ensure, runtime, status.image)
            except Exception as exc:
                status.ready, status.error = False, str(exc) if isinstance(exc, BundleUnavailable) else f"{type(exc).__name__}: {exc}"
                raise BundleUnavailable(f"runtime {runtime} is not available: {status.error}") from exc
            status.version, status.volume, status.ready, status.error = mount.version, mount.volume, True, None
            return mount

    def _ensure(self, runtime: str, image_ref: str) -> RuntimeMount:
        try:
            image = self.client.images.get(image_ref)
        except ImageNotFound as exc:
            raise BundleUnavailable(f"bundle image {image_ref} not found (build it with docker compose build)") from exc
        cached = self._ready.get(image.id)
        if cached is not None:
            return cached
        labels = image.labels or {}
        if labels.get(LABEL_RUNTIME) != runtime:
            raise BundleUnavailable(f"{image_ref} is not a {runtime} runtime bundle (label {LABEL_RUNTIME})")
        version = labels.get(LABEL_VERSION) or "unknown"
        name = volume_name(runtime, version, image.id)
        try:
            self.client.volumes.get(name)
        except NotFound:
            self.client.volumes.create(
                name,
                labels={LABEL_RUNTIME: runtime, LABEL_VERSION: version, LABEL_IMAGE: image.id},
            )
            log.info("extracting the %s bundle %s into volume %s", runtime, version, name)
        # Creating a container that mounts the empty volume over the bundle path makes
        # Docker copy the image's bundle into it; an already filled volume is left alone.
        # Reading VERSION through the same container checks the volume.
        container = self.client.containers.create(
            image.id,
            command=[ENTRYPOINT, "--version"],
            mounts=[Mount(target=BUNDLE_PATH, source=name, type="volume")],
            labels={LABEL_RUNTIME: runtime, "exradius.kind": "bundle-extract"},
        )
        try:
            chunks, _ = container.get_archive(f"{BUNDLE_PATH}/VERSION")
            text = _read_member(b"".join(chunks), "VERSION")
        except NotFound as exc:
            raise BundleUnavailable(f"volume {name} has no VERSION; remove it and restart the worker") from exc
        finally:
            container.remove(force=True)
        if f"exradius {version}" not in text:
            raise BundleUnavailable(f"volume {name} holds a different bundle ({text.strip()!r})")
        mount = RuntimeMount(name=runtime, version=version, volume=name)
        self._ready[image.id] = mount
        return mount

    async def ensure_all(self) -> None:
        for runtime in self.status:
            try:
                await self.ensure(runtime)
            except BundleUnavailable as exc:
                log.warning("%s", exc)

    async def remove_unused(self) -> list[str]:
        """Remove bundle volumes of other builds that no container uses."""
        current = {status.volume for status in self.status.values() if status.volume}

        def _remove() -> list[str]:
            removed = []
            for volume in self.client.volumes.list(filters={"label": LABEL_RUNTIME}):
                if volume.name in current:
                    continue
                try:
                    volume.remove()
                    removed.append(volume.name)
                except APIError as exc:  # in use by a container (409), or already gone
                    log.debug("keeping volume %s: %s", volume.name, exc)
            return removed

        removed = await asyncio.to_thread(_remove)
        if removed:
            log.info("removed old runtime bundle volumes: %s", ", ".join(removed))
        return removed

    async def report(self) -> list[dict]:
        """For the heartbeat. Re-checks each bundle image (cheap once extracted), so a bundle
        built or rebuilt after the worker started is picked up."""
        for runtime in self.status:
            try:
                await self.ensure(runtime)
            except BundleUnavailable:
                pass
        return [status.report() for status in self.status.values()]
