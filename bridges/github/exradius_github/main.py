"""Startup (design §6): check everything, sync the labels, reconcile, then serve."""

from __future__ import annotations

import asyncio
import logging
import sys

import jwt
import uvicorn

from exradius_github.exradius import Broker, DirectoryClient, ExradiusError
from exradius_github.flow import Bridge
from exradius_github.github import GitHubApp, GitHubError
from exradius_github.labels import Labels
from exradius_github.settings import Settings, SettingsError
from exradius_github.webhooks import Webhooks, make_app

log = logging.getLogger("exradius_github")


class StartupError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


async def start(settings: Settings, *, app: GitHubApp, directory_client: DirectoryClient, broker: Broker, **bridge_options) -> Bridge:
    """Check every dependency, then sync the labels, reconcile and start consuming."""
    problems: list[str] = []
    bot: str | None = None
    try:
        me = await app.app()
        bot = f"{me['slug']}[bot]"
    except GitHubError as exc:
        problems.append(f"GITHUB_APP_ID and the private key: GitHub refused them ({exc.status} {exc.detail})")
    except (jwt.PyJWTError, ValueError, TypeError) as exc:
        problems.append(f"the GitHub App private key cannot sign: {exc}")
    directory = None
    try:
        directory = await directory_client.fetch()
    except ExradiusError as exc:
        problems.append(f"EXRADIUS_API_URL and EXRADIUS_INTEGRATION_TOKEN: {exc}")
    try:
        await broker.connect()
    except Exception as exc:
        problems.append(f"EXRADIUS_BROKER_URL: cannot connect ({exc or type(exc).__name__})")
    if problems or bot is None or directory is None:
        raise StartupError(problems)

    labels = Labels(app, settings.github_host)
    bridge = Bridge(
        settings=settings, app=app, labels=labels, directory_client=directory_client, publisher=broker, bot=bot,
        **bridge_options,
    )
    bridge.directory = directory
    await labels.sync(directory)
    log.info("agent labels in sync for %d projects", len(directory.projects))
    await bridge.reconcile()
    await broker.consume(bridge.on_event)
    log.info("acting as %s", bot)
    return bridge


async def serve(settings: Settings) -> None:
    app = GitHubApp(settings.github_api_url, settings.github_app_id, settings.github_private_key)
    directory_client = DirectoryClient(settings.exradius_api_url, settings.exradius_integration_token)
    broker = Broker(settings.exradius_broker_url)
    try:
        bridge = await start(settings, app=app, directory_client=directory_client, broker=broker)
        log.info("listening for GitHub on port %d", settings.port)
        web = make_app(Webhooks(settings.github_webhook_secret, bridge))
        server = uvicorn.Server(uvicorn.Config(web, host="0.0.0.0", port=settings.port, lifespan="off", log_level="info"))
        await server.serve()
    finally:
        await broker.close()
        await directory_client.aclose()
        await app.aclose()


def _fail(problems: list[str]) -> None:
    print("exradius-github cannot start:", file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)
    sys.exit(1)


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        settings = Settings.from_env()
    except SettingsError as exc:
        _fail(exc.problems)
        return
    try:
        asyncio.run(serve(settings))
    except StartupError as exc:
        _fail(exc.problems)
    except KeyboardInterrupt:
        pass
