"""Sign-in with OpenID Connect and Desk's sessions (design §3): the authorization code flow
with PKCE against one issuer, no local accounts."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from sqlalchemy import delete, select

from exradius_desk.models import Login, Person, Session_, SessionFactory, utcnow
from exradius_desk.settings import Oidc as OidcSettings

log = logging.getLogger(__name__)

ALGORITHMS = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
DISCOVERY_FOR = 3600.0
LOGIN_FOR = timedelta(minutes=10)
LEEWAY = 60
COOKIE = "desk_session"


class SignInFailed(Exception):
    """Shown on the sign-in page as the reason."""


class ProviderProblem(Exception):
    """The identity provider cannot be reached or used; reported in the bridge status."""


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    return verifier, _b64url(hashlib.sha256(verifier.encode()).digest())


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def safe_next(value: str | None) -> str:
    """A path on Desk, never another site (``//evil``, ``https://...``, ``/\\evil``)."""
    if not value or not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    return value


def claim(claims: dict[str, Any], path: str) -> Any:
    value: Any = claims
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def groups_of(value: Any) -> list[str] | None:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [g for g in value if isinstance(g, str)]
    return None


class OidcClient:
    """Discovery, the authorization URL, the code exchange and ID token checks."""

    def __init__(self, settings: OidcSettings, redirect_uri: str, *, http: httpx.AsyncClient | None = None,
                 clock=time.monotonic):
        self.settings = settings
        self.redirect_uri = redirect_uri
        self.http = http or httpx.AsyncClient(timeout=15.0)
        self._clock = clock
        self._discovery: dict[str, Any] | None = None
        self._discovered_at = 0.0
        self._jwks: jwt.PyJWKSet | None = None
        self.problem: str | None = None

    async def aclose(self) -> None:
        await self.http.aclose()

    async def discover(self, *, force: bool = False) -> dict[str, Any]:
        if self._discovery is not None and not force and self._clock() - self._discovered_at < DISCOVERY_FOR:
            return self._discovery
        where = f"the identity provider at {self.settings.issuer}"
        try:
            response = await self.http.get(self.settings.discovery_url)
            response.raise_for_status()
            document = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.problem = f"{where} cannot be reached: {exc or type(exc).__name__}"
            raise ProviderProblem(self.problem) from exc
        problem = None
        if not isinstance(document, dict):
            problem = "its discovery document is not a JSON object"
        elif document.get("issuer") != self.settings.issuer:
            problem = f"its discovery document names the issuer {document.get('issuer')!r}"
        elif not all(document.get(k) for k in ("authorization_endpoint", "token_endpoint", "jwks_uri")):
            problem = "its discovery document lacks an authorization, token or JWKS endpoint"
        elif "code_challenge_methods_supported" in document and \
                "S256" not in (document.get("code_challenge_methods_supported") or []):
            problem = "it does not support PKCE with S256"
        if problem:
            self.problem = f"{where} is not usable: {problem}"
            raise ProviderProblem(self.problem)
        self.problem = None
        self._discovery, self._discovered_at = document, self._clock()
        return document

    async def authorization_url(self, *, state: str, nonce: str, challenge: str) -> str:
        document = await self.discover()
        query = urlencode({
            "response_type": "code",
            "client_id": self.settings.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.settings.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        endpoint = document["authorization_endpoint"]
        return f"{endpoint}{'&' if '?' in endpoint else '?'}{query}"

    async def exchange(self, code: str, verifier: str) -> dict[str, Any]:
        document = await self.discover()
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
                "code_verifier": verifier}
        auth = None
        if self.settings.client_secret:
            auth = httpx.BasicAuth(self.settings.client_id, self.settings.client_secret)
        else:
            form["client_id"] = self.settings.client_id
        try:
            response = await self.http.post(document["token_endpoint"], data=form, auth=auth,
                                            headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise SignInFailed(f"the identity provider cannot be reached: {exc or type(exc).__name__}") from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not isinstance(body, dict) or not body.get("id_token"):
            detail = body.get("error_description") or body.get("error") if isinstance(body, dict) else None
            raise SignInFailed(f"the identity provider refused the sign-in ({detail or response.status_code})")
        return body

    async def _key(self, kid: str | None) -> jwt.PyJWK:
        for attempt in (0, 1):
            if self._jwks is None or attempt:
                document = await self.discover()
                try:
                    response = await self.http.get(document["jwks_uri"])
                    response.raise_for_status()
                    self._jwks = jwt.PyJWKSet.from_dict(response.json())
                except (httpx.HTTPError, ValueError, jwt.PyJWTError) as exc:
                    raise SignInFailed(f"the identity provider's keys cannot be read: {exc}") from exc
            for key in self._jwks.keys:
                if kid is None or key.key_id == kid:
                    return key
        raise SignInFailed("the ID token is signed with a key the identity provider does not list")

    async def verify(self, id_token: str, nonce: str) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(id_token)
        except jwt.PyJWTError as exc:
            raise SignInFailed("the ID token is not a JWT") from exc
        if header.get("alg") not in ALGORITHMS:
            raise SignInFailed(f"the ID token is signed with {header.get('alg')!r}, which Desk does not accept")
        key = await self._key(header.get("kid"))
        try:
            claims = jwt.decode(
                id_token, key.key, algorithms=[header["alg"]], audience=self.settings.client_id,
                issuer=self.settings.issuer, leeway=LEEWAY,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as exc:
            raise SignInFailed(f"the ID token is not valid: {exc}") from exc
        if claims.get("nonce") != nonce:
            raise SignInFailed("the ID token's nonce does not match the sign-in")
        return claims

    async def userinfo(self, access_token: str | None) -> dict[str, Any]:
        document = await self.discover()
        endpoint = document.get("userinfo_endpoint")
        if not endpoint or not access_token:
            return {}
        try:
            response = await self.http.get(endpoint, headers={"Authorization": f"Bearer {access_token}"})
            body = response.json() if response.status_code == 200 else {}
        except (httpx.HTTPError, ValueError):
            return {}
        return body if isinstance(body, dict) else {}


@dataclass
class SignedIn:
    person: Person
    token: str


class SignIn:
    """The login rows, the callback, people and sessions."""

    def __init__(self, sessions: SessionFactory, oidc: OidcClient, *, session_hours: float):
        self.sessions = sessions
        self.oidc = oidc
        self.session_hours = session_hours

    async def start(self, next_path: str | None) -> str:
        """The URL to send the browser to."""
        state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        verifier, challenge = pkce_pair()
        url = await self.oidc.authorization_url(state=state, nonce=nonce, challenge=challenge)
        async with self.sessions() as session:
            await session.execute(delete(Login).where(Login.created_at < utcnow() - LOGIN_FOR))
            session.add(Login(state=state, verifier=verifier, nonce=nonce, next=safe_next(next_path)))
            await session.commit()
        return url

    async def finish(self, code: str | None, state: str | None, error: str | None = None,
                     error_description: str | None = None) -> tuple[SignedIn, str]:
        """The callback: returns the new session and where to go next."""
        async with self.sessions() as session:
            login = await session.get(Login, state) if state else None
            if login is not None:
                await session.delete(login)
                await session.commit()
        if login is None or login.created_at < utcnow() - LOGIN_FOR:
            raise SignInFailed("this sign-in is unknown or has expired; sign in again")
        if error:
            raise SignInFailed(f"the identity provider answered {error}" +
                               (f": {error_description}" if error_description else ""))
        if not code:
            raise SignInFailed("the identity provider sent no code")
        tokens = await self.oidc.exchange(code, login.verifier)
        claims = await self.oidc.verify(tokens["id_token"], login.nonce)
        groups = groups_of(claim(claims, self.oidc.settings.groups_claim))
        if groups is None:
            info = await self.oidc.userinfo(tokens.get("access_token"))
            if info.get("sub") in (None, claims["sub"]):
                groups = groups_of(claim(info, self.oidc.settings.groups_claim))
                claims = {**info, **claims}
        person = await self._remember(claims, groups or [])
        token = secrets.token_urlsafe(32)
        async with self.sessions() as session:
            session.add(Session_(token_hash=hash_token(token), person_id=person.id,
                                 expires_at=utcnow() + timedelta(hours=self.session_hours)))
            await session.commit()
        log.info("%s signed in (%d groups)", person.name, len(person.groups))
        return SignedIn(person=person, token=token), login.next

    async def _remember(self, claims: dict[str, Any], groups: list[str]) -> Person:
        subject = str(claims["sub"])
        email = claims.get("email") if isinstance(claims.get("email"), str) else None
        name = next((claims[k] for k in ("name", "preferred_username", "email")
                     if isinstance(claims.get(k), str) and claims[k].strip()), subject)
        async with self.sessions() as session:
            person = await session.scalar(select(Person).where(Person.subject == subject))
            if person is None:
                person = Person(subject=subject, name=name)
                session.add(person)
            person.name = name[:255]
            person.email = email
            person.email_verified = claims.get("email_verified") is not False
            person.groups = sorted(set(groups))
            person.last_seen_at = utcnow()
            await session.commit()
            return person

    async def person(self, token: str | None) -> Person | None:
        if not token:
            return None
        async with self.sessions() as session:
            row = await session.get(Session_, hash_token(token))
            if row is None:
                return None
            if row.expires_at <= utcnow():
                await session.delete(row)
                await session.commit()
                return None
            return await session.get(Person, row.person_id)

    async def end(self, token: str | None) -> None:
        if not token:
            return
        async with self.sessions() as session:
            await session.execute(delete(Session_).where(Session_.token_hash == hash_token(token)))
            await session.commit()
