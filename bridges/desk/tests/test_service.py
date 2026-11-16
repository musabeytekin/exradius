"""Settings (design §12), the status at startup (§11), the frontend's files, and a round trip
through a real RabbitMQ when ``DESK_TEST_BROKER_URL`` is set."""

from __future__ import annotations

import os

import httpx
import pytest
from exradius_bridge_kit import SettingsError

from exradius_desk.app import Web, make_app
from exradius_desk.settings import Settings, broker_url, normalise_database_url
from tests.conftest import PUBLIC

ENV = {
    "DESK_DATABASE_URL": "postgresql://desk:pw@exradius-desk-postgres-rw:5432/desk",
    "DESK_PUBLIC_URL": "https://desk.acme.test/",
    "DESK_OIDC_ISSUER": "https://idp.acme.test/realms/acme",
    "DESK_OIDC_CLIENT_ID": "exradius-desk",
    "EXRADIUS_BROKER_URL": "amqp://rabbitmq:5672/",
    "EXRADIUS_API_URL": "http://exradius-api:8000",
    "EXRADIUS_INTEGRATION_TOKEN": "exr_x",
}


def test_settings():
    settings = Settings.from_env({**ENV, "EXRADIUS_BROKER_USERNAME": "desk", "EXRADIUS_BROKER_PASSWORD": "p@ss/w:rd"})
    assert settings.database_url == "postgresql+asyncpg://desk:pw@exradius-desk-postgres-rw:5432/desk"
    assert settings.public_url == "https://desk.acme.test" and settings.origin == "https://desk.acme.test"
    assert settings.redirect_uri == "https://desk.acme.test/auth/callback" and settings.secure_cookies
    assert settings.oidc.discovery_url == "https://idp.acme.test/realms/acme/.well-known/openid-configuration"
    assert settings.oidc.client_secret is None and settings.oidc.groups_claim == "groups"
    assert settings.smtp is None and settings.session_hours == 12 and settings.port == 8080
    assert settings.kit.broker_url == "amqp://desk:p%40ss%2Fw%3Ard@rabbitmq:5672/"


def test_every_problem_at_once():
    with pytest.raises(SettingsError) as exc:
        Settings.from_env({"DESK_PUBLIC_URL": "desk.acme.test", "DESK_SMTP_HOST": "smtp", "DESK_SMTP_TLS": "ssl",
                           "DESK_SESSION_HOURS": "0", "PORT": "70000", "DESK_OIDC_SCOPES": "profile"})
    problems = str(exc.value).split("; ")
    for expected in ("DESK_DATABASE_URL is required", "DESK_PUBLIC_URL must start with https:// or http://",
                     "DESK_OIDC_ISSUER is required", "DESK_OIDC_CLIENT_ID is required",
                     "DESK_OIDC_SCOPES must include openid", "DESK_SMTP_TLS must be one of starttls, tls, none, not 'ssl'",
                     "DESK_SMTP_FROM is required with DESK_SMTP_HOST",
                     "DESK_SESSION_HOURS must be a positive number, not '0'",
                     "PORT must be a port number, not '70000'", "EXRADIUS_BROKER_URL is required",
                     "EXRADIUS_INTEGRATION_TOKEN is required"):
        assert expected in problems


def test_urls():
    assert normalise_database_url("postgres://a@b/c") == "postgresql+asyncpg://a@b/c"
    assert normalise_database_url("sqlite+aiosqlite:///x") == "sqlite+aiosqlite:///x"
    assert broker_url("amqp://h/vh", None, None) == "amqp://h/vh"
    assert broker_url("amqp://old:x@h:5673/", "u", None) == "amqp://u@h:5673/"


async def test_the_status_at_startup(desk, exradius):
    [report] = exradius.reports
    assert report["software"] == "exradius-desk test" and report["problems"] == []
    assert len(desk.install) == 4


async def test_the_frontend_is_served_with_its_routes(desk, signin, tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<div id=root></div>")
    (tmp_path / "assets" / "app-1.js").write_text("console.log(1)")
    app = make_app(desk, signin, Web(origin=PUBLIC, secure_cookies=False, session_hours=1, static=tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC) as client:
        page = await client.get("/tasks/plane-1")
        assert page.text == "<div id=root></div>" and page.headers["cache-control"] == "no-cache"
        asset = await client.get("/assets/app-1.js")
        assert "immutable" in asset.headers["cache-control"]
        assert (await client.get("/../secrets")).text == "<div id=root></div>"
        assert (await client.get("/api/nothing")).status_code == 404
        assert (await client.get("/healthz")).json() == {"ok": True}


@pytest.mark.skipif(not os.environ.get("DESK_TEST_BROKER_URL"), reason="needs DESK_TEST_BROKER_URL")
async def test_a_real_broker_takes_a_task_and_a_cancel():
    import asyncio
    import json
    import uuid

    import aio_pika
    from exradius_bridge_kit import Broker

    url = os.environ["DESK_TEST_BROKER_URL"]
    broker = Broker(url, f"desk-test-{uuid.uuid4().hex[:6]}")
    await broker.connect()
    got = []

    async def handler(event):
        got.append(event)

    await broker.consume(handler)
    connection = await aio_pika.connect_robust(url)
    channel = await connection.channel()
    exchange = await channel.declare_exchange("exradius.events", aio_pika.ExchangeType.TOPIC, durable=True)
    await exchange.publish(aio_pika.Message(json.dumps({"type": "status", "task_id": "x"}).encode()),
                           routing_key="task.status")
    for _ in range(100):
        if got:
            break
        await asyncio.sleep(0.05)
    assert got == [{"type": "status", "task_id": "x"}]
    # Mandatory publishing: both queues exist (declared on connect), so neither raises.
    await broker.publish_cancel({"version": 1, "task_id": f"never-{uuid.uuid4().hex[:6]}"})
    events = await channel.declare_queue(broker.queue_name, durable=True)
    await events.delete(if_unused=False, if_empty=False)
    await connection.close()
    await broker.close()
