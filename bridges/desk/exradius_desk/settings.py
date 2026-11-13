"""Desk's settings: the environment only (design §12). The kit's are read with them."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from urllib.parse import quote, urlsplit, urlunsplit

from exradius_bridge_kit import KitSettings, SettingsError

TLS_MODES = ("starttls", "tls", "none")


@dataclass(frozen=True)
class Smtp:
    host: str
    port: int
    tls: str
    username: str | None
    password: str | None
    sender: str


@dataclass(frozen=True)
class Oidc:
    issuer: str
    discovery_url: str
    client_id: str
    client_secret: str | None
    scopes: str
    groups_claim: str


@dataclass(frozen=True)
class Settings:
    database_url: str
    public_url: str
    oidc: Oidc
    session_hours: float
    smtp: Smtp | None
    port: int
    kit: KitSettings

    @property
    def secure_cookies(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def redirect_uri(self) -> str:
        return f"{self.public_url}/auth/callback"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Every problem at once, the kit's included."""
        env = os.environ if env is None else env
        problems: list[str] = []

        def text(name: str, default: str | None = None) -> str:
            value = env.get(name, "").strip() or (default or "")
            if not value:
                problems.append(f"{name} is required")
            return value

        def optional(name: str) -> str | None:
            return env.get(name, "").strip() or None

        def number(name: str, default: str, *, port: bool = False) -> float:
            raw = env.get(name, "").strip() or default
            try:
                value = float(raw)
                if value <= 0 or (port and (value != int(value) or value >= 65536)):
                    raise ValueError
            except ValueError:
                problems.append(f"{name} must be a {'port number' if port else 'positive number'}, not {raw!r}")
                return float(default)
            return value

        def url(name: str, value: str) -> str:
            value = value.rstrip("/")
            if value and not value.startswith(("https://", "http://")):
                problems.append(f"{name} must start with https:// or http://")
            return value

        database = normalise_database_url(text("DESK_DATABASE_URL"))
        public = url("DESK_PUBLIC_URL", text("DESK_PUBLIC_URL"))
        issuer = url("DESK_OIDC_ISSUER", text("DESK_OIDC_ISSUER"))
        discovery = url("DESK_OIDC_DISCOVERY_URL", optional("DESK_OIDC_DISCOVERY_URL") or "")
        oidc = Oidc(
            issuer=issuer,
            discovery_url=discovery or f"{issuer}/.well-known/openid-configuration",
            client_id=text("DESK_OIDC_CLIENT_ID"),
            client_secret=optional("DESK_OIDC_CLIENT_SECRET"),
            scopes=optional("DESK_OIDC_SCOPES") or "openid profile email",
            groups_claim=optional("DESK_OIDC_GROUPS_CLAIM") or "groups",
        )
        if "openid" not in oidc.scopes.split():
            problems.append("DESK_OIDC_SCOPES must include openid")

        smtp = None
        if host := optional("DESK_SMTP_HOST"):
            tls = (optional("DESK_SMTP_TLS") or "starttls").lower()
            if tls not in TLS_MODES:
                problems.append(f"DESK_SMTP_TLS must be one of {', '.join(TLS_MODES)}, not {tls!r}")
            sender = optional("DESK_SMTP_FROM")
            if not sender:
                problems.append("DESK_SMTP_FROM is required with DESK_SMTP_HOST")
            smtp = Smtp(host=host, port=int(number("DESK_SMTP_PORT", "587", port=True)), tls=tls,
                        username=optional("DESK_SMTP_USERNAME"), password=optional("DESK_SMTP_PASSWORD"),
                        sender=sender or "")
        hours = number("DESK_SESSION_HOURS", "12")
        port = int(number("PORT", "8080", port=True))

        try:
            kit = KitSettings.from_env(env)
        except SettingsError as exc:
            problems += str(exc).split("; ")
            kit = None
        if problems:
            raise SettingsError("; ".join(problems))
        assert kit is not None
        kit = replace(kit, broker_url=broker_url(kit.broker_url, optional("EXRADIUS_BROKER_USERNAME"),
                                                 optional("EXRADIUS_BROKER_PASSWORD")))
        return cls(database_url=database, public_url=public, oidc=oidc, session_hours=hours, smtp=smtp,
                   port=port, kit=kit)


def normalise_database_url(url: str) -> str:
    """CloudNativePG's ``uri`` is ``postgresql://``; Desk talks to it with asyncpg."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url.removeprefix(prefix)
    return url


def broker_url(url: str, username: str | None, password: str | None) -> str:
    """The broker's user, URL-quoted into the URL, as the chart hands it over (design §12)."""
    if not username:
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    userinfo = quote(username, safe="") + (f":{quote(password, safe='')}" if password else "")
    return urlunsplit((parts.scheme, f"{userinfo}@{host}", parts.path, parts.query, parts.fragment))
