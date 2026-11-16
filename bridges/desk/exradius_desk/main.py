"""``exradius-desk`` (serve, the default) and ``exradius-desk migrate`` (design §13)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from importlib.metadata import version

import uvicorn
from exradius_bridge_kit import Broker, ExradiusError, IntegrationApi, SettingsError, software_name

from exradius_desk.app import Web, make_app
from exradius_desk.auth import OidcClient, ProviderProblem, SignIn
from exradius_desk.db import migrate
from exradius_desk.desk import Desk
from exradius_desk.mail import Mailer
from exradius_desk.models import make_engine, make_sessions
from exradius_desk.settings import Settings, normalise_database_url

log = logging.getLogger("exradius_desk")


async def serve(settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    sessions = make_sessions(engine)
    oidc = OidcClient(settings.oidc, settings.redirect_uri)
    desk = Desk(
        sessions=sessions,
        broker=Broker(settings.kit.broker_url, "desk"),
        api=IntegrationApi(settings.kit.api_url, settings.kit.integration_token),
        public_url=settings.public_url,
        max_attachment_bytes=settings.kit.max_attachment_bytes,
        software=software_name(f"exradius-desk {version('exradius-desk')}"),
        mailer=Mailer(settings.smtp),
        api_url=settings.kit.api_url,
        provider_problem=lambda: oidc.problem,
    )
    try:
        try:
            await oidc.discover()
        except ProviderProblem as exc:
            log.warning("%s; sign-in waits for it", exc)  # reported in the status, Desk starts anyway
        await desk.start()
        await desk.listen()
        signin = SignIn(sessions, oidc, session_hours=settings.session_hours)
        app = make_app(desk, signin, Web(origin=settings.origin, secure_cookies=settings.secure_cookies,
                                         session_hours=settings.session_hours))
        log.info("Desk is at %s (install %s); listening on port %d", settings.public_url, desk.install, settings.port)
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=settings.port, lifespan="off",
                                               log_level="info", proxy_headers=True))
        await server.serve()
    finally:
        await desk.close()
        await oidc.aclose()
        await engine.dispose()


def _fail(problem: str) -> None:
    print(f"exradius-desk cannot start: {problem}", file=sys.stderr)
    sys.exit(1)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("aio_pika", "aiormq", "httpx"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    command = sys.argv[1] if len(sys.argv) > 1 else "serve"
    if command == "migrate":
        url = normalise_database_url(os.environ.get("DESK_DATABASE_URL", "").strip())
        if not url:
            _fail("DESK_DATABASE_URL is required")
        asyncio.run(migrate(url))
        print("Desk's database is at the latest migration", file=sys.stderr)
        return
    if command != "serve":
        _fail(f"unknown command {command!r} (serve, migrate)")
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        _fail(str(exc))
        return
    try:
        asyncio.run(serve(settings))
    except ExradiusError as exc:
        _fail(str(exc))
    except OSError as exc:  # the broker
        _fail(f"EXRADIUS_BROKER_URL: cannot connect ({exc})")
    except KeyboardInterrupt:
        pass
