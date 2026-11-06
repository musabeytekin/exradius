"""Admin API request and response models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from exradius.schemas.messages import AttachmentIn

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*$", max_length=100)]
EnvKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$", max_length=255)]
IdentityName = Annotated[str, StringConstraints(pattern=r"^[^<>\x00-\x1f]+$", min_length=1, max_length=255, strip_whitespace=True)]
"""A git author name: no angle brackets or control characters (git rejects them)."""
IdentityEmail = Annotated[str, StringConstraints(pattern=r"^[^@<>\s]+@[^@<>\s]+$", max_length=255, strip_whitespace=True)]
FolderName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", max_length=255)]


class _Out(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginIn(BaseModel):
    username: str
    password: str


class AdminOut(_Out):
    id: uuid.UUID
    username: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


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
    push_allowed: bool = False
    """Runs may push with the auth secret; needs ``auth_secret_id``."""


class RepositoryUpdate(BaseModel):
    name: FolderName | None = None
    url: str | None = None
    branch: str | None = None
    auth_secret_id: uuid.UUID | None = None
    push_allowed: bool | None = None


class RepositoryCheckOut(BaseModel):
    ok: bool
    branches: list[str] = Field(default_factory=list)
    default_branch: str | None = None
    error: str | None = None
    checked_at: datetime | None = None


class RepositoryOut(_Out):
    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    url: str
    branch: str
    auth_secret_id: uuid.UUID | None
    push_allowed: bool
    last_check: RepositoryCheckOut | None = None


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


class ToolInfo(BaseModel):
    name: str
    description: str = ""


class ConnectionLastTest(BaseModel):
    ok: bool
    tested_at: datetime
    tools: list[ToolInfo] = Field(default_factory=list)
    error: str | None = None


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
    last_test: ConnectionLastTest | None = None
    notice: str = (
        "Connections that reach production systems should use read-only credentials."
    )


class ConnectionTestOut(BaseModel):
    ok: bool
    tools: list[dict[str, Any]] = Field(default_factory=list)
    error: str | None = None


class SecretReference(BaseModel):
    name: str
    """As written in ``${secret:NAME}``."""
    resolves_to: str
    """The secret name looked up after the attachment's secret overrides."""
    scope: Literal["project", "global"] | None
    """Where it was found; ``None`` when it is unresolved."""


class ResolvedConnection(BaseModel):
    slug: str
    name: str
    transport: str
    command: str | None
    args: list[str]
    url: str | None
    env: dict[str, str]
    headers: dict[str, str]
    overridden: bool
    references: list[SecretReference]
    unresolved: list[str]


class ResolvedAgentOut(BaseModel):
    project_id: uuid.UUID
    agent_id: uuid.UUID
    secret_overrides: dict[str, Any]
    connections: list[ResolvedConnection]


class VerificationStep(BaseModel):
    name: str
    state: Literal["pending", "running", "passed", "failed", "skipped"]
    started_at: datetime | None = None
    ended_at: datetime | None = None
    log: str = ""


class VerificationOut(_Out):
    id: uuid.UUID
    project_id: uuid.UUID
    state: Literal["pending", "running", "passed", "failed"]
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    error: str | None
    steps: list[VerificationStep]


class VerificationCreated(BaseModel):
    verification_id: uuid.UUID


class AgentIn(BaseModel):
    slug: Slug
    name: str
    prompt: str = ""
    runtime: Literal["claude_code"] = "claude_code"
    connection_ids: list[uuid.UUID] = Field(default_factory=list)
    provider_id: uuid.UUID | None = None
    """``None`` uses the default provider."""
    identity_name: IdentityName | None = None
    """Git author/committer name in runs; defaults to ``name``."""
    identity_email: IdentityEmail


class AgentUpdate(BaseModel):
    slug: Slug | None = None
    name: str | None = None
    prompt: str | None = None
    runtime: Literal["claude_code"] | None = None
    connection_ids: list[uuid.UUID] | None = None
    provider_id: uuid.UUID | None = None
    identity_name: IdentityName | None = None
    identity_email: IdentityEmail | None = None


