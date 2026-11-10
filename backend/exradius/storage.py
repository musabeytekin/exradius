"""Object store for transcripts, artifacts and attachments.

Installs use one of three backends (docs/kubernetes.md §14): an S3 API (SeaweedFS in a local
kind install, S3 on AWS), Google Cloud Storage, or Azure Blob Storage. In clouds each signs
in with the pod's identity (EKS Pod Identity or IRSA through boto3's default chain; GKE and
AKS Workload Identity through ``exradius/pod_identity.py``), so no key is in values. The
filesystem and in-memory stores are for tests. Objects are addressed by key only; consumers
download them through the API (see ``exradius/downloads.py``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import os
import tempfile
from collections.abc import Awaitable, Callable
from email.utils import formatdate
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qsl, quote

import httpx

from exradius.config import Settings
from exradius.pod_identity import (
    STORAGE_SCOPE,
    AccessTokens,
    AzureWorkloadIdentityToken,
    GoogleMetadataToken,
)


class ObjectNotFound(KeyError):
    pass


class InvalidObjectKey(ValueError):
    pass


class ObjectStoreError(RuntimeError):
    """The store answered with an error other than "no such object"."""


class ObjectStore(Protocol):
    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        """Store ``data`` under ``key``, replacing any existing object."""
        ...

    async def get(self, key: str) -> bytes:
        """Return the object's bytes; raise ``ObjectNotFound`` if there is none."""
        ...

    async def ensure_bucket(self) -> None: ...


def validate_key(key: str) -> list[str]:
    """Split a key into path segments, refusing anything that could leave the store."""
    if not key or "\x00" in key or "\\" in key or key.startswith("/"):
        raise InvalidObjectKey(f"invalid object key {key!r}")
    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise InvalidObjectKey(f"invalid object key {key!r}")
    return parts


class FilesystemObjectStore:
    """Objects under ``<root>/<bucket>/<key>``; for tests."""

    def __init__(self, root: Path, bucket: str = "exradius"):
        validate_key(bucket)
        if "/" in bucket:
            raise InvalidObjectKey(f"invalid bucket {bucket!r}")
        self.bucket = bucket
        self.root = (root / bucket).resolve()

    def path(self, key: str) -> Path:
        path = self.root.joinpath(*validate_key(key))
        # Belt and braces: symlinks inside the store must not lead out of it either.
        if not path.resolve().is_relative_to(self.root):
            raise InvalidObjectKey(f"invalid object key {key!r}")
        return path

    def _put(self, key: str, data: bytes) -> None:
        target = self.path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, target)  # readers never see a partial object
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def _get(self, key: str) -> bytes:
        try:
            return self.path(key).read_bytes()
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError) as exc:
            raise ObjectNotFound(key) from exc

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        await asyncio.to_thread(self._put, key, data)

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._get, key)

    async def ensure_bucket(self) -> None:
        await asyncio.to_thread(self.root.mkdir, parents=True, exist_ok=True)


class S3ObjectStore:
    def __init__(self, settings: Settings):
        import boto3
        from botocore.config import Config

        self.bucket = settings.bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            # None and None: boto3's default chain (EKS Pod Identity, IRSA).
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            # Path-style for S3-compatible services (SeaweedFS); AWS itself prefers
            # virtual-hosted addresses.
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if settings.s3_endpoint_url else "virtual"},
                retries={"mode": "standard"},
            ),
        )

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        validate_key(key)
        await asyncio.to_thread(
            self._client.put_object, Bucket=self.bucket, Key=key, Body=data, ContentType=content_type
        )

    async def get(self, key: str) -> bytes:
        from botocore.exceptions import ClientError

        def _get() -> bytes:
            try:
                response = self._client.get_object(Bucket=self.bucket, Key=key)
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
                    raise ObjectNotFound(key) from exc
                raise
            return response["Body"].read()

        return await asyncio.to_thread(_get)

    async def ensure_bucket(self) -> None:
        from botocore.exceptions import ClientError

        def _ensure() -> None:
            try:
                self._client.head_bucket(Bucket=self.bucket)
            except ClientError:
                try:
                    self._client.create_bucket(Bucket=self.bucket)
                except ClientError as exc:  # created concurrently by another process
                    if exc.response.get("Error", {}).get("Code") not in {
                        "BucketAlreadyOwnedByYou",
                        "BucketAlreadyExists",
                    }:
                        raise

        await asyncio.to_thread(_ensure)


