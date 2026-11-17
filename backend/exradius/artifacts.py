"""Artifact persistence and limits, independent of result submission."""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import uuid
from datetime import timedelta
from urllib.parse import quote

from fastapi import HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from exradius.artifact_limits import MAX_FILE_BYTES
from exradius.models import Artifact, Run, utcnow
from exradius.storage import ObjectNotFound, ObjectStore

log = logging.getLogger(__name__)

MAX_RUN_BYTES = 250 * 1024 * 1024
MAX_RUN_FILES = 100
UPLOADS_PER_MINUTE = 20
# The legacy result endpoint accepts base64; bound its body before JSON parsing too.
MAX_RESULT_BYTES = MAX_FILE_BYTES * 4 // 3 + 1024 * 1024


async def read_body(request: Request, limit: int) -> bytes:
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > limit:
                raise HTTPException(413, f"request exceeds {limit} bytes")
        except ValueError as exc:
            raise HTTPException(400, "invalid content length") from exc
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, f"request exceeds {limit} bytes")
        body.extend(chunk)
    return bytes(body)


async def lock_run(session: AsyncSession, run_id: uuid.UUID) -> Run:
    """Serialize quota checks and selections with finalization and other uploads."""
    run = await session.get(Run, run_id, with_for_update={"of": Run}, populate_existing=True)
    if run is None or run.ended_at is not None or run.token_hash is None:
        raise HTTPException(401, "run has ended")
    return run


async def record_upload_attempt(session: AsyncSession, run_id: uuid.UUID) -> None:
    run = await lock_run(session, run_id)
    now = utcnow()
    start = run.artifact_upload_started_at
    if start is None or start.replace(tzinfo=now.tzinfo) <= now - timedelta(minutes=1):
        run.artifact_upload_started_at = now
        run.artifact_upload_count = 0
    if run.artifact_upload_count >= UPLOADS_PER_MINUTE:
        raise HTTPException(429, "too many artifact uploads; retry in one minute", headers={"Retry-After": "60"})
    run.artifact_upload_count += 1
    await session.commit()  # failed uploads count too; release the lock while receiving bytes


async def save(session: AsyncSession, store: ObjectStore, run: Run, name: str, content: bytes) -> Artifact:
    """Caller holds the run lock. Same name and bytes return the existing immutable file."""
    if len(content) > MAX_FILE_BYTES:
        raise HTTPException(413, f"a file is at most {MAX_FILE_BYTES} bytes")
    digest = hashlib.sha256(content).hexdigest()
    artifact_id = uuid.uuid5(run.id, f"{name}\0{digest}")
    existing = await session.get(Artifact, artifact_id)
    if existing is not None:
        return existing
    files = (await session.scalars(select(Artifact).where(Artifact.run_id == run.id))).all()
    if len(files) >= MAX_RUN_FILES or sum(f.size or 0 for f in files) + len(content) > MAX_RUN_BYTES:
        raise HTTPException(413, "the run's artifact quota is exhausted")
    key = f"runs/{run.id}/artifacts/{artifact_id}"
    content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    await store.put(key, content, "application/octet-stream")
    artifact = Artifact(id=artifact_id, run_id=run.id, name=name, content_type=content_type,
                        object_key=key, size=len(content), sha256=digest, selected=False)
    session.add(artifact)
    await session.flush()
    log.info("artifact stored run=%s artifact=%s size=%s", run.id, artifact.id, artifact.size)
    return artifact


async def download(store: ObjectStore, artifact: Artifact, principal: str) -> Response:
    try:
        content = await store.get(artifact.object_key)
    except ObjectNotFound as exc:
        raise HTTPException(404, "artifact content not found") from exc
    log.info("artifact downloaded run=%s artifact=%s principal=%s", artifact.run_id, artifact.id, principal)
    return Response(content, media_type="application/octet-stream", headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(artifact.name, safe='')}",
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": "sandbox; default-src 'none'",
    })
