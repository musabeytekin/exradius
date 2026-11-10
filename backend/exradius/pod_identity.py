"""Access tokens from the cloud's pod identity (docs/kubernetes.md §14).

In a cloud, the api and the worker sign in to the object store and the build registry as
their Kubernetes ServiceAccount, never with a key from values:

- **GKE Workload Identity**: the GKE metadata server answers each pod as the Google
  identity its ServiceAccount is bound to (``GoogleMetadataToken``).
- **AKS Workload Identity**: a webhook mounts a projected ServiceAccount token into pods
  labelled ``azure.workload.identity/use: "true"`` and names the tenant and the client in
  their environment; Entra ID exchanges that token for an access token
  (``AzureWorkloadIdentityToken``).
- **EKS Pod Identity or IRSA**: boto3's default credential chain reads what EKS injects, so
  S3 and ECR need nothing from here.

Each provider caches its token until a minute before it expires. They use plain httpx, as
``worker/kube.py`` does, rather than the clouds' SDKs.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import httpx

REFRESH_MARGIN = 60.0
"""Seconds before its expiry when a cached token is fetched again."""


class PodIdentityError(RuntimeError):
    pass


class AccessTokens(Protocol):
    async def token(self) -> str:
        """A bearer token, fetched again shortly before it expires."""
        ...


class _Cached:
    def __init__(self) -> None:
        self._token: str | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        if self._token and time.monotonic() < self._expires - REFRESH_MARGIN:
            return self._token
        async with self._lock:
            if self._token and time.monotonic() < self._expires - REFRESH_MARGIN:
                return self._token
            token, lifetime = await self._fetch()
            self._token, self._expires = token, time.monotonic() + lifetime
            return token

    async def _fetch(self) -> tuple[str, float]:
        raise NotImplementedError


def _parse(response: httpx.Response, what: str) -> tuple[str, float]:
    if response.status_code != 200:
        raise PodIdentityError(f"{what}: {response.status_code} {response.text[:300]}")
    data = response.json()
    token = data.get("access_token")
    if not token:
        raise PodIdentityError(f"{what}: the response has no access_token")
    return str(token), float(data.get("expires_in") or 300)


class GoogleMetadataToken(_Cached):
    """An OAuth access token for the Google service account of this pod (GKE Workload
    Identity, or a VM's own account), from the metadata server. ``GCE_METADATA_HOST``
    overrides its address, as in Google's own libraries."""

    def __init__(self, *, host: str | None = None, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__()
        self.host = host or os.environ.get("GCE_METADATA_HOST") or "169.254.169.254"
        self.transport = transport

    async def _fetch(self) -> tuple[str, float]:
        url = f"http://{self.host}/computeMetadata/v1/instance/service-accounts/default/token"
        # Never through a proxy: the metadata server answers only the pod itself.
        async with httpx.AsyncClient(transport=self.transport, timeout=10.0, trust_env=False) as client:
            try:
                response = await client.get(url, headers={"Metadata-Flavor": "Google"})
            except httpx.HTTPError as exc:
                raise PodIdentityError(f"the GKE metadata server ({self.host}): {exc}") from exc
        return _parse(response, "the GKE metadata server")


AZURE_AUTHORITY = "https://login.microsoftonline.com/"
STORAGE_SCOPE = "https://storage.azure.com/.default"
MANAGEMENT_SCOPE = "https://management.azure.com/.default"
"""The audience ACR's ``/oauth2/exchange`` accepts from every registry."""


class AzureWorkloadIdentityToken(_Cached):
    """An Entra ID access token for ``scope``, from the federated ServiceAccount token that
    AKS Workload Identity mounts (``AZURE_FEDERATED_TOKEN_FILE``, read again at every
    refresh because the kubelet rotates it), for the client ``AZURE_CLIENT_ID`` in the tenant
    ``AZURE_TENANT_ID``."""

    def __init__(self, scope: str, *, env: Mapping[str, str] | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        super().__init__()
        env = os.environ if env is None else env
        missing = [name for name in ("AZURE_CLIENT_ID", "AZURE_TENANT_ID", "AZURE_FEDERATED_TOKEN_FILE")
                   if not env.get(name)]
        if missing:
            raise PodIdentityError(
                f"AKS Workload Identity is not set up for this pod ({', '.join(missing)} missing): its "
                'ServiceAccount needs the azure.workload.identity/client-id annotation and the pod the '
                'azure.workload.identity/use: "true" label'
            )
        self.scope = scope
        self.client_id = env["AZURE_CLIENT_ID"]
        self.tenant_id = env["AZURE_TENANT_ID"]
        self.token_file = Path(env["AZURE_FEDERATED_TOKEN_FILE"])
        self.authority = (env.get("AZURE_AUTHORITY_HOST") or AZURE_AUTHORITY).rstrip("/")
        self.transport = transport

    async def _fetch(self) -> tuple[str, float]:
        assertion = (await asyncio.to_thread(self.token_file.read_text)).strip()
        async with httpx.AsyncClient(transport=self.transport, timeout=20.0) as client:
            try:
                response = await client.post(
                    f"{self.authority}/{self.tenant_id}/oauth2/v2.0/token",
                    data={
                        "client_id": self.client_id,
                        "scope": self.scope,
                        "grant_type": "client_credentials",
                        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
                        "client_assertion": assertion,
                    },
                )
            except httpx.HTTPError as exc:
                raise PodIdentityError(f"Entra ID ({self.authority}): {exc}") from exc
        return _parse(response, "Entra ID")
