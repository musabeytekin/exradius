"""Startup: the kit checks GitHub and Exradius and catches up, then webhooks are served."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from datetime import timedelta
from importlib.metadata import version

import uvicorn
from exradius_bridge_kit import ExradiusError, Kit, SettingsError, TrackerProblem

from exradius_github.github import GitHub
from exradius_github.settings import Settings
from exradius_github.store import Store
from exradius_github.tracker import GitHubTracker
from exradius_github.webhooks import Webhooks, make_app

log = logging.getLogger("exradius_github")


PURGE_EVERY = 3600.0


async def purge_forever(store: Store) -> None:
    """Expired rows go once an hour (design §10)."""
    while True:
        await asyncio.sleep(PURGE_EVERY)
        try:
            gone = await store.purge()
            if gone:
                log.info("forgot %d expired rows", gone)
        except Exception:  # noqa: BLE001 - tried again in an hour
            log.exception("purging the database failed")


async def serve(settings: Settings) -> None:
    store = Store(settings.db_path, timedelta(days=settings.db_ttl_days))
    await store.open()
    log.info("database %s: %s, %d expired rows forgotten", settings.db_path, await store.stats(), await store.purge())
    github = GitHub(settings.github_api_url, settings.github_token)
    tracker = GitHubTracker(github, settings.github_url, artifacts_branch=settings.artifacts_branch, store=store)
    kit = Kit(tracker, settings.kit, software=f"exradius-github {version('exradius-github')}")
    purging = asyncio.create_task(purge_forever(store))
    try:
        await kit.start()
        await kit.listen()
        log.info("acting as %s; listening for GitHub on port %d", tracker.bot.id if tracker.bot else "?", settings.port)
        app = make_app(Webhooks(settings.github_webhook_secret, kit, tracker))
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=settings.port, lifespan="off",
                                               log_level="info"))
        await server.serve()
    finally:
        purging.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await purging
        await kit.close()
        await github.aclose()
        await store.close()


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