class AgentOut(_Out):
    id: uuid.UUID
    slug: str
    name: str
    prompt: str
    runtime: str
    created_at: datetime
    connection_ids: list[uuid.UUID]
    provider_id: uuid.UUID | None
    identity_name: str
    identity_email: str


# Providers (docs/providers.md) -----------------------------------------------------------


class ProviderIn(BaseModel):
    slug: Slug
    name: str = Field(min_length=1)
    kind: str
    auth_method: str
    credential: str = Field(min_length=1)
    """Write-only: stored encrypted, never returned."""
    settings: dict[str, str] = Field(default_factory=dict)


class ProviderUpdate(BaseModel):
    slug: Slug | None = None
    name: str | None = Field(default=None, min_length=1)
    kind: str | None = None
    auth_method: str | None = None
    credential: str | None = Field(default=None, min_length=1)
    """Replaces the stored credential when given."""
    settings: dict[str, str] | None = None


class ProviderLastTest(BaseModel):
    ok: bool
    tested_at: datetime
    detail: str | None = None
    error: str | None = None


class ProviderOut(_Out):
    id: uuid.UUID
    slug: str
    name: str
    kind: str
    auth_method: str
    settings: dict[str, Any]
    is_default: bool
    last_test: ProviderLastTest | None
    agent_count: int
    created_at: datetime
    updated_at: datetime


class ProviderTestResult(BaseModel):
    """``POST /api/providers/{id}/test`` for checks the API makes itself (``api_key``)."""

    ok: bool
    detail: str | None = None
    error: str | None = None


class ProviderTestCreated(BaseModel):
    """``POST /api/providers/{id}/test`` for checks a worker makes (``oauth_token``)."""

    test_id: uuid.UUID


class ProviderTestOut(_Out):
    id: uuid.UUID
    provider_id: uuid.UUID
    state: Literal["pending", "running", "passed", "failed"]
    created_at: datetime
    started_at: datetime | None
    ended_at: datetime | None
    detail: str | None
    error: str | None


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
    task_id: str
    run_id: uuid.UUID | None
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


class AnswerSubmission(BaseModel):
    request_id: str = Field(min_length=1)
    answer: str
    attachments: list[AttachmentIn] = Field(default_factory=list)


class PublishedOut(BaseModel):
    queue: str
    message: dict[str, Any]
    """Exactly what was published."""


# System ----------------------------------------------------------------------------------


class CheckOut(BaseModel):
    ok: bool
    detail: str | None = None


class RuntimeBundleOut(BaseModel):
    name: str
    version: str | None
    ready: bool
    image: str | None = None
    error: str | None = None


class WorkerOut(_Out):
    id: str
    hostname: str
    started_at: datetime
    last_seen_at: datetime
    concurrency: int
    docker_ok: bool
    docker_detail: str | None
    runtimes: list[RuntimeBundleOut]
    version: str | None


class DefaultProviderOut(_Out):
    id: uuid.UUID
    name: str
    kind: str
    auth_method: str
    last_test: ProviderLastTest | None


class ProviderStatusOut(BaseModel):
    configured: bool
    default: DefaultProviderOut | None
    count: int


class SystemStatusOut(BaseModel):
    database: CheckOut
    broker: CheckOut
    workers: list[WorkerOut]
    provider: ProviderStatusOut
    public_api_url: str
    data_dir: str
    version: str


class ImageOut(BaseModel):
    tag: str
    created_at: str | None = None
    size: int | None = None


class SettingFieldOut(BaseModel):
    name: str
    label: str
    required: bool


class AuthMethodOut(BaseModel):
    name: str
    label: str
    credential_label: str
    settings: list[SettingFieldOut]
    notice: str | None = None


class ProviderKindOut(BaseModel):
    kind: str
    label: str
    auth_methods: list[AuthMethodOut]


class RuntimeOut(BaseModel):
    name: str
    label: str
    providers: list[ProviderKindOut]


class BrokerInfoOut(BaseModel):
    host: str
    port: int
    vhost: str
    username: str | None
    password_set: bool


class IntegrationOut(BaseModel):
    broker: BrokerInfoOut
    tasks_queue: str
    answers_queue: str
    events_exchange: str
    events_exchange_type: str = "topic"
    routing_keys: list[str]
    public_api_url: str
