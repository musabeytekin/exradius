"""Image configs from registries, for a service's ``HEALTHCHECK`` (docs/kubernetes.md §7.4).

Kubernetes ignores Docker's ``HEALTHCHECK``. A service with ``ready: null`` keeps its
meaning (the image's healthcheck when it has one, else "started when its process starts")
because the worker reads the image's config from its registry and gives the Run spec the
healthcheck's command as the service's ``ready.exec``. Public registries are read
anonymously with the Docker registry token flow; the registries named in
``insecure_registries`` (the kind registry) over plain HTTP. The builds registry of a cloud
install (ECR, Artifact Registry, ACR), where Exradius pushes built service images, is read
with the worker's pod identity (``builds.credentialHelper``, ``registry_credentials``).
When the config cannot be read (another private registry, the network), the service counts
as started once it runs, and the worker logs why.

``ready_from_healthcheck`` is the pure mapping; ``ImageConfigs`` fetches, with a cache per
image reference.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import platform
import re
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx
from botocore.exceptions import BotoCoreError, ClientError

from exradius.pod_identity import (
    MANAGEMENT_SCOPE,
    AccessTokens,
    AzureWorkloadIdentityToken,
    GoogleMetadataToken,
    PodIdentityError,
)

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


class RegistryCredentials(Protocol):
    async def basic(self) -> tuple[str, str]:
        """A user name and password for the registry's Basic challenge or token endpoint."""
        ...


ECR_HOST = re.compile(r"^(\d{12})\.dkr\.ecr(?:-fips)?\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?$")


class EcrCredentials:
    """``ecr:GetAuthorizationToken`` through boto3's default chain (EKS Pod Identity or IRSA):
    user ``AWS`` and a password valid for 12 hours."""

    def __init__(self, registry: str, *, client: Any = None):
        match = ECR_HOST.match(registry)
        if not match:
            raise ValueError(f"{registry} is not an ECR registry (<account>.dkr.ecr.<region>.amazonaws.com)")
        self.account, self.region = match.groups()
        self._client = client
        self._cached: tuple[float, tuple[str, str]] | None = None

    async def basic(self) -> tuple[str, str]:
        if self._cached and time.time() < self._cached[0] - 600:
            return self._cached[1]

        def fetch() -> tuple[float, tuple[str, str]]:
            if self._client is None:
                import boto3

                self._client = boto3.client("ecr", region_name=self.region)
            data = self._client.get_authorization_token(registryIds=[self.account])["authorizationData"][0]
            user, _, password = base64.b64decode(data["authorizationToken"]).decode().partition(":")
            return data["expiresAt"].timestamp(), (user, password)

        self._cached = await asyncio.to_thread(fetch)
        return self._cached[1]


class GoogleRegistryCredentials:
    """Artifact Registry with the pod's Google access token (GKE Workload Identity)."""

    def __init__(self, tokens: AccessTokens | None = None):
        self.tokens = tokens or GoogleMetadataToken()

    async def basic(self) -> tuple[str, str]:
        return "oauth2accesstoken", await self.tokens.token()


ACR_USER = "00000000-0000-0000-0000-000000000000"
"""The user name of an ACR refresh token."""


