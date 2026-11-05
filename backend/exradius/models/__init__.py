"""SQLAlchemy models for the Exradius schema (architecture §6)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Json = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {dict[str, Any]: Json, list[Any]: Json, Any: Json}


Timestamp = DateTime(timezone=True)


# --- Configuration ------------------------------------------------------------------


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    image: Mapped[str | None] = mapped_column(String(255))
    setup_commands: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)

    repositories: Mapped[list[Repository]] = relationship(
        back_populates="project", cascade="all, delete-orphan", order_by="Repository.name"
    )
    env_vars: Mapped[list[EnvVar]] = relationship(
        cascade="all, delete-orphan", order_by="EnvVar.key"
    )


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    branch: Mapped[str] = mapped_column(String(255), default="main")
    auth_secret_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("secrets.id", ondelete="SET NULL")
    )

    project: Mapped[Project] = relationship(back_populates="repositories")


class EnvVar(Base):
    __tablename__ = "env_vars"
    __table_args__ = (UniqueConstraint("project_id", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(255))
    value: Mapped[str] = mapped_column(Text)


class Secret(Base):
    __tablename__ = "secrets"
    __table_args__ = (UniqueConstraint("project_id", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE")
    )
    key: Mapped[str] = mapped_column(String(255))
    value_encrypted: Mapped[str] = mapped_column(Text)


class Connection(Base):
    __tablename__ = "connections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    transport: Mapped[str] = mapped_column(String(20))  # stdio | http
    command: Mapped[str | None] = mapped_column(Text)
    args: Mapped[list[Any]] = mapped_column(default=list)
    url: Mapped[str | None] = mapped_column(Text)
    env: Mapped[dict[str, Any]] = mapped_column(default=dict)
    headers: Mapped[dict[str, Any]] = mapped_column(default=dict)


class AgentConnection(Base):
    __tablename__ = "agent_connections"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    connection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("connections.id", ondelete="CASCADE"), primary_key=True
    )


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    prompt: Mapped[str] = mapped_column(Text, default="")
    runtime: Mapped[str] = mapped_column(String(50), default="claude_code")
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)

    connections: Mapped[list[Connection]] = relationship(
        secondary="agent_connections", order_by="Connection.slug", lazy="selectin"
    )


class ProjectAgent(Base):
    __tablename__ = "project_agents"

    project_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agents.id", ondelete="CASCADE"), primary_key=True
    )
    connection_overrides: Mapped[dict[str, Any]] = mapped_column(default=dict)
    secret_overrides: Mapped[dict[str, Any]] = mapped_column(default=dict)


# --- Tasks and runs ---------------------------------------------------------------------


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # the user's task_id
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    agent_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("agents.id", ondelete="RESTRICT"))
    instructions: Mapped[str] = mapped_column(Text)
    context: Mapped[Any] = mapped_column(nullable=True)
    correlation: Mapped[Any] = mapped_column(nullable=True)
    state: Mapped[str] = mapped_column(String(30), index=True)
    received_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow, index=True)

    project: Mapped[Project] = relationship(lazy="joined", innerjoin=True)
    agent: Mapped[Agent] = relationship(lazy="joined", innerjoin=True)
    attachments: Mapped[list[Attachment]] = relationship(
        cascade="all, delete-orphan", lazy="selectin", order_by="Attachment.name"
    )


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(255))
    object_key: Mapped[str | None] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String(30), index=True)
    container_id: Mapped[str | None] = mapped_column(String(100))
    runtime_session_id: Mapped[str | None] = mapped_column(String(255))
    directory: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    ended_at: Mapped[datetime | None] = mapped_column(Timestamp)
    error: Mapped[str | None] = mapped_column(Text)
    # Implementation columns (see "Implementation notes" in docs/architecture.md).
    token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    reported_status: Mapped[str | None] = mapped_column(String(30))
    summary: Mapped[str | None] = mapped_column(Text)
    output: Mapped[Any] = mapped_column(nullable=True)
    resume_request_ids: Mapped[list[Any] | None] = mapped_column(nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(64))
    """The worker driving the run through preparing/running/resuming (crash recovery)."""

    task: Mapped[Task] = relationship(lazy="joined", innerjoin=True)


class InfoRequest(Base):
    __tablename__ = "info_requests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    question: Mapped[str] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    answered_at: Mapped[datetime | None] = mapped_column(Timestamp)
    answer: Mapped[str | None] = mapped_column(Text)
    answer_attachments: Mapped[list[Any] | None] = mapped_column(nullable=True)
    # Implementation columns.
    published_at: Mapped[datetime | None] = mapped_column(Timestamp)
    delivered_at: Mapped[datetime | None] = mapped_column(Timestamp)
    late_answers: Mapped[list[Any]] = mapped_column(default=list)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    # Not a foreign key: rejected tasks are not stored but their events are.
    task_id: Mapped[str] = mapped_column(String(255), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    type: Mapped[str] = mapped_column(String(30))
    payload: Mapped[dict[str, Any]] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(Timestamp)


class TranscriptChunk(Base):
    __tablename__ = "transcript_chunks"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"), primary_key=True, autoincrement=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column()


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(255))
    object_key: Mapped[str] = mapped_column(Text)


class AdminUser(Base):
    __tablename__ = "admin_user"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)


__all__ = [
    "AdminUser",
    "Agent",
    "AgentConnection",
    "Artifact",
    "Attachment",
    "Base",
    "Connection",
    "EnvVar",
    "Event",
    "InfoRequest",
    "Project",
    "ProjectAgent",
    "Repository",
    "Run",
    "Secret",
    "Task",
    "TranscriptChunk",
    "utcnow",
]
