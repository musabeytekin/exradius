"""Store kinds and their settings (docs/config-repo.md §7).

A store's ``settings`` say where an external store's values are, never how to sign in: the
install signs in with its own pod identity. They are validated per kind with strict pydantic
models (``extra="forbid"``), as ``exradius.yaml`` is, so a typo is an error, not a store that
silently reads the wrong place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class StoreSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    def where(self) -> str:
        """Where the values are, for the panel and for messages."""
        return ""


class InternalSettings(StoreSettings):
    """Nothing to set: the values are encrypted in Exradius's database."""


_SEGMENT = re.compile(r"^[A-Za-z0-9_.@-]+$")
PATH_RULE = "segments of letters, digits, '_', '.', '@' and '-', separated by '/' (no '.' or '..' segment)"


def _vault_path(value: str) -> str:
    segments = value.strip("/").split("/")
    if not value.strip("/") or any(not _SEGMENT.match(s) or s in (".", "..") for s in segments):
        raise ValueError(PATH_RULE)
    return "/".join(segments)


class VaultSettings(StoreSettings):
    """A store whose values are the fields of one Vault KV version 2 secret."""

    mount: Annotated[str, Field(title="KV mount", description="The mount path of a KV version 2 secrets engine.")] = "secret"
    path: Annotated[str, Field(title="Path", description="The secret's path in the mount; its fields are the store's keys.")]

    @field_validator("mount", "path")
    @classmethod
    def _segments(cls, value: str) -> str:
        return _vault_path(value)

    def where(self) -> str:
        return f"{self.mount}/{self.path}"


@dataclass(frozen=True)
class Kind:
    name: str
    label: str
    """How the panel and messages name it."""
    settings: type[StoreSettings]
    external: bool
    """Its values are set in the backend itself, never through Exradius."""


INTERNAL = Kind("internal", "Exradius", InternalSettings, external=False)
VAULT = Kind("vault", "HashiCorp Vault", VaultSettings, external=True)
KINDS: dict[str, Kind] = {kind.name: kind for kind in (INTERNAL, VAULT)}


class SettingsInvalid(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


_MESSAGES = {
    "extra_forbidden": "unknown key",
    "missing": "required",
    "string_type": "must be a string",
}


def kind(name: str) -> Kind:
    found = KINDS.get(name)
    if found is None:
        raise SettingsInvalid([f"kind: {name!r} is not a store kind (known: {', '.join(KINDS)})"])
    return found


def parse(kind_name: str, settings: dict[str, Any]) -> StoreSettings:
    """The kind's settings model; ``SettingsInvalid`` lists every problem, with its key."""
    model = kind(kind_name).settings
    try:
        return model.model_validate(settings)
    except ValidationError as exc:
        errors = []
        for error in exc.errors(include_url=False):
            where = ".".join(map(str, error["loc"])) or "settings"
            message = _MESSAGES.get(error["type"]) or error["msg"].removeprefix("Value error, ")
            errors.append(f"settings.{where}: {message}")
        raise SettingsInvalid(errors) from None


def normalize(kind_name: str, settings: dict[str, Any]) -> dict[str, Any]:
    """The settings as stored: validated, defaults filled in."""
    return parse(kind_name, settings).model_dump(mode="json")


def where(kind_name: str, settings: dict[str, Any]) -> str:
    """Where a store's values are, or ``""`` (internal, or settings this version cannot read)."""
    try:
        return parse(kind_name, settings).where()
    except SettingsInvalid:
        return ""


def fields(kind_name: str) -> list[dict[str, Any]]:
    """The settings a kind takes, for the panel's form: name, label, description, default,
    whether it is required."""
    out = []
    for name, info in kind(kind_name).settings.model_fields.items():
        out.append({
            "name": name,
            "label": info.title or name,
            "description": info.description or "",
            "default": None if info.is_required() else info.default,
            "required": info.is_required(),
        })
    return out
