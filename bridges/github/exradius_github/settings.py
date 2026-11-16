"""The bridge's settings: the environment only (design §7). The kit's are read with them."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from urllib.parse import quote, urlsplit, urlunsplit

from exradius_bridge_kit import KitSettings, SettingsError


def broker_url(url: str, username: str, password: str) -> str:
    """The broker's user, URL-quoted into the URL, as the Helm chart hands it over."""
    if not url or not username:
        return url
    parts = urlsplit(url)
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parts.port:
        host = f"{host}:{parts.port}"
    userinfo = quote(username, safe="") + (f":{quote(password, safe='')}" if password else "")
    return urlunsplit((parts.scheme, f"{userinfo}@{host}", parts.path, parts.query, parts.fragment))


def web_url(api_url: str) -> str:
    """Where people see GitHub: https://github.com for api.github.com, else the server's
    root (GitHub Enterprise Server's API is ``https://<host>/api/v3``)."""
    parts = urlsplit(api_url)
    host = parts.netloc.lower()
    if host == "api.github.com":
        return "https://github.com"
    return f"{parts.scheme}://{parts.netloc}"


@dataclass(frozen=True)
class Settings:
    github_api_url: str
    github_token: str
    github_webhook_secret: str
    port: int
    kit: KitSettings

    @property
    def github_url(self) -> str:
        return web_url(self.github_api_url)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        """Every problem at once, the kit's included."""
        env = os.environ if env is None else env
        problems: list[str] = []

        def text(name: str) -> str:
            value = env.get(name, "").strip()
            if not value:
                problems.append(f"{name} is required")
            return value

        api = (env.get("GITHUB_API_URL", "").strip() or "https://api.github.com").rstrip("/")
        if not api.startswith(("https://", "http://")):
            problems.append("GITHUB_API_URL must start with https://")
        raw_port = env.get("PORT", "8080").strip() or "8080"
        port = int(raw_port) if raw_port.isdigit() and 0 < int(raw_port) < 65536 else 0
        if not port:
            problems.append(f"PORT must be a port number, not {raw_port!r}")
        values = dict(
            github_api_url=api,
            github_token=text("GITHUB_TOKEN"),
            github_webhook_secret=text("GITHUB_WEBHOOK_SECRET"),
            port=port,
        )
        try:
            kit = KitSettings.from_env(env)
        except SettingsError as exc:
            problems += str(exc).split("; ")
            kit = None
        if problems:
            raise SettingsError("; ".join(problems))
        assert kit is not None
        kit = replace(kit, broker_url=broker_url(kit.broker_url, env.get("EXRADIUS_BROKER_USERNAME", "").strip(),
                                                 env.get("EXRADIUS_BROKER_PASSWORD", "")))
        return cls(kit=kit, **values)
