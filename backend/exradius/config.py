"""Process configuration, read from ``EXRADIUS_*`` environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from exradius.version import package_version


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EXRADIUS_", extra="ignore")

    # Infrastructure
    database_url: str = "postgresql+asyncpg://exradius:exradius@localhost:5432/exradius"
    broker_url: str = "amqp://guest:guest@localhost:5672/"

    # Object store for transcripts, artifacts and attachments. "filesystem" (default)
    # keeps objects under <data_dir>/objects/<bucket>/; "s3" uses any S3 API.
    object_store: Literal["filesystem", "s3"] = "filesystem"
    s3_endpoint_url: str | None = None
    """``None`` means AWS S3; set it for another S3-compatible service."""
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_bucket: str = "exradius"
    s3_region: str = "us-east-1"

    public_broker_host: str | None = None
    public_broker_port: int | None = None
    """Broker host and port as integrations reach them (shown on the integration page);
    default to those of ``broker_url``."""

    public_api_url: str = "http://localhost:8000"
    """Base URL of the API as consumers of outbound events reach it; used to build the
    artifact and transcript download URLs in ``task.result``."""

    # Security
    secret_key: str = ""
    """Fernet key used to encrypt secrets at rest (``Fernet.generate_key()``)."""
    session_secret: str = ""
    """Signs the admin session cookie. Defaults to a value derived from ``secret_key``."""
    cookie_secure: bool = False
    admin_username: str = "admin"
    admin_password: str | None = None
    """Seeds the admin user at API start when none exists. It does not change an existing
    password; that is done in the panel."""

    openai_base_url: str = "https://api.openai.com/v1"
    """Where agent routing calls OpenAI's Decisions API (docs/agent-routing.md §7): a proxy
    the installer runs, or a fake in tests. The key itself is set in the panel."""

    anthropic_api_key: str | None = None
    """Legacy: read once, at API start, to create the default provider "Anthropic
    (migrated)" when no provider exists (docs/providers.md §3). Never passed to runs."""

    # API server
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Worker
    data_dir: Path = Path("/var/lib/exradius")
    """Where the worker keeps ``runs/`` and ``mirrors/`` (path inside the worker)."""
    host_data_dir: Path | None = None
    """The same directory as the Docker daemon sees it. Defaults to ``data_dir``."""
    worker_concurrency: int = 4
    """Prefetch count of the task consumer: how many runs one worker executes at once."""
    run_memory_budget: str | None = None
    """Memory the runs and verifications of this worker may reserve together, as a Docker
    size (``24g``). Default: the Docker daemon's ``MemTotal`` less 1 GiB, read at start."""
    runtime_image_claude_code: str = Field(default_factory=lambda: f"exradius-runtime-claude-code:{package_version()}")
    """Image holding the Claude Code runtime bundle (deploy/runtime-claude-code.Dockerfile)."""
    project_default_image: str = Field(default_factory=lambda: f"exradius-project-default:{package_version()}")
    """Image of run containers of projects without an image, and of provider probes."""
    runtime_mount: str = "/opt/exradius/runtime"
    """Where run containers see the runtime bundle (read-only)."""
    run_network: str | None = "exradius-runs"
    """Network every run, setup and probe container joins: the API's alias is there and
    nothing else of the stack is (never the stack network: its ``postgres`` would shadow a
    service of the same name). ``None`` uses Docker's default bridge."""
    run_api_url: str = "http://exradius-api:8000"
    """``EXRADIUS_API_URL`` given to run containers (the API's alias on ``run_network``)."""
    run_user: str = "1000:1000"
    run_state_mount: str = "/exradius/state"
    docker_url: str | None = None
    """Docker daemon URL; ``None`` uses the environment (``DOCKER_HOST`` or the local socket)."""

    @field_validator("s3_endpoint_url", "s3_access_key", "s3_secret_key", "run_memory_budget", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: str | None) -> str | None:
        return value or None

    def runtime_image(self, runtime: str) -> str:
        """The bundle image of a runtime (``EXRADIUS_RUNTIME_IMAGE_<NAME>``)."""
        return getattr(self, f"runtime_image_{runtime}")

    @property
    def objects_dir(self) -> Path:
        return self.data_dir / "objects"

    @property
    def download_signing_key(self) -> bytes:
        """Signs the download URLs in ``task.result`` (derived from ``secret_key``)."""
        if not self.secret_key:
            raise RuntimeError("EXRADIUS_SECRET_KEY must be set")
        import hashlib

        return hashlib.sha256(b"exradius-download:" + self.secret_key.encode()).digest()

    @property
    def effective_host_data_dir(self) -> Path:
        return self.host_data_dir or self.data_dir

    @property
    def effective_session_secret(self) -> str:
        if self.session_secret:
            return self.session_secret
        if not self.secret_key:
            raise RuntimeError("EXRADIUS_SECRET_KEY (or EXRADIUS_SESSION_SECRET) must be set")
        import hashlib

        return hashlib.sha256(b"exradius-session:" + self.secret_key.encode()).hexdigest()

    @property
    def run_uid_gid(self) -> tuple[int, int]:
        uid, _, gid = self.run_user.partition(":")
        return int(uid), int(gid or uid)


@lru_cache
def get_settings() -> Settings:
    return Settings()
