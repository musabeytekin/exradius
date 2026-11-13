"""Desk against fakes (design §15): the kit's FakeBroker and FakeExradius, a fake OIDC
issuer, and a small SMTP server on a local port."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from exradius_bridge_kit.exradius import IntegrationApi
from exradius_bridge_kit.testing import SETTINGS, FakeBroker, FakeExradius, project_entry

from exradius_desk.app import Web, make_app
from exradius_desk.auth import OidcClient, SignIn
from exradius_desk.db import migrate
from exradius_desk.desk import Desk
from exradius_desk.mail import Mailer
from exradius_desk.models import make_engine, make_sessions
from exradius_desk.settings import Oidc, Smtp

PUBLIC = "http://desk.test"
ISSUER = "https://idp.test/realms/acme"
CLIENT_ID = "exradius-desk"
CLIENT_SECRET = "s3cret"


class FakeIdp:
    """Discovery, JWKS, the token endpoint and userinfo, signing ID tokens with its own key."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "k1"
        self.codes: dict[str, dict[str, Any]] = {}
        self.userinfo: dict[str, dict[str, Any]] = {}
        self.userinfo_claims: dict[str, Any] = {}
        """Added to every userinfo answer."""
        self.discovery_overrides: dict[str, Any] = {}
        self.token_requests: list[dict[str, Any]] = []
        self.down = False
        self.issuer = ISSUER

    def discovery(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{ISSUER}/auth",
            "token_endpoint": f"{ISSUER}/token",
            "jwks_uri": f"{ISSUER}/certs",
            "userinfo_endpoint": f"{ISSUER}/userinfo",
            "code_challenge_methods_supported": ["plain", "S256"],
            **self.discovery_overrides,
        }

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        return {"keys": [{**jwk, "kid": self.kid, "use": "sig", "alg": "RS256"}]}

    def id_token(self, claims: dict[str, Any], **header: Any) -> str:
        now = int(time.time())
        body = {"iss": ISSUER, "aud": CLIENT_ID, "iat": now, "exp": now + 300, **claims}
        return jwt.encode(body, self.key, algorithm="RS256", headers={"kid": self.kid, **header})

    def authorize(self, url: str, claims: dict[str, Any], *, token_claims: dict[str, Any] | None = None) -> dict:
        """What the browser brings back to the callback after signing in as ``claims``."""
        query = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert query["response_type"] == "code" and query["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(8)
        self.codes[code] = {"claims": {"nonce": query["nonce"], **claims, **(token_claims or {})},
                            "challenge": query["code_challenge"], "redirect_uri": query["redirect_uri"]}
        return {"code": code, "state": query["state"]}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("idp down")
        path = request.url.path.removeprefix(urlsplit(ISSUER).path)
        if path == "/.well-known/openid-configuration":
            return httpx.Response(200, json=self.discovery())
        if path == "/certs":
            return httpx.Response(200, json=self.jwks())
        if path == "/token":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            self.token_requests.append({"form": form, "auth": request.headers.get("authorization")})
            expected = "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
            if request.headers.get("authorization") != expected:
                return httpx.Response(401, json={"error": "invalid_client"})
            grant = self.codes.pop(form.get("code", ""), None)
            if grant is None:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "unknown code"})
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"].encode()).digest())
            if challenge.rstrip(b"=").decode() != grant["challenge"] or form["redirect_uri"] != grant["redirect_uri"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE failed"})
            token = grant["claims"].pop("__raw_id_token", None) or self.id_token(grant["claims"])
            access = secrets.token_urlsafe(8)
            self.userinfo[access] = {"sub": grant["claims"]["sub"]}
            return httpx.Response(200, json={"id_token": token, "access_token": access, "token_type": "Bearer"})
        if path == "/userinfo":
            access = request.headers.get("authorization", "").removeprefix("Bearer ")
            known = self.userinfo.get(access)
            return httpx.Response(200, json={**known, **self.userinfo_claims} if known else {})
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@dataclass
class SmtpServer:
    """Enough SMTP for smtplib: every message received, as (recipients, raw text)."""

    port: int = 0
    received: list[tuple[list[str], str]] = field(default_factory=list)
    server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self.server is not None
        self.server.close()
        await self.server.wait_closed()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        def say(line: str) -> None:
            writer.write((line + "\r\n").encode())

        say("220 smtp.test ready")
        recipients: list[str] = []
        while line := (await reader.readline()).decode().rstrip("\r\n"):
            verb = line.split(" ", 1)[0].upper()
            if verb in ("EHLO", "HELO"):
                say("250 smtp.test")
            elif verb == "MAIL":
                recipients = []
                say("250 ok")
            elif verb == "RCPT":
                recipients.append(line.split(":", 1)[1].strip(" <>"))
                say("250 ok")
            elif verb == "DATA":
                say("354 go on")
                await writer.drain()
                data = []
                while (chunk := (await reader.readline()).decode()) != ".\r\n":
                    data.append(chunk)
                self.received.append((recipients, "".join(data)))
                say("250 queued")
            elif verb == "QUIT":
                say("221 bye")
                await writer.drain()
                break
            else:
                say("250 ok")
            await writer.drain()
        writer.close()

    def to(self, address: str) -> list[str]:
        return [text for rcpts, text in self.received if address in rcpts]

    def subjects(self, address: str) -> list[str]:
        return [next(line[9:] for line in text.splitlines() if line.startswith("Subject: "))
                for text in self.to(address)]


def entry(slug: str, groups: list[str] | None = None, **kwargs: Any) -> dict[str, Any]:
    return project_entry(slug, bridge_config={"groups": groups or ["team-a"]}, **kwargs)


@pytest.fixture
async def sessions(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path}/desk.sqlite"
    await migrate(url)
    engine = make_engine(url)
    yield make_sessions(engine)
    await engine.dispose()


@pytest.fixture
def idp():
    return FakeIdp()


@pytest.fixture
def exradius():
    return FakeExradius(bridge="desk", projects=[
        entry("plane", ["team-a"], name="Plane"),
        entry("bank", ["team-b"], name="Bank"),
    ])


@pytest.fixture
def broker():
    return FakeBroker()


@pytest.fixture
async def smtp():
    server = SmtpServer()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def mail_settings(smtp):
    return Smtp(host="127.0.0.1", port=smtp.port, tls="none", username=None, password=None,
                sender="Exradius Desk <desk@acme.test>")


@pytest.fixture
async def oidc(idp):
    client = OidcClient(
        Oidc(issuer=ISSUER, discovery_url=f"{ISSUER}/.well-known/openid-configuration", client_id=CLIENT_ID,
             client_secret=CLIENT_SECRET, scopes="openid profile email", groups_claim="groups"),
        f"{PUBLIC}/auth/callback",
        http=httpx.AsyncClient(transport=idp.transport()),
    )
    yield client
    await client.aclose()


@pytest.fixture
async def desk(sessions, broker, exradius, oidc, mail_settings):
    transport = exradius.transport()
    desk = Desk(
        sessions=sessions, broker=broker,
        api=IntegrationApi(SETTINGS.api_url, SETTINGS.integration_token, transport=transport),
        public_url=PUBLIC, max_attachment_bytes=1024 * 1024, software="exradius-desk test",
        mailer=Mailer(mail_settings), downloads=httpx.AsyncClient(transport=transport),
        api_url="http://exradius-api.internal:8000", provider_problem=lambda: oidc.problem,
    )
    await desk.start()
    await desk.listen()
    yield desk
    await desk.close()


@pytest.fixture
def signin(sessions, oidc):
    return SignIn(sessions, oidc, session_hours=12)


@pytest.fixture
def app(desk, signin):
    return make_app(desk, signin, Web(origin=PUBLIC, secure_cookies=False, session_hours=12, static=None))


@pytest.fixture
def browser(app, idp, desk):
    """``await browser("ada", groups=[...])``: a signed-in client of its own."""
    clients: list[httpx.AsyncClient] = []

    async def open_as(sub: str, *, name: str | None = None, groups: list[str] | None = None,
                      email: str | None = None, extra: dict | None = None) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC,
                                   headers={"Origin": PUBLIC})
        clients.append(client)
        start = await client.get("/auth/login", params={"next": "/tasks"})
        assert start.status_code == 303, start.text
        claims = {"sub": sub, "name": name or sub.capitalize(), "email": email or f"{sub}@acme.test",
                  "groups": groups if groups is not None else ["team-a"], **(extra or {})}
        back = await client.get("/auth/callback", params=idp.authorize(start.headers["location"], claims))
        assert back.status_code == 303 and back.headers["location"] == "/tasks", back.headers.get("location")
        client.person = (await client.get("/api/me")).json()  # type: ignore[attr-defined]
        return client

    yield open_as
    for client in clients:
        asyncio.get_event_loop().create_task(client.aclose())


def event(desk: Desk, type_: str, task_id: str, payload: dict[str, Any], *, task: str | None = None,
          agent: str | None = None, install: str | None = None, bridge: str = "desk") -> dict[str, Any]:
    """A task event as Exradius publishes it (architecture §4.3), for a Desk task."""
    install = install or desk.install
    key = task or re.sub(rf"(-\d+)?-{install}$", "", task_id)
    return {
        "version": 1, "task_id": task_id, "run_id": "r-1", "project": key.rsplit("-", 1)[0], "agent": agent,
        "correlation": {"bridge": bridge, "instance": PUBLIC, "install": install, "task": key},
        "timestamp": "2026-11-14T10:00:00Z", "type": type_, "payload": payload,
    }
