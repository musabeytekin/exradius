"""Model providers (docs/providers.md §5): named credential profiles injected into runs."""

from __future__ import annotations

import logging
import uuid
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update

from exradius.api.deps import DB, Admin, State, get_or_404
from exradius.models import Agent, Provider, ProviderTest, utcnow
from exradius.providers import (
    fingerprint,
    find_auth_method,
    last_test,
    provider_count,
    provider_models,
    settings_errors,
)
from exradius.schemas import api as s

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/providers")
NO_CONTENT = status.HTTP_204_NO_CONTENT

ANTHROPIC_API_URL = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"
TEST_TIMEOUT = 20
MODELS_SHOWN = 5
TRANSPORT: httpx.AsyncBaseTransport | None = None
"""Tests replace the HTTP transport of the API key check."""
WORKER_TESTED = {("anthropic", "oauth_token")}
"""Kind/auth method pairs with no cheap API check: a worker runs a one-line session."""


async def _agent_counts(session) -> dict[uuid.UUID, int]:
    rows = await session.execute(
        select(Agent.provider_id, func.count()).where(Agent.provider_id.is_not(None)).group_by(Agent.provider_id)
    )
    return {provider_id: count for provider_id, count in rows.all()}


async def _out(session, provider: Provider, counts: dict[uuid.UUID, int] | None = None) -> s.ProviderOut:
    counts = counts if counts is not None else await _agent_counts(session)
    count = counts.get(provider.id, 0)
    if provider.is_default:
        # Agents without a provider of their own use the default.
        count += await session.scalar(select(func.count()).select_from(Agent).where(Agent.provider_id.is_(None))) or 0
    return s.ProviderOut.model_validate(
        {
            **{c: getattr(provider, c) for c in s.ProviderOut.model_fields if c not in ("agent_count", "models")},
            "agent_count": count,
            "models": provider_models(provider.kind),
        }
    )


def _validate(kind: str, auth_method: str, settings: dict[str, str]) -> None:
    method = find_auth_method(kind, auth_method)
    if method is None:
        raise HTTPException(422, f"no runtime accepts provider kind {kind!r} with auth method {auth_method!r}")
    errors = settings_errors(method, settings)
    if errors:
        raise HTTPException(422, "; ".join(errors))


@router.get("", response_model=list[s.ProviderOut])
async def list_providers(session: DB, _: Admin):
    counts = await _agent_counts(session)
    providers = (await session.scalars(select(Provider).order_by(Provider.name, Provider.slug))).all()
    return [await _out(session, p, counts) for p in providers]


@router.post("", response_model=s.ProviderOut, status_code=201)
async def create_provider(body: s.ProviderIn, session: DB, state: State, _: Admin):
    _validate(body.kind, body.auth_method, body.settings)
    provider = Provider(
        slug=body.slug,
        name=body.name,
        kind=body.kind,
        auth_method=body.auth_method,
        credential_encrypted=state.box.encrypt(body.credential),
        settings=body.settings,
        is_default=await provider_count(session) == 0,  # the first one becomes the default
    )
    session.add(provider)
    await session.commit()
    return await _out(session, provider)


@router.get("/{provider_id}", response_model=s.ProviderOut)
async def get_provider(provider_id: uuid.UUID, session: DB, _: Admin):
    return await _out(session, await get_or_404(session, Provider, provider_id))


@router.patch("/{provider_id}", response_model=s.ProviderOut)
async def update_provider(provider_id: uuid.UUID, body: s.ProviderUpdate, session: DB, state: State, _: Admin):
    provider = await get_or_404(session, Provider, provider_id)
    data = body.model_dump(exclude_unset=True)
    for key in ("slug", "name", "kind", "auth_method"):
        if data.get(key) is None:
            data.pop(key, None)
    kind = data.get("kind", provider.kind)
    auth_method = data.get("auth_method", provider.auth_method)
    settings = data["settings"] if data.get("settings") is not None else provider.settings or {}
    _validate(kind, auth_method, settings)
    if (kind, auth_method) != (provider.kind, provider.auth_method):
        await _check_agents_accept(session, provider.id, kind, auth_method)
        if not data.get("credential"):
            raise HTTPException(422, "a new credential is required when the kind or auth method changes")

    before = fingerprint(provider)
    provider.slug = data.get("slug", provider.slug)
    provider.name = data.get("name", provider.name)
    provider.kind, provider.auth_method, provider.settings = kind, auth_method, settings
    if data.get("credential"):
        provider.credential_encrypted = state.box.encrypt(data["credential"])
    if fingerprint(provider) != before:
        provider.last_test = None  # the stored result no longer describes this provider
    provider.updated_at = utcnow()
    await session.commit()
    return await _out(session, provider)


async def _check_agents_accept(session, provider_id: uuid.UUID, kind: str, auth_method: str) -> None:
    from exradius.runtimes import runtime_class

    agents = (await session.scalars(select(Agent).where(Agent.provider_id == provider_id))).all()
    refused = [a.slug for a in agents if not runtime_class(a.runtime).capabilities.accepts(kind, auth_method)]
    if refused:
        raise HTTPException(409, f"agents {', '.join(refused)} use this provider and their runtime cannot use {kind}/{auth_method}")


