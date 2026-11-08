"""System information for the overview, integration and settings pages (onboarding §4)."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import unquote, urlsplit

from sqlalchemy import select, text

from fastapi import APIRouter

from exradius import messaging
from exradius.api.deps import DB, Admin, State
from exradius.models import Worker, utcnow
from exradius.providers import default_provider, provider_count
from exradius.providers import provider_models
from exradius.runtimes import all_runtimes
from exradius.schemas import api as s
from exradius.schemas.messages import ROUTING_KEYS
from exradius.worker.heartbeat import WORKER_TIMEOUT_SECONDS, package_version

router = APIRouter(prefix="/api/system")

async def live_workers(session) -> list[Worker]:
    cutoff = utcnow() - timedelta(seconds=WORKER_TIMEOUT_SECONDS)
    stmt = select(Worker).where(Worker.last_seen_at >= cutoff).order_by(Worker.started_at)
    return list((await session.scalars(stmt)).all())


@router.get("/status", response_model=s.SystemStatusOut)
async def system_status(session: DB, state: State, _: Admin):
    try:
        await session.execute(text("SELECT 1"))
        database = s.CheckOut(ok=True, detail=session.bind.dialect.name)
    except Exception as exc:  # noqa: BLE001 - reported
        database = s.CheckOut(ok=False, detail=f"{type(exc).__name__}: {exc}")
    broker_ok, broker_detail = await state.broker.check()

    default = await default_provider(session)
    workers = await live_workers(session)
    seen = next((w for w in sorted(workers, key=lambda w: w.last_seen_at, reverse=True) if w.operator), None)
    return s.SystemStatusOut(
        database=database,
        broker=s.CheckOut(ok=broker_ok, detail=broker_detail),
        workers=[s.WorkerOut.model_validate(w) for w in workers],
        operator=s.OperatorOut.model_validate(seen.operator) if seen else None,
        provider=s.ProviderStatusOut(
            configured=default is not None,
            default=s.DefaultProviderOut.model_validate(default) if default else None,
            count=await provider_count(session),
        ),
        public_api_url=state.settings.public_api_url,
        version=package_version(),
    )


def runtime_out(runtime) -> s.RuntimeOut:
    return s.RuntimeOut(
        name=runtime.name,
        label=runtime.label,
        providers=[
            s.ProviderKindOut(
                kind=kind.kind,
                label=kind.label,
                auth_methods=[
                    s.AuthMethodOut(
                        name=m.name,
                        label=m.label,
                        credential_label=m.credential_label,
                        settings=[s.SettingFieldOut(name=f.name, label=f.label, required=f.required) for f in m.settings],
                        notice=m.notice,
                    )
                    for m in kind.auth_methods
                ],
                models=provider_models(kind.kind),
            )
            for kind in runtime.capabilities.providers
        ],
    )


@router.get("/runtimes", response_model=list[s.RuntimeOut])
async def system_runtimes(_: Admin):
    """Each runtime with the provider kinds and auth methods it accepts (providers.md §2.1)."""
    return [runtime_out(runtime) for runtime in all_runtimes()]


@router.get("/integration", response_model=s.IntegrationOut)
async def system_integration(state: State, _: Admin):
    settings = state.settings
    url = urlsplit(settings.effective_broker_url)
    vhost = unquote(url.path[1:]) if len(url.path) > 1 else "/"
    return s.IntegrationOut(
        broker=s.BrokerInfoOut(
            host=settings.public_broker_host or url.hostname or "localhost",
            port=settings.public_broker_port or url.port or 5672,
            vhost=vhost,
            username=unquote(url.username) if url.username else None,
            password_set=bool(url.password),
        ),
        tasks_queue=messaging.TASKS_QUEUE,
        answers_queue=messaging.ANSWERS_QUEUE,
        events_exchange=messaging.EVENTS_EXCHANGE,
        routing_keys=list(ROUTING_KEYS.values()),
        public_api_url=settings.public_api_url,
    )
