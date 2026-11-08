"""Broker message contracts (architecture §4). Every message carries ``version: 1``."""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator, model_validator

from exradius.states import State

PROTOCOL_VERSION = 1


class _Message(BaseModel):
    # Unknown fields are ignored (§4).
    model_config = ConfigDict(extra="ignore")

    version: Literal[1] = PROTOCOL_VERSION


class AttachmentIn(BaseModel):
    """An attachment on a task or an answer: either a URL or inline base64 content."""

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=255)
    content_type: str = "application/octet-stream"
    url: str | None = None
    content_base64: str | None = None

    @field_validator("name")
    @classmethod
    def _plain_file_name(cls, value: str) -> str:
        if "/" in value or "\\" in value or value in {".", ".."} or "\x00" in value:
            raise ValueError("attachment name must be a plain file name")
        return value

    @model_validator(mode="after")
    def _exactly_one_source(self) -> AttachmentIn:
        if (self.url is None) == (self.content_base64 is None):
            raise ValueError("attachment needs exactly one of 'url' or 'content_base64'")
        if self.content_base64 is not None:
            try:
                base64.b64decode(self.content_base64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("content_base64 is not valid base64") from exc
        if self.url is not None and not self.url.startswith(("http://", "https://")):
            raise ValueError("attachment url must be http(s)")
        return self

    def decoded(self) -> bytes:
        assert self.content_base64 is not None
        return base64.b64decode(self.content_base64)


def _unique_names(attachments: list[AttachmentIn]) -> list[AttachmentIn]:
    names = [a.name for a in attachments]
    if len(names) != len(set(names)):
        raise ValueError("attachment names must be unique")
    return attachments


class TaskMessage(_Message):
    """Inbound on queue ``exradius.tasks`` (§4.1)."""

    task_id: str = Field(min_length=1, max_length=255)
    project: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    instructions: str
    context: Any = None
    attachments: list[AttachmentIn] = Field(default_factory=list)
    correlation: Any = None

    @field_validator("attachments")
    @classmethod
    def _names_unique(cls, value: list[AttachmentIn]) -> list[AttachmentIn]:
        return _unique_names(value)


class AnswerMessage(_Message):
    """Inbound on queue ``exradius.answers`` (§4.2)."""

    task_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    answer: str
    attachments: list[AttachmentIn] = Field(default_factory=list)

    @field_validator("attachments")
    @classmethod
    def _names_unique(cls, value: list[AttachmentIn]) -> list[AttachmentIn]:
        return _unique_names(value)


# --- Outbound -----------------------------------------------------------------


class StatusPayload(BaseModel):
    state: State
    previous: State | None = None
    reason: str | None = None


class InfoRequestPayload(BaseModel):
    request_id: str
    question: str
    context: str | None = None


def _http_url(value: str | None) -> str | None:
    if value is not None and not value.startswith(("http://", "https://")):
        raise ValueError("must be an http(s) download URL served by the Exradius API")
    return value


class ArtifactRef(BaseModel):
    name: str
    content_type: str
    url: str
    """HTTP download URL served by the Exradius API (signed, see §4.3)."""

    _check_url = field_validator("url")(_http_url)


class ResultPayload(BaseModel):
    state: Literal["completed", "failed", "cancelled"]
    summary: str | None = None
    output: Any = None
    artifacts: list[ArtifactRef] = Field(default_factory=list)
    transcript_url: str | None = None
    """HTTP download URL of the run's transcript (JSON lines), served by the Exradius API."""
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    """Work left in the run directory that was discarded with it (uncommitted changes,
    commits not pushed). Report only."""

    _check_transcript_url = field_validator("transcript_url")(_http_url)


EventType = Literal["status", "info_request", "result"]

ROUTING_KEYS: dict[str, str] = {
    "status": "task.status",
    "info_request": "task.info_request",
    "result": "task.result",
    "directory_changed": "directory.changed",
}
"""Every routing key on ``exradius.events``, by event type (the task events and the
directory notice)."""


def _utc_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class EventEnvelope(_Message):
    """Outbound on topic exchange ``exradius.events`` (§4.3)."""

    task_id: str
    run_id: str | None = None
    project: str | None = None
    agent: str | None = None
    correlation: Any = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    type: EventType
    payload: dict[str, Any]

    @field_serializer("timestamp")
    def _timestamp(self, value: datetime) -> str:
        return _utc_z(value)

    @property
    def routing_key(self) -> str:
        return ROUTING_KEYS[self.type]

    @model_validator(mode="after")
    def _payload_matches_type(self) -> EventEnvelope:
        model = {"status": StatusPayload, "info_request": InfoRequestPayload, "result": ResultPayload}
        model[self.type].model_validate(self.payload)
        return self

    def to_json(self) -> bytes:
        return self.model_dump_json().encode()


class DirectoryChangedPayload(BaseModel):
    projects: list[str]
    """Slugs whose directory entry was added, changed or removed. Informational: an
    integration fetches the whole directory either way."""


class DirectoryEvent(_Message):
    """Outbound on ``exradius.events`` with routing key ``directory.changed``
    (docs/integration-api.md §3). It belongs to no task, so it has no task fields."""

    type: Literal["directory_changed"] = "directory_changed"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    payload: DirectoryChangedPayload

    @field_serializer("timestamp")
    def _timestamp(self, value: datetime) -> str:
        return _utc_z(value)

    @property
    def routing_key(self) -> str:
        return ROUTING_KEYS[self.type]

    def to_json(self) -> bytes:
        return self.model_dump_json().encode()


def status_payload(state: State, previous: State | None, reason: str | None = None) -> dict:
    return StatusPayload(state=state, previous=previous, reason=reason).model_dump(mode="json")
