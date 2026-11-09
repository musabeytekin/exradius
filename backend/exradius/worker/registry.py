"""Image configs from registries, for a service's ``HEALTHCHECK`` (docs/kubernetes.md §7.4).

Kubernetes ignores Docker's ``HEALTHCHECK``. A service with ``ready: null`` keeps its
meaning (the image's healthcheck when it has one, else "started when its process starts")
because the worker reads the image's config from its registry and gives the Run spec the
healthcheck's command as the service's ``ready.exec``. Public registries are read
anonymously with the Docker registry token flow; the registries named in
``insecure_registries`` (the kind registry) over plain HTTP. When the config cannot be read
(a private registry, the network), the service counts as started once it runs, and the
worker logs why.

``ready_from_healthcheck`` is the pure mapping; ``ImageConfigs`` fetches, with a cache per
image reference.
"""

from __future__ import annotations

import logging
import platform
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import httpx

log = logging.getLogger(__name__)

DOCKER_HUB = "registry-1.docker.io"
MANIFESTS = ", ".join((
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
))
INDEXES = ("application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json")
CACHE_SECONDS = 3600.0
"""How long a tag's healthcheck is reused: tags move, healthchecks rarely do."""


@dataclass(frozen=True)
class Reference:
    registry: str
    """``host[:port]``; Docker Hub is ``registry-1.docker.io``."""
    repository: str
    reference: str
    """A tag or a digest."""


def parse_reference(image: str) -> Reference:
    """An image name as Docker normalises it: ``postgres:16`` is
    ``registry-1.docker.io/library/postgres:16``."""
    name, _, digest = image.partition("@")
    tag = None
    if ":" in name.rsplit("/", 1)[-1]:
        name, tag = name.rsplit(":", 1)
    first, _, rest = name.partition("/")
    if rest and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, rest
    else:
        registry, repository = "docker.io", name
    if registry in ("docker.io", "index.docker.io"):
        registry = DOCKER_HUB
        if "/" not in repository:
            repository = f"library/{repository}"
    return Reference(registry, repository, digest or tag or "latest")


def ready_from_healthcheck(test: list[str] | None) -> dict[str, list[str]] | None:
    """A ``HEALTHCHECK`` test as a Run spec ``ready``: ``CMD`` runs its arguments,
    ``CMD-SHELL`` its command through ``/bin/sh -c``; ``NONE``, none and anything else is no
    check."""
    if not test:
        return None
    kind, args = test[0], [str(a) for a in test[1:]]
    if kind == "CMD" and args:
        return {"exec": args}
    if kind == "CMD-SHELL" and args:
        return {"exec": ["/bin/sh", "-c", args[0]]}
    return None


def _arch() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "aarch64": "arm64"}.get(machine, machine)


_CHALLENGE = re.compile(r'(\w+)="([^"]*)"')


class RegistryError(Exception):
    pass


class ImageConfigs:
    """Reads ``config.Healthcheck.Test`` of image references; ``None`` when there is none."""

    def __init__(self, insecure: Iterable[str] = (), *, transport: httpx.AsyncBaseTransport | None = None,
                 timeout: float = 15.0, arch: str | None = None):
        self.insecure = {host.strip() for host in insecure if host.strip()}
        self.transport = transport
        self.timeout = timeout
        self.arch = arch or _arch()
        self._cache: dict[str, tuple[float, list[str] | None]] = {}

    async def healthcheck(self, image: str) -> list[str] | None:
        cached = self._cache.get(image)
        if cached is not None and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        try:
            test = await self._read(parse_reference(image))
        except (RegistryError, httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            log.warning("cannot read the image config of %s (%s): the service counts as started once it runs",
                        image, exc)
            return None  # not cached: the next run tries again
        self._cache[image] = (time.monotonic(), test)
        return test

    async def _read(self, ref: Reference) -> list[str] | None:
        scheme = "http" if ref.registry in self.insecure else "https"
        base = f"{scheme}://{ref.registry}/v2/{ref.repository}"
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout, follow_redirects=True) as client:
            session = _Session(client, ref.repository)
            manifest = await session.get_json(f"{base}/manifests/{ref.reference}", MANIFESTS)
            if manifest.get("mediaType") in INDEXES or "manifests" in manifest:
                digest = self._pick(manifest)
                manifest = await session.get_json(f"{base}/manifests/{digest}", MANIFESTS)
            config = await session.get_json(f"{base}/blobs/{manifest['config']['digest']}", "*/*")
        test = ((config.get("config") or {}).get("Healthcheck") or {}).get("Test")
        return list(test) if test else None

    def _pick(self, index: dict[str, Any]) -> str:
        """The manifest for linux on this worker's architecture, else amd64, else the first."""
        candidates = [m for m in index.get("manifests") or []
                      if (m.get("platform") or {}).get("os") == "linux"]
        for arch in (self.arch, "amd64"):
            for entry in candidates:
                if entry["platform"].get("architecture") == arch:
                    return entry["digest"]
        if not candidates:
            raise RegistryError("the image index has no linux manifest")
        return candidates[0]["digest"]


class _Session:
    """GETs with the registry's anonymous bearer token, fetched on the first 401."""

    def __init__(self, client: httpx.AsyncClient, repository: str):
        self.client = client
        self.repository = repository
        self.token: str | None = None

    async def get_json(self, url: str, accept: str) -> dict[str, Any]:
        response = await self._get(url, accept)
        if response.status_code == 401 and self.token is None:
            await self._authenticate(response.headers.get("www-authenticate", ""))
            response = await self._get(url, accept)
        if response.status_code >= 300:
            raise RegistryError(f"GET {url}: {response.status_code}")
        return response.json()

    async def _get(self, url: str, accept: str) -> httpx.Response:
        headers = {"Accept": accept}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return await self.client.get(url, headers=headers)

    async def _authenticate(self, challenge: str) -> None:
        if not challenge.lower().startswith("bearer "):
            raise RegistryError(f"the registry asks for credentials ({challenge.split(' ', 1)[0] or 'no challenge'})")
        params = dict(_CHALLENGE.findall(challenge))
        realm = params.pop("realm", None)
        if not realm:
            raise RegistryError("the registry's challenge names no realm")
        params.setdefault("scope", f"repository:{self.repository}:pull")
        response = await self.client.get(realm, params=params)
        if response.status_code >= 300:
            raise RegistryError(f"no anonymous token ({response.status_code})")
        data = response.json()
        self.token = data.get("token") or data.get("access_token")
        if not self.token:
            raise RegistryError("the token response has no token")
