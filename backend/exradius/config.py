"""Process configuration, read from ``EXRADIUS_*`` environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    """When set, the admin user is created (or its password reset) at API start."""

    # The single, global Anthropic API key handed to every run container.
    anthropic_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("EXRADIUS_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
    )

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
    runner_image: str = "exradius-runner:latest"
    run_network: str | None = None
    run_api_url: str = "http://api:8000"
    """``EXRADIUS_API_URL`` given to run containers."""
    run_user: str = "1000:1000"
    run_state_mount: str = "/exradius/state"
    docker_url: str | None = None
    """Docker daemon URL; ``None`` uses the environment (``DOCKER_HOST`` or the local socket)."""

    @field_validator("s3_endpoint_url", "s3_access_key", "s3_secret_key", mode="before")
    @classmethod
    def _empty_is_unset(cls, value: str | None) -> str | None:
        return value or None

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
