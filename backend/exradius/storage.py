"""Object store for transcripts, artifacts and attachments.

The default keeps objects on the filesystem under ``<data_dir>/objects/<bucket>/<key>``;
``EXRADIUS_OBJECT_STORE=s3`` stores them in any S3 API instead. Objects are addressed by
key only; consumers download them through the API (see ``exradius/downloads.py``).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from typing import Protocol

from exradius.config import Settings


class ObjectNotFound(KeyError):
    pass


class InvalidObjectKey(ValueError):
    pass


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

        self.bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
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
    if settings.object_store == "s3":
        return S3ObjectStore(settings)
    return FilesystemObjectStore(settings.objects_dir, settings.s3_bucket)
