"""Internal run API: what the runner and the API server exchange."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ConnectionSpecModel(BaseModel):
    name: str
    transport: Literal["stdio", "http"]
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    headers: dict[str, str] = Field(default_factory=dict)


class AttachmentPath(BaseModel):
    path: str
    """Relative to the working directory, e.g. ``.exradius/attachments/stack.txt``."""
    content_type: str
    id: str | None = None
    """For ``GET /attachments/{id}``: a task attachment's id, or ``<request id>.<n>``."""


class AttachmentItem(BaseModel):
    """A task attachment as the pod's ``prepare`` step fetches it."""

    id: str
    name: str
    content_type: str


class DeliveredAnswer(BaseModel):
    request_id: str
    question: str
    answer: str
    attachments: list[AttachmentPath] = Field(default_factory=list)


class RunSpecResponse(BaseModel):
    run_id: str
    task_id: str
    runtime: str
    mode: Literal["start", "resume"]
    agent_prompt: str
    environment_text: str = ""
    """The generated "# Your environment" section (docs/environment-and-services.md §1)."""
    model: str | None = None
    """The agent's model; ``None`` is the runtime's default."""
    instructions: str
    context: Any = None
    attachments: list[AttachmentPath] = Field(default_factory=list)
    connections: list[ConnectionSpecModel] = Field(default_factory=list)
    session_id: str | None = None
    answers: list[DeliveredAnswer] = Field(default_factory=list)


class SessionReport(BaseModel):
    session_id: str


class TranscriptEvent(BaseModel):
    seq: int
    payload: dict[str, Any]


class TranscriptBatch(BaseModel):
    events: list[TranscriptEvent]


class InfoRequestCreate(BaseModel):
    question: str = Field(min_length=1)
    context: str | None = None


class InfoRequestCreated(BaseModel):
    request_id: str


class ArtifactUpload(BaseModel):
    name: str = Field(min_length=1, max_length=255, pattern=r"^[^/\\\x00]+$")
    content_type: str = "application/octet-stream"
    content_base64: str


class ResultSubmission(BaseModel):
    output: Any = None
    artifacts: list[ArtifactUpload] = Field(default_factory=list)


class OutcomeReport(BaseModel):
    status: Literal["completed", "waiting_for_input", "failed", "cancelled"]
    session_id: str | None = None
    final_message: str | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    """The delivery check's findings (docs/kubernetes.md §8.5)."""


class Reported(BaseModel):
    """What the runner reports while it stays alive (docs/kubernetes.md §8.2)."""

    status: Literal["waiting", "resumed"]


class AnswersResponse(BaseModel):
    """``GET /answers?wait=``: the run's state, with the answers to resume with once the run
    is ``resuming``."""

    state: str
    answers: list[DeliveredAnswer] = Field(default_factory=list)


class VerifySpec(BaseModel):
    """The connections a verify Run lists the tools of (docs/agent-routing.md §3.1)."""

    connections: list[ConnectionSpecModel] = Field(default_factory=list)


class ToolReport(BaseModel):
    name: str
    ok: bool
    tools: list[dict[str, Any]] | None = None
    error: str | None = None
    log: str = ""


class ProbeReport(BaseModel):
    ok: bool
    detail: str | None = None
    error: str | None = None


# -- the egress proxy (docs/kubernetes.md §11) -------------------------------------------------


class EgressRoute(BaseModel):
    """An upstream base URL and the headers the proxy sets on it."""

    url: str
    headers: dict[str, str] = Field(default_factory=dict)


class EgressRepository(BaseModel):
    url: str
    push: bool
    headers: dict[str, str] = Field(default_factory=dict)


class EgressConnection(BaseModel):
    name: str
    url: str
    headers: dict[str, str] = Field(default_factory=dict)


class EgressPolicy(BaseModel):
    """What a run may reach in the agent phase, and its routes' credentials."""

    run: str
    allow: list[str] = Field(default_factory=list)
    model: EgressRoute | None = None
    repositories: list[EgressRepository] = Field(default_factory=list)
    connections: list[EgressConnection] = Field(default_factory=list)


class EgressRecordIn(BaseModel):
    run: str
    phase: str
    route: str
    method: str | None = None
    host: str
    port: int = 0
    path: str | None = None
    allowed: bool
    reason: str | None = None
    status: int | None = None
    bytes_sent: int = 0
    bytes_received: int = 0
    duration_ms: int = 0
    at: datetime


class EgressBatch(BaseModel):
    records: list[EgressRecordIn] = Field(default_factory=list, max_length=5000)
