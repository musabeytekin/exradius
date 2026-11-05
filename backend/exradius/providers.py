"""Model providers (docs/providers.md): resolution, injection and the one-time migration
of the legacy Anthropic key."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from exradius.config import Settings
from exradius.crypto import SecretBox
from exradius.db import SessionFactory
from exradius.models import Agent, Provider, Secret, utcnow
from exradius.runtimes import AuthMethod, ProviderCredential, all_runtimes, runtime_class

log = logging.getLogger(__name__)

NO_PROVIDER = "no provider configured"
LEGACY_SECRET = "ANTHROPIC_API_KEY"
MIGRATED_SLUG = "anthropic-migrated"
MIGRATED_NAME = "Anthropic (migrated)"


def find_auth_method(kind: str, auth_method: str) -> AuthMethod | None:
    """The auth method as declared by any runtime that accepts this provider kind."""
    for runtime in all_runtimes():
        provider_kind = runtime.capabilities.provider_kind(kind)
        method = provider_kind.auth_method(auth_method) if provider_kind else None
        if method is not None:
            return method
    return None


def settings_errors(method: AuthMethod, settings: dict[str, Any]) -> list[str]:
    declared = {field.name for field in method.settings}
    errors = [f"unknown setting {name!r}" for name in settings if name not in declared]
    errors += [
        f"setting {field.name!r} is required"
        for field in method.settings
        if field.required and not settings.get(field.name)
    ]
    return errors


def fingerprint(provider: Provider) -> str:
    """Changes whenever what a test exercises changes (credential, settings, kind, method)."""
    material = json.dumps(
        [provider.kind, provider.auth_method, provider.credential_encrypted, provider.settings or {}],
        sort_keys=True,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def last_test(ok: bool, detail: str | None = None, error: str | None = None, at: datetime | None = None) -> dict:
    return {"ok": ok, "tested_at": (at or utcnow()).isoformat(), "detail": detail, "error": error}


async def default_provider(session) -> Provider | None:
    return await session.scalar(select(Provider).where(Provider.is_default.is_(True)))


async def provider_count(session) -> int:
    return await session.scalar(select(func.count()).select_from(Provider)) or 0


async def resolve_provider(session, agent: Agent) -> Provider | None:
    """The agent's provider, or the default one."""
    if agent.provider_id is not None:
        return await session.get(Provider, agent.provider_id)
    return await default_provider(session)


def credential(box: SecretBox, provider: Provider) -> ProviderCredential:
    return ProviderCredential(
        kind=provider.kind,
        auth_method=provider.auth_method,
        credential=box.decrypt(provider.credential_encrypted),
        settings=dict(provider.settings or {}),
    )


def runtime_environment(runtime: str, box: SecretBox, provider: Provider) -> dict[str, str]:
    """The container variables for ``provider`` under ``runtime`` (decrypts now)."""
    return runtime_class(runtime).provider_environment(credential(box, provider))


def strip_credentials(runtime: str, env: dict[str, str]) -> dict[str, str]:
    """Drop provider variables a project defined itself: a run gets only its provider's."""
    names = runtime_class(runtime).credential_variables
    dropped = sorted(key for key in env if key in names)
    if dropped:
        log.warning("ignoring project variables %s: credentials come from the provider", ", ".join(dropped))
    return {key: value for key, value in env.items() if key not in names}


async def bootstrap_provider(sessions: SessionFactory, settings: Settings, box: SecretBox) -> uuid.UUID | None:
    """One-time migration (providers.md §3): with no provider yet, turn the legacy global
    secret ``ANTHROPIC_API_KEY`` or ``EXRADIUS_ANTHROPIC_API_KEY`` into the default provider."""
    async with sessions() as session:
        if await provider_count(session):
            return None
        secret = await session.scalar(
            select(Secret).where(Secret.project_id.is_(None), Secret.key == LEGACY_SECRET)
        )
        if secret is not None:
            key, source = box.decrypt(secret.value_encrypted), f"global secret {LEGACY_SECRET}"
        elif settings.anthropic_api_key:
            key, source = settings.anthropic_api_key, "EXRADIUS_ANTHROPIC_API_KEY"
        else:
            return None
        provider = Provider(
            slug=MIGRATED_SLUG,
            name=MIGRATED_NAME,
            kind="anthropic",
            auth_method="api_key",
            credential_encrypted=box.encrypt(key),
            settings={},
            is_default=True,
        )
        session.add(provider)
        try:
            await session.commit()
        except IntegrityError:  # another API process migrated at the same moment
            await session.rollback()
            return None
        log.warning(
            "created default provider %r from %s; that value is no longer read, manage the "
            "credential on the Providers page",
            MIGRATED_NAME,
            source,
        )
        return provider.id
