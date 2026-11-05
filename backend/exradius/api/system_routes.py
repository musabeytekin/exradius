"""System information for the overview, integration and settings pages (onboarding §4)."""

from __future__ import annotations

from datetime import timedelta
from urllib.parse import unquote, urlsplit

from sqlalchemy import select, text

from fastapi import APIRouter

from exradius import messaging
from exradius.api.deps import DB, Admin, State
from exradius.models import Secret, Worker, utcnow
from exradius.runtimes import RUNTIME_LABELS
from exradius.schemas import api as s
from exradius.schemas.messages import ROUTING_KEYS
from exradius.worker.heartbeat import WORKER_TIMEOUT_SECONDS, package_version

router = APIRouter(prefix="/api/system")

PROBE_KEY = "system/probe"


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

    secret = await session.scalar(
        select(Secret.id).where(Secret.project_id.is_(None), Secret.key == "ANTHROPIC_API_KEY")
    )
    source = "secret" if secret else ("env" if state.settings.anthropic_api_key else None)

    try:
        await state.store.put(PROBE_KEY, b"ok", "text/plain")
        store_ok = await state.store.get(PROBE_KEY) == b"ok"
        store_detail = None if store_ok else "read back a different value"
    except Exception as exc:  # noqa: BLE001 - reported
        store_ok, store_detail = False, f"{type(exc).__name__}: {exc}"

    return s.SystemStatusOut(
        database=database,
        broker=s.CheckOut(ok=broker_ok, detail=broker_detail),
        workers=[s.WorkerOut.model_validate(w) for w in await live_workers(session)],
        anthropic_key=s.AnthropicKeyOut(configured=source is not None, source=source),
        object_store=s.ObjectStoreOut(kind=state.settings.object_store, ok=store_ok, detail=store_detail),
        public_api_url=state.settings.public_api_url,
        version=package_version(),
    )


@router.get("/images", response_model=list[s.ImageOut])
async def system_images(session: DB, _: Admin):
    """Runner images (label ``exradius.runner=true``) reported by live workers."""
    images: dict[str, dict] = {}
    for worker in await live_workers(session):
        for image in worker.images or []:
            images.setdefault(image["tag"], image)
    return [s.ImageOut.model_validate(images[tag]) for tag in sorted(images)]


@router.get("/runtimes", response_model=list[s.RuntimeOut])
async def system_runtimes(_: Admin):
    return [s.RuntimeOut(name=name.value, label=label) for name, label in RUNTIME_LABELS.items()]


@router.get("/integration", response_model=s.IntegrationOut)
async def system_integration(state: State, _: Admin):
    settings = state.settings
    url = urlsplit(settings.broker_url)
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
