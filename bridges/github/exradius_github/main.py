"""Startup: the kit checks GitHub and Exradius and catches up, then webhooks are served."""

from __future__ import annotations

import asyncio
import logging
import sys
from importlib.metadata import version

import uvicorn
from exradius_bridge_kit import ExradiusError, Kit, SettingsError, TrackerProblem

from exradius_github.github import GitHub
from exradius_github.settings import Settings
from exradius_github.tracker import GitHubTracker
from exradius_github.webhooks import Webhooks, make_app

log = logging.getLogger("exradius_github")


async def serve(settings: Settings) -> None:
    github = GitHub(settings.github_api_url, settings.github_token)
    tracker = GitHubTracker(github, settings.github_url)
    kit = Kit(tracker, settings.kit, software=f"exradius-github {version('exradius-github')}")
    try:
        await kit.start()
        await kit.listen()
        log.info("acting as %s; listening for GitHub on port %d", tracker.bot.id if tracker.bot else "?", settings.port)
        app = make_app(Webhooks(settings.github_webhook_secret, kit, tracker))
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=settings.port, lifespan="off",
                                               log_level="info"))
        await server.serve()
    finally:
        await kit.close()
        await github.aclose()


def _fail(problem: str) -> None:
    print(f"exradius-github cannot start: {problem}", file=sys.stderr)
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
