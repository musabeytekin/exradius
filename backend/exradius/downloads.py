"""Artifact URLs require bridge authentication; transcript URLs retain signed access.

Artifact downloads go through the integration API with a bridge token. The admin uses
its session and the separate admin artifact route. Transcript signatures use the current
or previous download signing key and retain the existing non-expiring behavior.
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


def _hmac(key: bytes, path: str) -> str:
    return hmac.new(key, path.encode(), hashlib.sha256).hexdigest()


def sign(settings: Settings, path: str) -> str:
    return _hmac(settings.download_signing_key, path)


def verify(settings: Settings, path: str, signature: str | None) -> bool:
    if not signature:
        return False
    return any(hmac.compare_digest(_hmac(key, path), signature) for key in settings.download_verify_keys)


def signed_url(settings: Settings, path: str) -> str:
    return f"{settings.public_api_url.rstrip('/')}{path}?sig={sign(settings, path)}"


def artifact_url(settings: Settings, artifact_id: uuid.UUID | str) -> str:
    return f"{settings.public_api_url.rstrip('/')}/api/integration/v1/artifacts/{artifact_id}/content"


def transcript_url(settings: Settings, run_id: uuid.UUID | str) -> str:
    return signed_url(settings, transcript_path(run_id))
