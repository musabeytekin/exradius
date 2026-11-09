"""SQLAlchemy models for the Exradius schema (architecture §6, docs/config-repo.md §8)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
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

from exradius.crypto import new_hook_token

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
    hook_token: Mapped[str] = mapped_column(String(64), unique=True, default=new_hook_token)
    """The token of the config hook URL (docs/integration-api.md §2). It grants only a
    re-read, so it is stored as is and shown in the panel."""
    hook_last_at: Mapped[datetime | None] = mapped_column(Timestamp)
    """The last accepted call of the config hook."""


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
    """An image built from a Dockerfile in a config repository (config-repo.md §4): one per
    ``Build`` object (docs/kubernetes.md §12), written by the worker from its status."""

    __tablename__ = "image_builds"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    """Also the Build's ``spec.id``: the path of its context on the internal API."""
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(63))
    """``workstation`` or the service's name."""
    tree_hash: Mapped[str] = mapped_column(String(64))
    image: Mapped[str] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(20))  # running | succeeded | failed
    started_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(Timestamp)
    log_tail: Mapped[str] = mapped_column(Text, default="")
    build_object: Mapped[str | None] = mapped_column(String(253))
    """The Build's name in the builds namespace."""
    digest: Mapped[str | None] = mapped_column(String(100))
    """The pushed image's manifest digest."""
    context_key: Mapped[str | None] = mapped_column(String(255))
    """The context tar in the object store, served to the build pod."""
    token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    """The build pod's token for its context; cleared when the build ends."""


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
    token_hash: Mapped[str | None] = mapped_column(String(64))
    """Of the probe Run's token (docs/kubernetes.md §13); cleared when the test ends."""
    report: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    """What the probe reported through the internal API: ok, detail, error."""


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
    runtime_session_id: Mapped[str | None] = mapped_column(String(255))
    started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    ended_at: Mapped[datetime | None] = mapped_column(Timestamp)
    error: Mapped[str | None] = mapped_column(Text)
    # Implementation columns (see "Implementation notes" in docs/architecture.md).
    token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    reported_status: Mapped[str | None] = mapped_column(String(30))
    summary: Mapped[str | None] = mapped_column(Text)
    output: Mapped[Any] = mapped_column(nullable=True)
    resume_request_ids: Mapped[list[Any] | None] = mapped_column(nullable=True)
    run_object: Mapped[str | None] = mapped_column(String(100))
    """The name of the run's Kubernetes ``Run`` once the worker created it (``run-<id>``);
    recovery tells a Run that disappeared from one never created (docs/kubernetes.md §4)."""
    delivery_warnings: Mapped[list[Any] | None] = mapped_column(nullable=True)
    """What the runner's delivery check found after the session (§8.5), for the result."""
    config_commit: Mapped[str | None] = mapped_column(String(64))
    """The config repository commit the run read in ``preparing``."""
    config: Mapped[dict[str, Any] | None] = mapped_column(OrderedJson, nullable=True)
    """The parsed config of that commit (secret references as written), so resumes and
    the run spec use what the run started with."""
    agent_snapshot: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    """name, model, provider, runtime, identity and connection names of the agent."""
    waiting_for: Mapped[str | None] = mapped_column(String(30))
    """``capacity`` while the run's pod cannot be scheduled (docs/kubernetes.md §6); the run
    stays ``preparing``."""
    routing: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    """How the agent was picked (docs/agent-routing.md §8)."""

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
    runtimes: Mapped[list[Any]] = mapped_column(default=list)
    """The configured runtime bundles: [{name, version, ready, image, error}]."""
    operator: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    """The operator as this worker sees its leader Lease: alive, holder, renewed_at."""
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
    commit: Mapped[str | None] = mapped_column(String(64))
    """The config commit "Read config" read (setup-and-verify.md §1.2)."""
    diagnosis: Mapped[str | None] = mapped_column(Text)
    """What went wrong, from ``worker/diagnose.py``; ``None`` when passed or unrecognised."""
    token_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    """Of the verify Run's token (docs/kubernetes.md §13); cleared when it ends."""
    tool_reports: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    """What the verify pod's runner listed, by connection: ok, tools, error, log."""


# --- Integrations (docs/integration-api.md) ------------------------------------------------


class DirectoryState(Base):
    """The directory as last published with ``directory.changed`` (§3). One row, ``id`` 1."""

    __tablename__ = "directory_state"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    directory: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(Timestamp)


class IntegrationToken(Base):
    """A bearer token for ``/api/integration/v1/*`` (§5), bound to one bridge. Only its
    sha256 is stored."""

    __tablename__ = "integration_tokens"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    bridge: Mapped[str] = mapped_column(String(100), index=True)
    """The bridge whose directory the token reads and whose status it reports."""
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(Timestamp)
    """Written at most once a minute."""
    revoked_at: Mapped[datetime | None] = mapped_column(Timestamp)


class BridgeStatus(Base):
    """The latest status report of a bridge (integration-api.md §7)."""

    __tablename__ = "bridge_status"

    bridge: Mapped[str] = mapped_column(String(100), primary_key=True)
    report: Mapped[dict[str, Any]] = mapped_column()
    reported_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    token_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("integration_tokens.id", ondelete="SET NULL"))


# --- Agent routing (docs/agent-routing.md) --------------------------------------------------


class ConnectionTools(Base):
    """A connection's MCP tools as the last Verify listed them (§3.1)."""

    __tablename__ = "connection_tools"
    __table_args__ = (UniqueConstraint("project_id", "connection"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_uuid)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    connection: Mapped[str] = mapped_column(String(100))
    definition_hash: Mapped[str] = mapped_column(String(64))
    """sha256 of the connection's definition as the config writes it (secret references)."""
    tools: Mapped[list[Any]] = mapped_column(default=list)
    commit: Mapped[str | None] = mapped_column(String(64))
    verification_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("project_verifications.id", ondelete="SET NULL")
    )
    listed_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)


class RoutingSettings(Base):
    """The OpenAI key routing uses (§7). One row, ``id`` 1."""

    __tablename__ = "routing_settings"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    openai_key_encrypted: Mapped[str | None] = mapped_column(Text)
    key_set_at: Mapped[datetime | None] = mapped_column(Timestamp)
    last_test: Mapped[dict[str, Any] | None] = mapped_column(nullable=True)


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
    "BridgeStatus",
    "ConnectionTools",
    "DirectoryState",
    "Event",
    "GitCredential",
    "ImageBuild",
    "InfoRequest",
    "IntegrationToken",
    "Project",
    "ProjectVerification",
    "Provider",
    "ProviderTest",
    "RoutingSettings",
    "Run",
    "SecretStore",
    "SecretStoreEntry",
    "Task",
    "TranscriptChunk",
    "Worker",
    "utcnow",
]