RETRIES = 3
"""Attempts of a request to GCS or Azure that failed with a connection error, 408, 429 or 5xx
(boto3 retries S3 requests the same way)."""
RETRY_STATUS = {408, 429, 500, 502, 503, 504}


async def _send(
    transport: httpx.AsyncBaseTransport | None,
    build: Callable[[httpx.AsyncClient], Awaitable[httpx.Request]],
) -> httpx.Response:
    """Send the request ``build`` makes (an awaitable, so it can fetch a token first), retrying
    transient failures with a short backoff. Every request is idempotent: a put replaces."""
    async with httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        for attempt in range(RETRIES):
            last = attempt == RETRIES - 1
            try:
                request = await build(client)
                response = await client.send(request)
            except httpx.TransportError:
                if last:
                    raise
            else:
                if response.status_code not in RETRY_STATUS or last:
                    return response
            await asyncio.sleep(0.5 * 2**attempt)
    raise AssertionError("unreachable")


def _error(service: str, request: str, response: httpx.Response) -> ObjectStoreError:
    return ObjectStoreError(f"{service} {request}: {response.status_code} {response.text[:300]}")


class GCSObjectStore:
    """Google Cloud Storage through its JSON API, as the pod's Google service account (GKE
    Workload Identity). The bucket is made by the installer (deploy/terraform/gcp): the
    identity has object rights on it only, so ``ensure_bucket`` checks and never creates."""

    def __init__(self, bucket: str, *, endpoint: str | None = None, tokens: AccessTokens | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.bucket = bucket
        self.endpoint = (endpoint or "https://storage.googleapis.com").rstrip("/")
        self.tokens = tokens
        self.transport = transport

    async def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = dict(extra or {})
        if self.tokens is not None:
            headers["Authorization"] = f"Bearer {await self.tokens.token()}"
        return headers

    def _object_url(self, key: str) -> str:
        return f"{self.endpoint}/storage/v1/b/{quote(self.bucket, safe='')}/o/{quote(key, safe='')}"

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        validate_key(key)

        async def build(client: httpx.AsyncClient) -> httpx.Request:
            return client.build_request(
                "POST", f"{self.endpoint}/upload/storage/v1/b/{quote(self.bucket, safe='')}/o",
                params={"uploadType": "media", "name": key},
                content=data, headers=await self._headers({"Content-Type": content_type}),
            )

        response = await _send(self.transport, build)
        if response.status_code != 200:
            raise _error("GCS", f"upload {key}", response)

    async def get(self, key: str) -> bytes:
        validate_key(key)

        async def build(client: httpx.AsyncClient) -> httpx.Request:
            return client.build_request("GET", self._object_url(key), params={"alt": "media"},
                                        headers=await self._headers())

        response = await _send(self.transport, build)
        if response.status_code == 404:
            raise ObjectNotFound(key)
        if response.status_code != 200:
            raise _error("GCS", f"download {key}", response)
        return response.content

    async def ensure_bucket(self) -> None:
        # Listing needs only object rights (storage.objects.list); reading the bucket itself
        # would need storage.buckets.get, which the identity is not given.
        async def build(client: httpx.AsyncClient) -> httpx.Request:
            return client.build_request(
                "GET", f"{self.endpoint}/storage/v1/b/{quote(self.bucket, safe='')}/o",
                params={"maxResults": "1", "fields": "kind"}, headers=await self._headers(),
            )

        response = await _send(self.transport, build)
        if response.status_code == 404:
            raise ObjectStoreError(f"the GCS bucket {self.bucket} does not exist; the installer creates it")
        if response.status_code != 200:
            raise _error("GCS", f"list {self.bucket}", response)


AZURE_VERSION = "2023-11-03"
"""The Blob service's REST API version (``x-ms-version``); OAuth needs 2017-11-09 or later,
and single PUTs of up to 5000 MiB 2019-12-12 or later."""


class AzureBlobObjectStore:
    """Azure Blob Storage through its REST API: with a Shared Key from a Secret (clusters
    outside AKS, Azurite), or as the pod's Entra ID identity (AKS Workload Identity) with the
    "Storage Blob Data Contributor" role on the container. ``bucket`` is the container."""

    def __init__(self, account: str, container: str, *, endpoint: str | None = None,
                 account_key: str | None = None, tokens: AccessTokens | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        if (account_key is None) == (tokens is None):
            raise ValueError("an Azure store signs in with either an account key or tokens")
        self.account = account
        self.container = container
        self.endpoint = (endpoint or f"https://{account}.blob.core.windows.net").rstrip("/")
        self.key = base64.b64decode(account_key) if account_key is not None else None
        self.tokens = tokens
        self.transport = transport

    def _url(self, key: str | None = None) -> str:
        url = f"{self.endpoint}/{quote(self.container, safe='')}"
        return url if key is None else f"{url}/{quote(key, safe='/')}"

    async def _request(self, client: httpx.AsyncClient, method: str, url: str, *, params=None,
                       content: bytes | None = None, headers: dict[str, str] | None = None) -> httpx.Request:
        headers = {"x-ms-version": AZURE_VERSION, "x-ms-date": formatdate(usegmt=True), **(headers or {})}
        if self.tokens is not None:
            headers["Authorization"] = f"Bearer {await self.tokens.token()}"
        request = client.build_request(method, url, params=params, content=content, headers=headers)
        if self.key is not None:
            request.headers["Authorization"] = f"SharedKey {self.account}:{self._signature(request)}"
        return request

    def _signature(self, request: httpx.Request) -> str:
        """Shared Key signature, version 2015-02-21 and later (Content-Length empty when 0)."""
        h = request.headers
        length = h.get("content-length", "")
        standard = [
            request.method, h.get("content-encoding", ""), h.get("content-language", ""),
            "" if length == "0" else length, h.get("content-md5", ""), h.get("content-type", ""),
            "",  # Date: x-ms-date is used instead
            h.get("if-modified-since", ""), h.get("if-match", ""), h.get("if-none-match", ""),
            h.get("if-unmodified-since", ""), h.get("range", ""),
        ]
        ms = sorted((name.lower(), " ".join(value.split())) for name, value in h.items()
                    if name.lower().startswith("x-ms-"))
        path, _, query = request.url.raw_path.decode().partition("?")
        resource = f"/{self.account}{path}"
        params: dict[str, list[str]] = {}
        for name, value in parse_qsl(query, keep_blank_values=True):
            params.setdefault(name.lower(), []).append(value)
        for name in sorted(params):
            resource += f"\n{name}:{','.join(sorted(params[name]))}"
        text = "\n".join(standard) + "\n" + "".join(f"{n}:{v}\n" for n, v in ms) + resource
        assert self.key is not None
        return base64.b64encode(hmac.new(self.key, text.encode(), hashlib.sha256).digest()).decode()

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        validate_key(key)
        response = await _send(self.transport, lambda client: self._request(
            client, "PUT", self._url(key), content=data,
            headers={"x-ms-blob-type": "BlockBlob", "Content-Type": content_type},
        ))
        if response.status_code != 201:
            raise _error("Azure Blob", f"PUT {key}", response)

    async def get(self, key: str) -> bytes:
        validate_key(key)
        response = await _send(self.transport, lambda client: self._request(client, "GET", self._url(key)))
        if response.status_code == 404:
            raise ObjectNotFound(key)
        if response.status_code != 200:
            raise _error("Azure Blob", f"GET {key}", response)
        return response.content

    async def ensure_bucket(self) -> None:
        params = {"restype": "container"}
        response = await _send(self.transport, lambda client: self._request(
            client, "HEAD", self._url(), params=params))
        if response.status_code == 200:
            return
        if response.status_code != 404:
            raise _error("Azure Blob", f"HEAD container {self.container}", response)
        response = await _send(self.transport, lambda client: self._request(
            client, "PUT", self._url(), params=params))
        if response.status_code not in (201, 409):  # 409: created meanwhile
            raise _error("Azure Blob", f"create container {self.container}", response)


class MemoryObjectStore:
    """In-memory store for tests."""

    def __init__(self, bucket: str = "exradius"):
        self.bucket = bucket
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        validate_key(key)
        self.objects[key] = (data, content_type)

    async def get(self, key: str) -> bytes:
        try:
            return self.objects[key][0]
        except KeyError as exc:
            raise ObjectNotFound(key) from exc

    async def ensure_bucket(self) -> None:
        return None


def make_store(settings: Settings) -> ObjectStore:
    if settings.object_store == "gcs":
        return GCSObjectStore(
            settings.bucket, endpoint=settings.gcs_endpoint,
            tokens=None if settings.gcs_endpoint else GoogleMetadataToken(),
        )
    if settings.object_store == "azure":
        if not settings.azure_account:
            raise ValueError("EXRADIUS_AZURE_ACCOUNT must name the storage account")
        key = settings.azure_account_key
        return AzureBlobObjectStore(
            settings.azure_account, settings.bucket, endpoint=settings.azure_endpoint, account_key=key,
            tokens=None if key else AzureWorkloadIdentityToken(STORAGE_SCOPE),
        )
    return S3ObjectStore(settings)
