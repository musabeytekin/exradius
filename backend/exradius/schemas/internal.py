"""Internal run API: what the runner and the API server exchange."""

from __future__ import annotations

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
    status: Literal["completed", "waiting_for_input", "failed"]
    session_id: str | None = None
    final_message: str | None = None
    error: str | None = None
