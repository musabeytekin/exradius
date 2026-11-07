"""Admin API request and response models."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from exradius.schemas.messages import AttachmentIn

Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]*$", max_length=100)]
ModelId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/\[\]-]*$", max_length=255, strip_whitespace=True)]


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


# Projects (docs/config-repo.md §11) ------------------------------------------------------


def _optional_text(value):
    """Blank strings mean "not set" (the remote's default branch, the default file)."""
    return value.strip() or None if isinstance(value, str) else value


class ProjectIn(BaseModel):
    config_url: str = Field(min_length=1)
    config_branch: str | None = None
    """``null``: the remote's default branch."""
    config_path: str | None = None
    """``null``: ``exradius.yaml``, then ``.exradius/exradius.yaml``."""

    _blank = field_validator("config_branch", "config_path", mode="before")(_optional_text)


class ProjectUpdate(BaseModel):
    config_branch: str | None = None
    config_path: str | None = None
    """Either set to ``null`` returns to the default."""

    _blank = field_validator("config_branch", "config_path", mode="before")(_optional_text)


class RequiredKeyOut(BaseModel):
    name: str
    present: bool
    """Whether the project's store holds it."""


Freshness = Literal["current", "stale", "failed", "running", "never"]


class VerificationSummary(_Out):
    """The latest verification, for the project's header and lists (setup-and-verify.md §1.2)."""

    id: uuid.UUID
    state: Literal["pending", "running", "passed", "failed"]
    commit: str | None
    ended_at: datetime | None
    diagnosis: str | None


class ProjectOut(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    """``project.name`` of the file (the slug when unreadable)."""
    description: str = ""
    config_url: str
    config_branch: str | None
    config_path: str | None
    commit: str | None
    """The commit of the latest read; ``null`` when it could not be read."""
    read_at: datetime | None
    branch: str | None
    """The branch read (``config_branch`` or the remote's default)."""
    path: str | None
    """The file read."""
    health: Literal["ok", "config_errors", "missing_secrets"]
    runnable: bool
    reason: str | None
    """Why tasks are refused, as the rejection says it."""
    errors: list[str]
    warnings: list[str]
    store: str | None
    store_id: uuid.UUID | None
    store_exists: bool
    required_keys: list[RequiredKeyOut]
    missing_keys: list[str]
    agents: list[str]
    """Agent slugs of the config, for the send form."""
    config: dict[str, Any] | None
    """The parsed file (secret references as written), for read-only rendering."""
    cache_bytes: int | None = None
    """Disk usage of the project's caches (``workstation.cache``), on the project page only;
    ``null`` when none exist on disk."""
    raw: str | None
    """The file's text."""
    created_at: datetime
    verification: VerificationSummary | None = None
    freshness: Freshness = "never"
    """Advice, never a gate: ``current`` passed at ``commit``, ``stale`` passed at an older
    commit, ``failed``, ``running`` (pending or running) or ``never``."""


class ResolvedConnection(BaseModel):
    name: str
    catalog: str | None
    transport: Literal["stdio", "http"]
    command: str | None
    args: list[str]
    url: str | None
    env: dict[str, str]
    headers: dict[str, str]
    """Values with each secret as ``$NAME``."""
    secrets: list[str]
    """Secret names the connection uses."""


class ResolvedProvider(BaseModel):
    id: uuid.UUID
    slug: str
    name: str
    kind: str
    auth_method: str


class IdentityOut(BaseModel):
    name: str
    email: str


class ResolvedAgentOut(BaseModel):
    project_id: uuid.UUID
    commit: str | None
    agent: str
    name: str
    role: str
    model: str | None
    """``null``: the runtime's default."""
    runtime: str
    provider: str | None
    """As the config names it; ``null`` is the default provider."""
    resolved_provider: ResolvedProvider | None
    """The provider runs would use now; ``null`` with ``provider_error``."""
    provider_error: str | None
    identity: IdentityOut
    connections: list[ResolvedConnection]
    image: str
    """The workstation: its image name or ``built from <dockerfile>``."""
    environment_text: str
    """The "# Your environment" section as a run of this config gets it."""


class CatalogConfigKeyOut(BaseModel):
    key: str
    label: str
    required: bool
    target: Literal["env", "header", "arg"]
    name: str | None
    """The environment variable or header it sets."""
    format: str
    default: str | None


class CatalogEntryOut(BaseModel):
    key: str
    name: str
    description: str
    transport: Literal["stdio", "http"]
    command: str | None
    args: list[str]
    url: str | None
    requires: str | None
    docs: str | None
    config: list[CatalogConfigKeyOut]


# Config repository access (§6) and secret stores (§7); values are write-only ------------

StoreName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]*$", max_length=63)]
Host = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9.-]+(:[0-9]+)?$", max_length=255, to_lower=True)]


class GitCredentialIn(BaseModel):
    host: Host
    """``github.com``; config repositories on this host are read with it."""
    kind: Literal["token", "basic"]
    value: str = Field(min_length=1)
    """A token, or ``user:password`` for ``basic``."""

    @model_validator(mode="after")
    def _basic(self) -> GitCredentialIn:
        if self.kind == "basic" and ":" not in self.value:
            raise ValueError("a basic credential is user:password")
        return self


class GitCredentialOut(_Out):
    id: uuid.UUID
    host: str
    kind: str
    created_at: datetime


class StoreIn(BaseModel):
    name: StoreName


class StoreOut(BaseModel):
    id: uuid.UUID
    name: str
    kind: str
    created_at: datetime
    keys: list[str]
    used_by: list[str]
    """Slugs of the projects whose config names this store."""


class StoreKeyOut(BaseModel):
    key: str
    updated_at: datetime


class StoreValueIn(BaseModel):
    value: str = Field(min_length=1)


class StoreValuesIn(BaseModel):
    values: dict[str, Annotated[str, Field(min_length=1)]] = Field(min_length=1)


# Verification ------------------------------------------------------------------------------


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
    commit: str | None = None
    diagnosis: str | None = None


class VerificationCreated(BaseModel):
    verification_id: uuid.UUID


# Providers (docs/providers.md) -----------------------------------------------------------


class ModelOut(BaseModel):
    id: str
    description: str


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
    models: list[ModelOut] = Field(default_factory=list)
    """The catalog's models for this provider's kind."""
    created_at: datetime
    updated_at: datetime


class ProviderTestIn(BaseModel):
    model: ModelId | None = None
    """Check this model id too (before saving it on an agent)."""


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
    model: str | None = None


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
    config_commit: str | None = None
    """The config repository commit the run used."""
    agent_snapshot: dict[str, Any] | None = None
    """name, model, provider, runtime, identity and connections of the agent at run start."""
    waiting_for: str | None = None
    """``memory`` while the run waits for its memory reservation."""


class TaskSummaryOut(BaseModel):
    task_id: str
    project: str
    agent: str
    state: str
    received_at: datetime
    run_id: uuid.UUID | None
    waiting_for: str | None = None
    """``memory`` while the run waits for its memory reservation (it stays queued)."""


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
    models: list[ModelOut] = Field(default_factory=list)
    """Models the provider offers, for the agent form (any other id may be typed)."""


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
