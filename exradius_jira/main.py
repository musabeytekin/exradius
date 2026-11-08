"""Startup: the kit checks Jira and Exradius and catches up, then webhooks are served."""

from __future__ import annotations

import asyncio
import logging
import sys
from importlib.metadata import version

import uvicorn
from exradius_bridge_kit import ExradiusError, Kit, SettingsError, TrackerProblem

from exradius_jira.jira import Jira
from exradius_jira.settings import Settings
from exradius_jira.tracker import JiraTracker
from exradius_jira.webhooks import Webhooks, make_app

log = logging.getLogger("exradius_jira")


async def serve(settings: Settings) -> None:
    jira = Jira(settings.jira_site, settings.jira_bot_email, settings.jira_bot_token)
    tracker = JiraTracker(jira)
    kit = Kit(tracker, settings.kit, software=f"exradius-jira {version('exradius-jira')}")
    try:
        await kit.start()
        await kit.listen()
        log.info("acting as %s; listening for Jira on port %d", tracker.bot.name if tracker.bot else "?", settings.port)
        app = make_app(Webhooks(settings.jira_webhook_secret, kit, tracker))
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=settings.port, lifespan="off",
                                               log_level="info"))
        await server.serve()
    finally:
        await kit.close()
        await jira.aclose()


def _fail(problem: str) -> None:
    print(f"exradius-jira cannot start: {problem}", file=sys.stderr)
    sys.exit(1)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        _fail(str(exc))
        return
    try:
        asyncio.run(serve(settings))
    except (TrackerProblem, ExradiusError) as exc:
        _fail(str(exc))
    except OSError as exc:  # the broker
        _fail(f"EXRADIUS_BROKER_URL: cannot connect ({exc})")
    except KeyboardInterrupt:
        pass
