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
    raw: str | None
    """The file's text."""
    created_at: datetime
    verification: VerificationSummary | None = None
    freshness: Freshness = "never"
    """Advice, never a gate: ``current`` passed at ``commit``, ``stale`` passed at an older
    commit, ``failed``, ``running`` (pending or running) or ``never``."""
    hook_url: str
    """The config hook (docs/integration-api.md §2), built from ``EXRADIUS_PUBLIC_API_URL``."""
    hook_last_at: datetime | None = None
    """The last accepted hook call; ``null``: never."""
    bridges: list["BridgeFileOut"] = Field(default_factory=list)
    """The config's bridge files (config-repo.md §2.1)."""
    bridge_problems: list["BridgeProblemOut"] = Field(default_factory=list)
    """What the bridges last reported about this project (integration-api.md §7)."""


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
    run_object: str | None = None
    """The run's Kubernetes ``Run`` (``run-<id>``), once created; deleted when the run ends."""
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
    """``capacity`` while the run's pod waits for room in the cluster (it stays preparing)."""
    routing: dict[str, Any] | None = None
    """How the agent was picked (docs/agent-routing.md §8)."""


class TaskSummaryOut(BaseModel):
    task_id: str
    project: str
    agent: str
    state: str
    received_at: datetime
    run_id: uuid.UUID | None
    waiting_for: str | None = None
    """``capacity`` while the run's pod waits for room in the cluster (it stays preparing)."""


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


class NetworkHostOut(BaseModel):
    """One host a run's pod asked the egress proxy for."""

    host: str
    ports: list[int] = Field(default_factory=list)
    phases: list[str] = Field(default_factory=list)
    """``setup`` and/or ``agent``."""
    routes: list[str] = Field(default_factory=list)
    """``connect`` (a tunnel), ``http``, ``model``, ``git``, ``mcp``."""
    requests: int = 0
    bytes: int = 0
    last_at: datetime | None = None
    reason: str | None = None
    """Why the latest refusal was refused (refused hosts only)."""


class NetworkOut(BaseModel):
    """``GET /api/tasks/{task_id}/network``: hosts reached and refused."""

    reached: list[NetworkHostOut] = Field(default_factory=list)
    refused: list[NetworkHostOut] = Field(default_factory=list)
    requests: int = 0


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
    runtimes: list[RuntimeBundleOut]
    version: str | None


class OperatorOut(BaseModel):
    """The operator as the workers see its leader Lease (docs/kubernetes.md §15)."""

    alive: bool
    holder: str | None = None
    renewed_at: str | None = None
    detail: str | None = None


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
    operator: OperatorOut | None = None
    """``None`` while no live worker reported it."""
    provider: ProviderStatusOut
    public_api_url: str
    version: str


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


# Integrations (docs/integration-api.md) -----------------------------------------------------


BridgeName = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9-]*$", max_length=100)]
"""A bridge's name, as in ``bridges/<name>.yaml`` (config-repo.md §2.1)."""


class IntegrationTokenIn(BaseModel):
    bridge: BridgeName
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)] | None = None
    """A unique label; defaults to the bridge."""


class IntegrationTokenOut(_Out):
    id: uuid.UUID
    name: str
    bridge: str
    created_at: datetime
    last_used_at: datetime | None
    """Written at most once a minute."""
    revoked_at: datetime | None


class IntegrationTokenCreated(IntegrationTokenOut):
    token: str
    """Shown once: only its sha256 is stored."""


class DirectoryAgentOut(BaseModel):
    slug: str
    name: str


class DirectoryRepositoryOut(BaseModel):
    name: str
    url: str
    """As the config writes it, without any userinfo."""


class DirectoryProjectOut(BaseModel):
    slug: str
    name: str
    runnable: bool
    repositories: list[DirectoryRepositoryOut]
    """In config order."""
    agents: list[DirectoryAgentOut]
    bridge_config: dict[str, Any] | None
    """The token's bridge file, as written."""
    bridge_config_error: str | None
    """Set instead of ``bridge_config`` when the file is broken."""


class DirectoryOut(BaseModel):
    """``GET /api/integration/v1/directory`` (§4): the token's bridge's projects by slug."""

    bridge: str
    projects: list[DirectoryProjectOut]


MAX_PROBLEMS = 100
MAX_PROBLEM_MESSAGE = 1000


class BridgeProblem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    project: str | None = None
    """The project slug the problem is about; ``null`` for the bridge as a whole."""
    message: str


class BridgeStatusIn(BaseModel):
    """``PUT /api/integration/v1/status`` (integration-api.md §7)."""

    model_config = ConfigDict(extra="ignore")

    version: Literal[1] = 1
    software: Annotated[str, StringConstraints(max_length=200)] = ""
    started_at: datetime | None = None
    problems: list[BridgeProblem] = Field(default_factory=list)


class BridgeOut(BaseModel):
    """A bridge as the panel shows it: from its tokens, its files and its reports."""

    bridge: str
    tokens: int
    """Tokens bound to it that are not revoked."""
    projects: list[str]
    """Projects whose config has its file."""
    reported_at: datetime | None
    reporting: bool
    """A report in the last 15 minutes."""
    software: str | None
    started_at: datetime | None
    problems: list[BridgeProblem]


class BridgeFileOut(BaseModel):
    name: str
    config: dict[str, Any] | None
    error: str | None


class BridgeProblemOut(BaseModel):
    bridge: str
    message: str
    reported_at: datetime


# Agent routing (docs/agent-routing.md) --------------------------------------------------------


class RoutingTestOut(BaseModel):
    ok: bool
    at: datetime
    ms: int | None = None
    detail: str
    """The answer (``web, confidence 0.97``) or the error."""


class RoutingOut(BaseModel):
    key_set: bool
    key_set_at: datetime | None
    last_test: RoutingTestOut | None
    model: str
    guess_below: float


class RoutingKeyIn(BaseModel):
    key: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class ToolOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    description: str | None = None
    annotations: dict[str, Any] | None = None


class ProfileConnectionOut(BaseModel):
    name: str
    kind: str
    tools: list[ToolOut] | None
    tools_listed_at: datetime | None = None
    tools_note: str | None = None


class AgentProfileOut(BaseModel):
    """What routing sees of an agent (docs/agent-routing.md §3)."""

    slug: str
    name: str
    role: str | None
    connections: list[ProfileConnectionOut]
