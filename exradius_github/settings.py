"""The bridge's settings: environment variables only, no config file (design §7)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


class SettingsError(Exception):
    """Every problem found in the environment, reported at once."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def host_of(api_url: str) -> str:
    """The host repository URLs live on: github.com for api.github.com, else the API's host."""
    host = (urlsplit(api_url).hostname or "").lower()
    return "github.com" if host == "api.github.com" else host


@dataclass(frozen=True)
class Settings:
    github_app_id: str
    github_private_key: str
    github_webhook_secret: str
    github_api_url: str
    exradius_broker_url: str
    exradius_api_url: str
    exradius_integration_token: str
    port: int

    @property
    def github_host(self) -> str:
        return host_of(self.github_api_url)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        problems: list[str] = []

        def text(name: str, default: str | None = None) -> str:
            value = env.get(name, "").strip()
            if not value:
                if default is not None:
                    return default
                problems.append(f"{name} is not set")
            return value

        def url(name: str, schemes: tuple[str, ...], default: str | None = None) -> str:
            value = text(name, default).rstrip("/")
            if value and not value.startswith(schemes):
                problems.append(f"{name} must start with {' or '.join(schemes)}")
            return value

        app_id = text("GITHUB_APP_ID")
        if app_id and not app_id.isdigit():
            problems.append("GITHUB_APP_ID must be the App's numeric id")

        key_file = env.get("GITHUB_PRIVATE_KEY_FILE", "").strip()
        key_text = env.get("GITHUB_PRIVATE_KEY", "").strip()
        key = ""
        if key_file and key_text:
            problems.append("set GITHUB_PRIVATE_KEY_FILE or GITHUB_PRIVATE_KEY, not both")
        elif key_file:
            try:
                key = Path(key_file).read_text()
            except OSError as exc:
                problems.append(f"GITHUB_PRIVATE_KEY_FILE cannot be read: {exc.strerror or exc}")
        elif key_text:
            key = key_text.replace("\\n", "\n")
        else:
            problems.append("GITHUB_PRIVATE_KEY_FILE or GITHUB_PRIVATE_KEY is not set")
        if key and "PRIVATE KEY" not in key:
            problems.append("the GitHub App private key is not a PEM private key")

        raw_port = text("PORT", "8080")
        try:
            port = int(raw_port)
            if not 1 <= port <= 65535:
                problems.append("PORT must be between 1 and 65535")
        except ValueError:
            problems.append(f"PORT must be a whole number, not {raw_port!r}")
            port = 8080

        settings = cls(
            github_app_id=app_id,
            github_private_key=key,
            github_webhook_secret=text("GITHUB_WEBHOOK_SECRET"),
            github_api_url=url("GITHUB_API_URL", ("https://", "http://"), "https://api.github.com"),
            exradius_broker_url=url("EXRADIUS_BROKER_URL", ("amqp://", "amqps://")),
            exradius_api_url=url("EXRADIUS_API_URL", ("https://", "http://")),
            exradius_integration_token=text("EXRADIUS_INTEGRATION_TOKEN"),
            port=port,
        )
        if problems:
            raise SettingsError(problems)
        return settings
