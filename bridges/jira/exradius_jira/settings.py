"""The bridge's settings: the environment only (design §7). The kit's are read with them."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from exradius_bridge_kit import KitSettings, SettingsError


@dataclass(frozen=True)
class Settings:
    jira_site: str
    jira_bot_email: str
    jira_bot_token: str
    jira_webhook_secret: str
    port: int
    kit: KitSettings

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

        site = text("JIRA_SITE").rstrip("/")
        if site and not site.startswith(("https://", "http://")):
            problems.append("JIRA_SITE must start with https://")
        raw_port = env.get("PORT", "8080").strip() or "8080"
        port = int(raw_port) if raw_port.isdigit() and 0 < int(raw_port) < 65536 else 0
        if not port:
            problems.append(f"PORT must be a port number, not {raw_port!r}")
        values = dict(
            jira_site=site,
            jira_bot_email=text("JIRA_BOT_EMAIL"),
            jira_bot_token=text("JIRA_BOT_TOKEN"),
            jira_webhook_secret=text("JIRA_WEBHOOK_SECRET"),
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
        return cls(kit=kit, **values)
