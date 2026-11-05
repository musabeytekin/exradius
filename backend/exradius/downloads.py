"""HTTP download URLs for artifacts and transcripts, as published in ``task.result``.

The consumer of outbound events is the user's integration layer, which has no admin
session, so each URL carries a signature (``?sig=``): an HMAC of its path, keyed by a
value derived from ``EXRADIUS_SECRET_KEY``. The API accepts either the admin session or a
valid signature. Signatures do not expire in version one.
"""

from __future__ import annotations

import hashlib
import hmac
import uuid

from exradius.config import Settings


def artifact_path(artifact_id: uuid.UUID | str) -> str:
    return f"/api/artifacts/{artifact_id}/content"


def transcript_path(run_id: uuid.UUID | str) -> str:
    return f"/api/runs/{run_id}/transcript"


def transcript_key(run_id: uuid.UUID | str) -> str:
    """Object store key of a run's transcript (JSON lines)."""
    return f"runs/{run_id}/transcript.jsonl"


def sign(settings: Settings, path: str) -> str:
    return hmac.new(settings.download_signing_key, path.encode(), hashlib.sha256).hexdigest()


def verify(settings: Settings, path: str, signature: str | None) -> bool:
    return bool(signature) and hmac.compare_digest(sign(settings, path), signature)


def signed_url(settings: Settings, path: str) -> str:
    return f"{settings.public_api_url.rstrip('/')}{path}?sig={sign(settings, path)}"


def artifact_url(settings: Settings, artifact_id: uuid.UUID | str) -> str:
    return signed_url(settings, artifact_path(artifact_id))


def transcript_url(settings: Settings, run_id: uuid.UUID | str) -> str:
    return signed_url(settings, transcript_path(run_id))
