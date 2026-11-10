"""The kit's settings, from the environment (design §10). A bridge adds its own."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


class SettingsError(Exception):
    """Every problem with the environment at once."""


@dataclass(frozen=True)
class KitSettings:
    broker_url: str
    api_url: str
    integration_token: str
    max_attachment_mb: float = 10.0

    @property
    def max_attachment_bytes(self) -> int:
        return int(self.max_attachment_mb * 1024 * 1024)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> KitSettings:
        env = os.environ if environ is None else environ
        problems = [f"{name} is required" for name in ("EXRADIUS_BROKER_URL", "EXRADIUS_API_URL",
                                                       "EXRADIUS_INTEGRATION_TOKEN") if not env.get(name, "").strip()]
        size = env.get("MAX_ATTACHMENT_MB", "10").strip() or "10"
        try:
            mb = float(size)
            if mb <= 0:
                raise ValueError
        except ValueError:
            problems.append(f"MAX_ATTACHMENT_MB must be a positive number, not {size!r}")
            mb = 10.0
        if problems:
            raise SettingsError("; ".join(problems))
        return cls(
            broker_url=env["EXRADIUS_BROKER_URL"].strip(),
            api_url=env["EXRADIUS_API_URL"].strip().rstrip("/"),
            integration_token=env["EXRADIUS_INTEGRATION_TOKEN"].strip(),
            max_attachment_mb=mb,
        )
