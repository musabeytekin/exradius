"""Admin API request and response models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*$", max_length=100)]
EnvKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=255)]
FolderName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=255)]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginIn(BaseModel):
    username: str
    password: str


class AdminOut(_Out):
    id: uuid.UUID
    username: str


# Projects ----------------------------------------------------------------------------


class ProjectIn(BaseModel):
    slug: Slug
    name: str
    image: str | None = None
    setup_commands: list[str] = Field(default_factory=list)


class ProjectUpdate(BaseModel):
    slug: Slug | None = None
    name: str | None = None
    image: str | None = None
    setup_commands: list[str] | None = None


class ProjectOut(_Out):
    id: uuid.UUID
    slug: str
    name: str
    image: str | None
    setup_commands: list[str]
    created_at: datetime


class RepositoryIn(BaseModel):
    name: FolderName
    url: str
    branch: str = "main"
    auth_secret_id: uuid.UUID | None = None


class RepositoryUpdate(BaseModel):
    name: FolderName | None = None
    url: str | None = None
    branch: str | None = None
    auth_secret_id: uuid.UUID | None = None


class RepositoryOut(_Out):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    url: str
    branch: str
    auth_secret_id: uuid.UUID | None


class EnvVarIn(BaseModel):
    key: EnvKey
    value: str


class EnvVarUpdate(BaseModel):
    key: EnvKey | None = None
    value: str | None = None


class EnvVarOut(_Out):
    id: uuid.UUID
    project_id: uuid.UUID
    key: str
    value: str


# Secrets are write-only: the value is accepted but never returned.


class SecretIn(BaseModel):
    key: EnvKey
    value: str


class SecretUpdate(BaseModel):
    value: str


class SecretOut(_Out):
    id: uuid.UUID
    project_id: uuid.UUID | None
    key: str


# Connections and agents ----------------------------------------------------------------


class ConnectionIn(BaseModel):
    slug: Slug
    name: str
    transport: Literal["stdio", "http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _transport_fields(self) -> ConnectionIn:
        if self.transport == "stdio" and not self.command:
            raise ValueError("a stdio connection needs a command")
        if self.transport == "http" and not self.url:
            raise ValueError("an http connection needs a url")
        if self.slug == "exradius":
            raise ValueError("'exradius' is reserved for the built-in connection")
        return self


class ConnectionUpdate(BaseModel):
    slug: Slug | None = None
    name: str | None = None
    transport: Literal["stdio", "http"] | None = None
    command: str | None = None
    args: list[str] | None = None
    url: str | None = None
    env: dict[str, str] | None = None
    headers: dict[str, str] | None = None


class ConnectionOut(_Out):
    id: uuid.UUID
    slug: str
    name: str
    transport: str
    command: str | None
    args: list[str]
    url: str | None
    env: dict[str, str]
    headers: dict[str, str]
    notice: str = (
        "Connections that reach production systems should use read-only credentials."
    )


class ConnectionTestOut(BaseModel):
    ok: bool
    tools: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class AgentIn(BaseModel):
    slug: Slug
    name: str
    prompt: str = ""
    runtime: Literal["claude_code"] = "claude_code"
    connection_ids: list[uuid.UUID] = Field(default_factory=list)


class AgentUpdate(BaseModel):
    slug: Slug | None = None
    name: str | None = None
    prompt: str | None = None
    runtime: Literal["claude_code"] | None = None
    connection_ids: list[uuid.UUID] | None = None


class AgentOut(_Out):
    id: uuid.UUID
    slug: str
    name: str
    prompt: str
    runtime: str
    created_at: datetime
    connection_ids: list[uuid.UUID]


class ProjectAgentIn(BaseModel):
    agent_id: uuid.UUID
    connection_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    secret_overrides: dict[str, str] = Field(default_factory=dict)


class ProjectAgentUpdate(BaseModel):
    connection_overrides: dict[str, dict[str, Any]] | None = None
    secret_overrides: dict[str, str] | None = None


class ProjectAgentOut(_Out):
    project_id: uuid.UUID
    agent_id: uuid.UUID
    connection_overrides: dict[str, Any]
    secret_overrides: dict[str, Any]


# Tasks and runs ------------------------------------------------------------------------


class RunOut(_Out):
    id: uuid.UUID
    state: str
    container_id: str | None
    runtime_session_id: str | None
    started_at: datetime | None
    ended_at: datetime | None
    error: str | None
    summary: str | None
    output: Any = None


class TaskSummaryOut(BaseModel):
    task_id: str
    project: str
    agent: str
    state: str
    received_at: datetime
    run_id: uuid.UUID | None


class AttachmentOut(_Out):
    id: uuid.UUID
    name: str
    content_type: str
    object_key: str | None
    source_url: str | None


class InfoRequestOut(_Out):
    id: uuid.UUID
    question: str
    context: str | None
    created_at: datetime
    answered_at: datetime | None
    answer: str | None
    answer_attachments: list[Any] | None
    late_answers: list[Any]


class ArtifactOut(_Out):
    id: uuid.UUID
    name: str
    content_type: str
    object_key: str


class EventOut(_Out):
    id: uuid.UUID
    type: str
    payload: dict[str, Any]
    created_at: datetime
    published_at: datetime | None


class TaskDetailOut(TaskSummaryOut):
    instructions: str
    context: Any = None
    correlation: Any = None
    attachments: list[AttachmentOut]
    run: RunOut | None
    info_requests: list[InfoRequestOut]
    artifacts: list[ArtifactOut]
    events: list[EventOut]


class TranscriptChunkOut(_Out):
    seq: int
    payload: dict[str, Any]
