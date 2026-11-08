"""The worker's Kubernetes client (docs/kubernetes.md §3): Runs and per-run Secrets in the
runs namespace, and the operator's Lease.

What the worker may do there is its RBAC: create, patch, delete, get, list and watch Runs;
create, patch and delete Secrets (never read them); get the operator's Lease. It never
creates pods. The client speaks to the API server over HTTPS with the pod's
ServiceAccount token, which the kubelet rotates, so the token file is re-read.

``Cluster`` is the protocol ``RunManager`` uses; the tests give it a fake.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

GROUP = "exradius.dev"
VERSION = "v1alpha1"
API_VERSION = f"{GROUP}/{VERSION}"
SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TOKEN_REFRESH_SECONDS = 60


class KubeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"Kubernetes API {status}: {message}")
        self.status = status
        self.message = message


class Gone(KubeError):
    """A watch's resourceVersion is too old (410): list again."""


class Cluster(Protocol):
    async def create_run(self, body: dict[str, Any]) -> dict[str, Any]:
        """Create a Run; an existing one of that name is returned as it is."""
        ...

    async def get_run(self, name: str) -> dict[str, Any] | None: ...

    async def list_runs(self) -> tuple[list[dict[str, Any]], str]:
        """Every Run in the runs namespace and the list's resourceVersion."""
        ...

    async def patch_run(self, name: str, spec: dict[str, Any]) -> dict[str, Any] | None:
        """Merge ``spec`` into the Run's spec; ``None`` when there is no such Run."""
        ...

    async def delete_run(self, name: str) -> bool:
        """Delete the Run (its pod, workspace and Secret go with it); ``False`` if absent."""
        ...

    async def create_secret(self, body: dict[str, Any]) -> None: ...

    async def delete_secret(self, name: str) -> None: ...

    def watch_runs(self, resource_version: str) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """``(type, object)`` from ``resource_version`` on; raises ``Gone`` when it expired."""
        ...

    async def get_lease(self, namespace: str, name: str) -> dict[str, Any] | None: ...


class KubeCluster:
    def __init__(self, runs_namespace: str, api_url: str | None = None, token_file: Path | None = None,
                 ca_file: Path | None = None):
        self.namespace = runs_namespace
        if api_url is None:
            host = os.environ.get("KUBERNETES_SERVICE_HOST")
            port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
            if not host:
                raise RuntimeError("not in a Kubernetes pod (KUBERNETES_SERVICE_HOST is unset) and no API URL given")
            api_url = f"https://{host}:{port}" if ":" not in host else f"https://[{host}]:{port}"
        self.api_url = api_url.rstrip("/")
        self.token_file = token_file or SERVICE_ACCOUNT / "token"
        ca = ca_file or SERVICE_ACCOUNT / "ca.crt"
        verify: ssl.SSLContext | bool = ssl.create_default_context(cafile=str(ca)) if ca.exists() else True
        self._client = httpx.AsyncClient(base_url=self.api_url, verify=verify, timeout=30)
        self._token: str | None = None
        self._token_read = 0.0

    def _headers(self, content_type: str | None = None) -> dict[str, str]:
        if self._token is None or time.monotonic() - self._token_read > TOKEN_REFRESH_SECONDS:
            try:
                self._token = self.token_file.read_text().strip()
            except OSError:
                self._token = None
            self._token_read = time.monotonic()
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    def _runs(self, name: str | None = None) -> str:
        path = f"/apis/{GROUP}/{VERSION}/namespaces/{self.namespace}/runs"
        return f"{path}/{name}" if name else path

    def _secrets(self, name: str | None = None) -> str:
        path = f"/api/v1/namespaces/{self.namespace}/secrets"
        return f"{path}/{name}" if name else path

    async def _request(self, method: str, path: str, *, body: Any = None, content_type: str = "application/json",
                       params: dict | None = None) -> httpx.Response:
        response = await self._client.request(
            method, path, params=params, headers=self._headers(content_type if body is not None else None),
            content=json.dumps(body) if body is not None else None,
        )
        return response

    @staticmethod
    def _error(response: httpx.Response) -> KubeError:
        try:
            message = response.json().get("message") or response.text
        except ValueError:
            message = response.text
        return (Gone if response.status_code == 410 else KubeError)(response.status_code, message[:500])

    async def create_run(self, body: dict[str, Any]) -> dict[str, Any]:
        response = await self._request("POST", self._runs(), body=body)
        if response.status_code == 409:
            existing = await self.get_run(body["metadata"]["name"])
            if existing is not None:
                return existing
        if response.status_code >= 300:
            raise self._error(response)
        return response.json()

    async def get_run(self, name: str) -> dict[str, Any] | None:
        response = await self._request("GET", self._runs(name))
        if response.status_code == 404:
            return None
        if response.status_code >= 300:
            raise self._error(response)
        return response.json()

    async def list_runs(self) -> tuple[list[dict[str, Any]], str]:
        response = await self._request("GET", self._runs())
        if response.status_code >= 300:
            raise self._error(response)
        data = response.json()
        return list(data.get("items") or []), data.get("metadata", {}).get("resourceVersion", "")

    async def patch_run(self, name: str, spec: dict[str, Any]) -> dict[str, Any] | None:
        response = await self._request("PATCH", self._runs(name), body={"spec": spec},
                                       content_type="application/merge-patch+json")
        if response.status_code == 404:
            return None
        if response.status_code >= 300:
            raise self._error(response)
        return response.json()

    async def delete_run(self, name: str) -> bool:
        response = await self._request("DELETE", self._runs(name), body={"propagationPolicy": "Background"})
        if response.status_code == 404:
            return False
        if response.status_code >= 300:
            raise self._error(response)
        return True

    async def create_secret(self, body: dict[str, Any]) -> None:
        response = await self._request("POST", self._secrets(), body=body)
        if response.status_code == 409:
            # Left by an earlier attempt for the same Run: replace its data and owner.
            response = await self._request("PATCH", self._secrets(body["metadata"]["name"]), body=body,
                                           content_type="application/merge-patch+json")
        if response.status_code >= 300:
            raise self._error(response)

    async def delete_secret(self, name: str) -> None:
        response = await self._request("DELETE", self._secrets(name))
        if response.status_code not in (200, 202, 404):
            raise self._error(response)

    async def watch_runs(self, resource_version: str) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        params = {"watch": "1", "resourceVersion": resource_version, "allowWatchBookmarks": "true",
                  "timeoutSeconds": "300"}
        async with self._client.stream("GET", self._runs(), params=params, headers=self._headers(),
                                       timeout=httpx.Timeout(30, read=330)) as response:
            if response.status_code >= 300:
                await response.aread()
                raise self._error(response)
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                event = json.loads(line)
                kind, obj = event.get("type"), event.get("object") or {}
                if kind == "ERROR":
                    code = int(obj.get("code") or 500)
                    raise (Gone if code == 410 else KubeError)(code, str(obj.get("message") or ""))
                yield kind, obj

    async def get_lease(self, namespace: str, name: str) -> dict[str, Any] | None:
        response = await self._request("GET", f"/apis/coordination.k8s.io/v1/namespaces/{namespace}/leases/{name}")
        if response.status_code == 404:
            return None
        if response.status_code >= 300:
            raise self._error(response)
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
