"""SQLAlchemy models for the Exradius schema (architecture §6, docs/config-repo.md §8)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

Json = JSON().with_variant(JSONB(), "postgresql")
OrderedJson = JSON()
"""Plain ``json`` on Postgres, which keeps the text as written: JSONB reorders object keys,
and a config's services, env, connections and agents are ordered by the file."""


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
    """A config repository holding ``exradius.yaml`` (docs/config-repo.md §1). Everything
    else about the project is read from that file; ``snapshot`` caches the latest read for
    the panel. Runs never use the cache: they read the file afresh."""

    __tablename__ = "projects"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    """``project.slug`` of the file when the project was added; tasks name it."""
    config_url: Mapped[str] = mapped_column(Text)
    config_branch: Mapped[str | None] = mapped_column(String(255))
    """``None``: the remote's default branch."""
    config_path: Mapped[str | None] = mapped_column(Text)
    """``None``: ``exradius.yaml``, then ``.exradius/exradius.yaml``."""
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(OrderedJson, nullable=True)
    """``ProjectConfigSnapshot.to_json()`` of the latest read."""
    snapshot_commit: Mapped[str | None] = mapped_column(String(64))
    snapshot_read_at: Mapped[datetime | None] = mapped_column(Timestamp)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)


class GitCredential(Base):
    """How Exradius reads config repositories on one git host (§6)."""

    __tablename__ = "git_credentials"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    host: Mapped[str] = mapped_column(String(255), unique=True)
    kind: Mapped[str] = mapped_column(String(20))  # token | basic
    value_encrypted: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)


class SecretStore(Base):
    """A named set of secrets a project's config refers to with ``secret_store`` (§7)."""

    __tablename__ = "secret_stores"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(63), unique=True)
    kind: Mapped[str] = mapped_column(String(20), default="internal")
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)

    entries: Mapped[list[SecretStoreEntry]] = relationship(
        cascade="all, delete-orphan", order_by="SecretStoreEntry.key"
    )


class SecretStoreEntry(Base):
    __tablename__ = "secret_store_entries"
    __table_args__ = (UniqueConstraint("store_id", "key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("secret_stores.id", ondelete="CASCADE"), index=True)
    key: Mapped[str] = mapped_column(String(255))
    value_encrypted: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow, onupdate=utcnow)


class ImageBuild(Base):
    """An image built from a Dockerfile in a config repository (§4)."""

    __tablename__ = "image_builds"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(63))
    """``workstation`` or the service's name."""
    tree_hash: Mapped[str] = mapped_column(String(64))
    image: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(20))  # running | succeeded | failed
    started_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(Timestamp)
    log_tail: Mapped[str] = mapped_column(Text, default="")


class Provider(Base):
    """A named credential profile a runtime injects into run containers (providers.md §2)."""

    __tablename__ = "providers"
    __table_args__ = (
        # At most one default; the API keeps exactly one once any provider exists.
        Index(
            "uq_providers_default",
            "is_default",
            unique=True,
            postgresql_where=text("is_default"),
            sqlite_where=text("is_default"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    kind: Mapped[str] = mapped_column(String(50))
    auth_method: Mapped[str] = mapped_column(String(50))
    credential_encrypted: Mapped[str] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    last_test: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    """ok, tested_at, detail, error; cleared when the credential or settings change."""
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow, onupdate=utcnow)


class ProviderTest(Base):
    """A provider test that needs a run container (``oauth_token``), run by a worker."""

    __tablename__ = "provider_tests"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    provider_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String(20))  # pending | running | passed | failed
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    ended_at: Mapped[datetime | None] = mapped_column(Timestamp)
    detail: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The model the test session uses; ``None`` is the runtime's default."""
    fingerprint: Mapped[str] = mapped_column(String(64))
    """Of the provider's credential and settings when the test was requested; the result
    is stored as the provider's ``last_test`` only if they are unchanged."""
    worker_id: Mapped[str | None] = mapped_column(String(64))


# --- Tasks and runs ---------------------------------------------------------------------


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # the user's task_id
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="RESTRICT"))
    agent: Mapped[str] = mapped_column(String(63), index=True)
    """The agent's slug in the project's config."""
    instructions: Mapped[str] = mapped_column(Text)
    context: Mapped[Any] = mapped_column(nullable=True)
    correlation: Mapped[Any] = mapped_column(nullable=True)
    state: Mapped[str] = mapped_column(String(30), index=True)
    received_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow, index=True)

    project: Mapped[Project] = relationship(lazy="joined", innerjoin=True)
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
    config_commit: Mapped[str | None] = mapped_column(String(64))
    """The config repository commit the run read in ``preparing``."""
    config: Mapped[dict[str, Any] | None] = mapped_column(OrderedJson, nullable=True)
    """The parsed config of that commit (secret references as written), so resumes and
    the run spec use what the run started with."""
    agent_snapshot: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    """name, model, provider, runtime, identity and connection names of the agent."""

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


class Worker(Base):
    """Worker heartbeats, for reporting only (liveness for recovery uses advisory locks)."""

    __tablename__ = "workers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(Timestamp)
    last_seen_at: Mapped[datetime] = mapped_column(Timestamp, index=True)
    concurrency: Mapped[int] = mapped_column(Integer)
    docker_ok: Mapped[bool] = mapped_column(default=False)
    docker_detail: Mapped[str | None] = mapped_column(Text)
    images: Mapped[list[Any]] = mapped_column(default=list)
    runtimes: Mapped[list[Any]] = mapped_column(default=list)
    """Runtime bundles: [{name, version, ready, image, error}] (runtime-bundles.md §3)."""
    version: Mapped[str | None] = mapped_column(String(50))


class ProjectVerification(Base):
    """A dry run of a project's preparation (onboarding §3.2)."""

    __tablename__ = "project_verifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    state: Mapped[str] = mapped_column(String(20))  # pending | running | passed | failed
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    ended_at: Mapped[datetime | None] = mapped_column(Timestamp)
    error: Mapped[str | None] = mapped_column(Text)
    steps: Mapped[list[Any]] = mapped_column(default=list)
    worker_id: Mapped[str | None] = mapped_column(String(64))


class AdminUser(Base):
    __tablename__ = "admin_user"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)


__all__ = [
    "AdminUser",
    "Artifact",
    "Attachment",
    "Base",
    "Event",
    "GitCredential",
    "ImageBuild",
    "InfoRequest",
    "Project",
    "ProjectVerification",
    "Provider",
    "ProviderTest",
    "Run",
    "SecretStore",
    "SecretStoreEntry",
    "Task",
    "TranscriptChunk",
    "Worker",
    "utcnow",
]
