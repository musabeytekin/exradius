"""Sign-in and sessions (design §3)."""

from __future__ import annotations

import time
from datetime import timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from sqlalchemy import update

from exradius_desk.auth import groups_of, claim, safe_next
from exradius_desk.models import Login, Session_, utcnow
from tests.conftest import ISSUER, PUBLIC


def error_of(response: httpx.Response) -> str:
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/signin?")
    return parse_qs(urlsplit(location).query)["error"][0]


@pytest.fixture
def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC, headers={"Origin": PUBLIC})


async def start(client) -> str:
    response = await client.get("/auth/login", params={"next": "/tasks/plane-1"})
    assert response.status_code == 303
    return response.headers["location"]


async def test_the_code_flow_with_pkce(client, idp):
    assert (await client.get("/api/me")).status_code == 401
    url = await start(client)
    query = parse_qs(urlsplit(url).query)
    assert url.startswith(f"{ISSUER}/auth?")
    assert query["redirect_uri"] == [f"{PUBLIC}/auth/callback"] and query["scope"] == ["openid profile email"]
    assert query["client_id"] == ["exradius-desk"] and len(query["code_challenge"][0]) == 43

    back = await client.get("/auth/callback", params=idp.authorize(url, {
        "sub": "u-ada", "name": "Ada Lovelace", "email": "ada@acme.test", "groups": ["team-a", "ops"]}))
    assert back.status_code == 303 and back.headers["location"] == "/tasks/plane-1"
    cookie = back.headers["set-cookie"]
    assert "desk_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    me = (await client.get("/api/me")).json()
    assert me["name"] == "Ada Lovelace" and me["groups"] == ["ops", "team-a"] and me["exradius"] == "Exradius"
    [request] = idp.token_requests
    assert request["form"]["grant_type"] == "authorization_code" and "client_id" not in request["form"]

    assert (await client.post("/auth/logout")).status_code == 204
    assert (await client.get("/api/me")).status_code == 401


async def test_a_state_is_used_once(client, idp):
    url = await start(client)
    params = idp.authorize(url, {"sub": "u-ada"})
    assert (await client.get("/auth/callback", params=params)).status_code == 303
    again = await client.get("/auth/callback", params={**params, "code": "other"})
    assert "unknown or has expired" in error_of(again)
    assert "unknown or has expired" in error_of(await client.get("/auth/callback", params={"code": "x", "state": "nope"}))


async def test_an_old_login_has_expired(client, idp, sessions):
    url = await start(client)
    params = idp.authorize(url, {"sub": "u-ada"})
    async with sessions() as session:
        await session.execute(update(Login).values(created_at=utcnow() - timedelta(minutes=11)))
        await session.commit()
    assert "expired" in error_of(await client.get("/auth/callback", params=params))


async def test_the_provider_answers_an_error(client, idp):
    url = await start(client)
    state = parse_qs(urlsplit(url).query)["state"][0]
    response = await client.get("/auth/callback", params={"state": state, "error": "access_denied",
                                                          "error_description": "not allowed"})
    assert error_of(response) == "the identity provider answered access_denied: not allowed"


@pytest.mark.parametrize("claims, reason", [
    ({"nonce": "wrong"}, "nonce does not match"),
    ({"iss": "https://evil.test"}, "Invalid issuer"),
    ({"aud": "someone-else"}, "Audience doesn't match"),
    ({"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}, "Signature has expired"),
])
async def test_id_token_checks(client, idp, claims, reason):
    url = await start(client)
    response = await client.get("/auth/callback", params=idp.authorize(url, {"sub": "u-ada"}, token_claims=claims))
    assert reason in error_of(response)
    assert (await client.get("/api/me")).status_code == 401


async def test_unsigned_and_foreign_keys_are_refused(client, idp):
    import jwt as pyjwt

    url = await start(client)
    nonce = parse_qs(urlsplit(url).query)["nonce"][0]
    unsigned = pyjwt.encode({"sub": "x", "iss": ISSUER, "aud": "exradius-desk", "nonce": nonce,
                             "iat": int(time.time()), "exp": int(time.time()) + 60}, key=None, algorithm="none")
    response = await client.get("/auth/callback", params=idp.authorize(url, {"sub": "x"},
                                                                       token_claims={"__raw_id_token": unsigned}))
    assert "Desk does not accept" in error_of(response)

    url = await start(client)
    params = idp.authorize(url, {"sub": "x"})
    idp.kid = "k2"  # tokens now name a key the JWKS does not list
    from cryptography.hazmat.primitives.asymmetric import rsa

    old_jwks = idp.jwks
    idp.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    idp.jwks = lambda: {"keys": [{**k, "kid": "k1"} for k in old_jwks()["keys"]]}
    assert "does not list" in error_of(await client.get("/auth/callback", params=params))


async def test_groups_from_a_nested_claim_and_from_userinfo(app, idp, signin, client):
    signin.oidc.settings = signin.oidc.settings.__class__(**{**signin.oidc.settings.__dict__,
                                                            "groups_claim": "realm_access.roles"})
    url = await start(client)
    await client.get("/auth/callback", params=idp.authorize(url, {"sub": "u-ada", "realm_access": {"roles": "team-a"}}))
    assert (await client.get("/api/me")).json()["groups"] == ["team-a"]

    other = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=PUBLIC, headers={"Origin": PUBLIC})
    url = await start(other)
    params = idp.authorize(url, {"sub": "u-ben"})
    idp.userinfo_claims = {"realm_access": {"roles": ["team-b", 7]}}
    await other.get("/auth/callback", params=params)
    assert (await other.get("/api/me")).json()["groups"] == ["team-b"]


async def test_sessions_expire(client, idp, sessions):
    url = await start(client)
    await client.get("/auth/callback", params=idp.authorize(url, {"sub": "u-ada"}))
    assert (await client.get("/api/me")).status_code == 200
    async with sessions() as session:
        await session.execute(update(Session_).values(expires_at=utcnow() - timedelta(seconds=1)))
        await session.commit()
    assert (await client.get("/api/me")).status_code == 401


async def test_a_provider_that_cannot_be_reached(client, idp, desk, exradius):
    idp.down = True
    response = await client.get("/auth/login")
    assert "the identity provider at https://idp.test/realms/acme cannot be reached" in error_of(response)
    await desk.status.report()
    assert any("cannot be reached" in p["message"] for p in exradius.reports[-1]["problems"])
    idp.down = False
    idp.discovery_overrides = {"code_challenge_methods_supported": ["plain"]}
    response = await client.get("/auth/login")
    assert error_of(response).endswith("is not usable: it does not support PKCE with S256")


def test_helpers():
    assert safe_next("/tasks/plane-1?x=1") == "/tasks/plane-1?x=1"
    for bad in (None, "", "https://evil.test", "//evil.test", "/\\evil.test", "tasks"):
        assert safe_next(bad) == "/"
    assert claim({"a": {"b": ["x"]}}, "a.b") == ["x"] and claim({"a": 1}, "a.b") is None
    assert groups_of("x") == ["x"] and groups_of(["x", 1]) == ["x"] and groups_of(None) is None


async def test_cross_site_writes_are_refused(browser):
    ada = await browser("ada")
    response = await ada.post("/api/tasks", data={"project": "plane", "title": "x"},
                              headers={"Origin": "https://evil.test"})
    assert response.status_code == 403
    assert (await ada.post("/api/tasks", data={"project": "plane", "title": "x"})).status_code == 201