class AcrCredentials:
    """ACR with the pod's Entra ID identity (AKS Workload Identity): its token for Azure
    Resource Manager is exchanged at the registry's ``/oauth2/exchange`` for a refresh token,
    which the registry's token endpoint takes as a password."""

    def __init__(self, registry: str, *, tokens: AccessTokens | None = None, tenant: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.registry = registry
        identity = tokens or AzureWorkloadIdentityToken(MANAGEMENT_SCOPE)
        self.tokens = identity
        self.tenant = tenant or getattr(identity, "tenant_id", "")
        self.transport = transport
        self._cached: tuple[float, str] | None = None

    async def basic(self) -> tuple[str, str]:
        # Refresh tokens last three hours; one is reused for half an hour.
        if self._cached and time.monotonic() < self._cached[0]:
            return ACR_USER, self._cached[1]
        async with httpx.AsyncClient(transport=self.transport, timeout=20.0) as client:
            response = await client.post(f"https://{self.registry}/oauth2/exchange", data={
                "grant_type": "access_token", "service": self.registry, "tenant": self.tenant,
                "access_token": await self.tokens.token(),
            })
        if response.status_code != 200 or not response.json().get("refresh_token"):
            raise RegistryError(f"ACR token exchange: {response.status_code} {response.text[:200]}")
        self._cached = (time.monotonic() + 1800, response.json()["refresh_token"])
        return ACR_USER, self._cached[1]


def registry_credentials(kind: str, registry: str) -> dict[str, RegistryCredentials]:
    """The credentials of the builds registry (``registry``, ``host[/path]``) for
    ``builds.credentialHelper`` ``kind``; nothing for ``none``."""
    host = registry.split("/", 1)[0]
    if kind == "none" or not host:
        return {}
    if kind == "ecr":
        return {host: EcrCredentials(host)}
    if kind == "gcr":
        return {host: GoogleRegistryCredentials()}
    if kind == "acr":
        return {host: AcrCredentials(host)}
    raise ValueError(f"unknown registry credentials {kind!r}")


class ImageConfigs:
    """Reads ``config.Healthcheck.Test`` of image references; ``None`` when there is none."""

    def __init__(self, insecure: Iterable[str] = (), *, credentials: Mapping[str, RegistryCredentials] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 15.0, arch: str | None = None):
        self.insecure = {host.strip() for host in insecure if host.strip()}
        self.credentials = dict(credentials or {})
        """Per registry host; a registry not named here is read anonymously."""
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
        except (RegistryError, PodIdentityError, BotoCoreError, ClientError, httpx.HTTPError, ValueError, KeyError,
                TypeError) as exc:
            log.warning("cannot read the image config of %s (%s): the service counts as started once it runs",
                        image, exc)
            return None  # not cached: the next run tries again
        self._cache[image] = (time.monotonic(), test)
        return test

    async def _read(self, ref: Reference) -> list[str] | None:
        scheme = "http" if ref.registry in self.insecure else "https"
        base = f"{scheme}://{ref.registry}/v2/{ref.repository}"
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout, follow_redirects=True) as client:
            session = _Session(client, ref.repository, self.credentials.get(ref.registry))
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
    """GETs with the registry's bearer token, fetched on the first 401: anonymously, or with
    ``credentials`` (also answering a Basic challenge with them)."""

    def __init__(self, client: httpx.AsyncClient, repository: str, credentials: RegistryCredentials | None = None):
        self.client = client
        self.repository = repository
        self.credentials = credentials
        self.authorization: str | None = None

    async def get_json(self, url: str, accept: str) -> dict[str, Any]:
        response = await self._get(url, accept)
        if response.status_code == 401 and self.authorization is None:
            await self._authenticate(response.headers.get("www-authenticate", ""))
            response = await self._get(url, accept)
        if response.status_code >= 300:
            raise RegistryError(f"GET {url}: {response.status_code}")
        return response.json()

    async def _get(self, url: str, accept: str) -> httpx.Response:
        headers = {"Accept": accept}
        if self.authorization:
            headers["Authorization"] = self.authorization
        return await self.client.get(url, headers=headers)

    async def _authenticate(self, challenge: str) -> None:
        scheme = challenge.split(" ", 1)[0].lower()
        if scheme == "basic" and self.credentials is not None:  # ECR
            user, password = await self.credentials.basic()
            self.authorization = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
            return
        if scheme != "bearer":
            raise RegistryError(f"the registry asks for credentials ({challenge.split(' ', 1)[0] or 'no challenge'})")
        params = dict(_CHALLENGE.findall(challenge))
        realm = params.pop("realm", None)
        if not realm:
            raise RegistryError("the registry's challenge names no realm")
        params.setdefault("scope", f"repository:{self.repository}:pull")
        auth = await self.credentials.basic() if self.credentials is not None else None
        response = await self.client.get(realm, params=params, auth=auth)
        if response.status_code >= 300:
            raise RegistryError(f"no {'token' if auth else 'anonymous token'} ({response.status_code})")
        data = response.json()
        token = data.get("token") or data.get("access_token")
        if not token:
            raise RegistryError("the token response has no token")
        self.authorization = f"Bearer {token}"