@router.delete("/{provider_id}", status_code=NO_CONTENT)
async def delete_provider(provider_id: uuid.UUID, session: DB, _: Admin):
    provider = await get_or_404(session, Provider, provider_id)
    agents = (await session.scalars(select(Agent.slug).where(Agent.provider_id == provider.id).order_by(Agent.slug))).all()
    if agents:
        raise HTTPException(409, f"used by agents {', '.join(agents)}; move them to another provider first")
    if provider.is_default and await provider_count(session) > 1:
        raise HTTPException(409, "this is the default provider; make another provider the default first")
    await session.delete(provider)
    await session.commit()
    return Response(status_code=NO_CONTENT)


@router.post("/{provider_id}/default", status_code=NO_CONTENT)
async def make_default(provider_id: uuid.UUID, session: DB, _: Admin):
    provider = await get_or_404(session, Provider, provider_id)
    if not provider.is_default:
        # Clear first: the partial unique index allows one default at any moment.
        await session.execute(update(Provider).where(Provider.is_default.is_(True)).values(is_default=False))
        await session.execute(update(Provider).where(Provider.id == provider.id).values(is_default=True))
        await session.commit()
    return Response(status_code=NO_CONTENT)


async def check_model(key: str, model: str) -> s.ProviderTestResult:
    """``GET /v1/models/{model}`` with the key: the id (or alias) exists and the key may use it."""
    try:
        async with httpx.AsyncClient(base_url=ANTHROPIC_API_URL, timeout=TEST_TIMEOUT, transport=TRANSPORT) as client:
            response = await client.get(
                f"/v1/models/{quote(model, safe='')}",
                headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
            )
    except httpx.HTTPError as exc:
        return s.ProviderTestResult(ok=False, error=f"could not reach {ANTHROPIC_API_URL}: {type(exc).__name__}: {exc}")
    if response.status_code != 200:
        return s.ProviderTestResult(ok=False, error=f"model {model!r}: {_error_text(response)}")
    data = response.json() if response.content else {}
    resolved = data.get("id") if isinstance(data, dict) else None
    name = data.get("display_name") if isinstance(data, dict) else None
    detail = f"model {model} is available" + (f" ({resolved})" if resolved and resolved != model else "")
    return s.ProviderTestResult(ok=True, detail=detail + (f": {name}" if name else ""))


def _error_text(response: httpx.Response) -> str:
    try:
        message = response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = response.text[:300]
    return f"HTTP {response.status_code}: {message}"


async def check_api_key(key: str) -> s.ProviderTestResult:
    """``GET /v1/models`` with the key; success lists the first few model ids."""
    try:
        async with httpx.AsyncClient(base_url=ANTHROPIC_API_URL, timeout=TEST_TIMEOUT, transport=TRANSPORT) as client:
            response = await client.get(
                "/v1/models",
                params={"limit": 20},
                headers={"x-api-key": key, "anthropic-version": ANTHROPIC_VERSION},
            )
    except httpx.HTTPError as exc:
        return s.ProviderTestResult(ok=False, error=f"could not reach {ANTHROPIC_API_URL}: {type(exc).__name__}: {exc}")
    if response.status_code != 200:
        return s.ProviderTestResult(ok=False, error=_error_text(response))
    ids = [m.get("id") for m in response.json().get("data", []) if isinstance(m, dict)]
    shown = ", ".join(i for i in ids[:MODELS_SHOWN] if i)
    more = f" and {len(ids) - MODELS_SHOWN} more" if len(ids) > MODELS_SHOWN else ""
    return s.ProviderTestResult(ok=True, detail=f"models: {shown}{more}" if shown else "key accepted")


@router.post(
    "/{provider_id}/test",
    response_model=s.ProviderTestResult,
    responses={202: {"model": s.ProviderTestCreated}},
)
async def test_provider(
    provider_id: uuid.UUID, session: DB, state: State, _: Admin, body: s.ProviderTestIn | None = None
):
    """Test the credential, and with ``model`` that model id too. A test with a model is
    stored as ``last_test`` only when it passes: a wrong model id says nothing against
    the credential."""
    provider = await get_or_404(session, Provider, provider_id)
    model = body.model if body else None
    if (provider.kind, provider.auth_method) in WORKER_TESTED:
        test = ProviderTest(provider_id=provider.id, state="pending", fingerprint=fingerprint(provider), model=model)
        session.add(test)
        await session.commit()
        try:
            await state.broker.test_provider(str(test.id))
        except Exception as exc:  # noqa: BLE001 - reported
            test.state, test.ended_at = "failed", utcnow()
            test.error = f"could not reach the broker: {exc}"
            await session.commit()
            raise HTTPException(503, f"could not reach the broker to start the test: {exc}") from exc
        return JSONResponse({"test_id": str(test.id)}, status_code=202)

    if (provider.kind, provider.auth_method) != ("anthropic", "api_key"):
        raise HTTPException(422, f"no test for {provider.kind}/{provider.auth_method}")
    tested = fingerprint(provider)
    key = state.box.decrypt(provider.credential_encrypted)
    result = await (check_model(key, model) if model else check_api_key(key))
    await session.refresh(provider)
    if fingerprint(provider) == tested and (model is None or result.ok):
        provider.last_test = last_test(result.ok, result.detail, result.error)
        await session.commit()
    return result


@router.get("/{provider_id}/tests/{test_id}", response_model=s.ProviderTestOut)
async def get_provider_test(provider_id: uuid.UUID, test_id: uuid.UUID, session: DB, _: Admin):
    test = await session.get(ProviderTest, test_id)
    if test is None or test.provider_id != provider_id:
        raise HTTPException(404, "ProviderTest not found")
    return test
