"""FastAPI application: admin API (session cookie) and internal run API (run token)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from starlette.middleware.sessions import SessionMiddleware

from exradius import messaging
from exradius.api import config_routes, hooks, integration_routes, internal, provider_routes, system_routes, task_routes
from exradius.api.deps import DB, Admin, AppState
from exradius.config import Settings, get_settings
from exradius.crypto import SecretBox, hash_password, verify_password
from exradius.db import SessionFactory, make_engine, make_session_factory
from exradius.models import AdminUser
from exradius.providers import bootstrap_provider
from exradius.schemas.api import AdminOut, LoginIn, PasswordChange
from exradius.schemas.messages import DirectoryEvent
from exradius.storage import ObjectStore, make_store

log = logging.getLogger(__name__)


class BrokerPublisher:
    """The API's one broker connection: publishes tasks and answers exactly as an
    integration would, admin actions (cancel, verify) to the workers, and the directory
    notice on the events exchange."""

    def __init__(self, broker_url: str):
        self._url = broker_url
        self._connection: Any = None
        self._channel: Any = None
        self._exchange: Any = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> Any:
        async with self._lock:
            if self._channel is None or self._channel.is_closed:
                self._connection = await messaging.connect(self._url)
                self._channel = await self._connection.channel()
                self._exchange = await messaging.declare_topology(self._channel)
            return self._channel

    async def publish(self, queue: str, body: dict[str, Any]) -> None:
        await messaging.publish_to_queue(await self._ensure(), queue, body)

    async def publish_directory(self, event: DirectoryEvent) -> None:
        await self._ensure()
        await messaging.publish_directory(self._exchange, event)

    async def cancel(self, run_id: str) -> None:
        await self.publish(messaging.CONTROL_QUEUE, {"version": 1, "action": "cancel", "run_id": run_id})

    async def verify(self, verification_id: str) -> None:
        await self.publish(
            messaging.CONTROL_QUEUE, {"version": 1, "action": "verify", "verification_id": verification_id}
        )

    async def test_provider(self, test_id: str) -> None:
        await self.publish(messaging.CONTROL_QUEUE, {"version": 1, "action": "test_provider", "test_id": test_id})

    async def check(self) -> tuple[bool, str]:
        try:
            async with asyncio.timeout(5):
                await self._ensure()
        except Exception as exc:  # noqa: BLE001 - reported on the status page
            return False, f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
        return True, "connected"

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()


async def ensure_admin(sessions: SessionFactory, settings: Settings) -> None:
    """Seed the admin user from ``EXRADIUS_ADMIN_PASSWORD`` when none exists. After that the
    password is changed in the panel (``POST /api/auth/password``), never from the
    environment."""
    if not settings.admin_password:
        return
    async with sessions() as session:
        if await session.scalar(select(AdminUser.id).limit(1)) is not None:
            return
        session.add(
            AdminUser(username=settings.admin_username, password_hash=hash_password(settings.admin_password))
        )
        try:
            await session.commit()
        except IntegrityError:  # another API process seeded it first
            await session.rollback()
            return
        log.info("created admin user %r", settings.admin_username)


auth = APIRouter(prefix="/api/auth")


@auth.post("/login", response_model=AdminOut)
async def login(body: LoginIn, request: Request, session: DB):
    admin = await session.scalar(select(AdminUser).where(AdminUser.username == body.username))
    if admin is None or not verify_password(body.password, admin.password_hash):
        raise HTTPException(401, "invalid username or password")
    request.session.clear()
    request.session["admin_id"] = str(admin.id)
    return admin


@auth.post("/logout", status_code=204)
async def logout(request: Request):
    request.session.clear()


@auth.get("/me", response_model=AdminOut)
async def me(admin: Admin):
    return admin


@auth.post("/password", status_code=204)
async def change_password(body: PasswordChange, admin: Admin, session: DB):
    if not verify_password(body.current_password, admin.password_hash):
        raise HTTPException(400, "the current password is not correct")
    admin.password_hash = hash_password(body.new_password)
    await session.commit()
    return Response(status_code=204)


def create_app(
    settings: Settings | None = None,
    store: ObjectStore | None = None,
    broker: Any = None,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        sessions = make_session_factory(engine)
        object_store = store or make_store(settings)
        await object_store.ensure_bucket()
        publisher = broker or BrokerPublisher(settings.broker_url)
        app.state.exradius = AppState(
            settings=settings,
            sessions=sessions,
            store=object_store,
            box=SecretBox(settings.secret_key),
            broker=publisher,
        )
        app.state.exradius.rereads = hooks.make_rereads(app.state.exradius)
        await ensure_admin(sessions, settings)
        await bootstrap_provider(sessions, settings, app.state.exradius.box)
        # Once at startup, for changes no running process published (integration-api.md §3);
        # in the background, so a broker that is slow to answer does not hold up the API.
        startup_check = asyncio.create_task(config_routes.check_directory(app.state.exradius))
        app.state.directory_startup_check = startup_check
        try:
            yield
        finally:
            startup_check.cancel()
            await asyncio.gather(startup_check, return_exceptions=True)
            await app.state.exradius.rereads.close()
            if hasattr(publisher, "close"):
                await publisher.close()
            await engine.dispose()

    app = FastAPI(title="Exradius", version="1", lifespan=lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.effective_session_secret,
        session_cookie="exradius_session",
        https_only=settings.cookie_secure,
        same_site="lax",
    )

    @app.exception_handler(IntegrityError)
    async def _conflict(request: Request, exc: IntegrityError):
        return JSONResponse({"detail": "conflict with existing data (duplicate or in use)"}, 409)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    app.include_router(auth)
    app.include_router(config_routes.router)
    app.include_router(provider_routes.router)
    app.include_router(task_routes.router)
    app.include_router(system_routes.router)
    app.include_router(hooks.router)
    app.include_router(integration_routes.router)
    app.include_router(integration_routes.admin_router)
    app.include_router(internal.router)
    return app
