"""Process configuration, read from ``EXRADIUS_*`` environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import quote, unquote, urlsplit, urlunsplit

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from exradius.version import package_version


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EXRADIUS_", extra="ignore")

    # Infrastructure
    database_url: str = "postgresql+asyncpg://exradius:exradius@localhost:5432/exradius"
    """``postgresql://`` (CloudNativePG's ``uri``) is accepted and means asyncpg."""
    broker_url: str = "amqp://guest:guest@localhost:5672/"
    broker_username: str | None = None
    broker_password: str | None = None
    """When set, they replace the user info of ``broker_url`` (quoted as needed), so the
    RabbitMQ operator's generated credentials can come from its Secret as they are."""

    # Object store for transcripts, artifacts and attachments (docs/kubernetes.md §14): an S3
    # API (SeaweedFS in a local install, S3 in AWS), Google Cloud Storage or Azure Blob
    # Storage. In clouds each signs in with the pod's identity (exradius/pod_identity.py).
    # The filesystem and in-memory stores are for tests.
    object_store: Literal["s3", "gcs", "azure"] = "s3"
    bucket: str = "exradius"
    """The S3 or GCS bucket, or the Azure Blob container."""
    s3_endpoint_url: str | None = None
    """``None`` means AWS S3; set it for another S3-compatible service."""
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    """Both ``None``: boto3's default chain, which reads EKS Pod Identity or IRSA."""
    s3_region: str = "us-east-1"
    gcs_endpoint: str | None = None
    """An emulator (fake-gcs-server), reached without credentials; ``None`` is Google Cloud
    Storage with the GKE Workload Identity token."""
    azure_account: str | None = None
    """The storage account of ``object_store: azure``."""
    azure_endpoint: str | None = None
    """``None`` is ``https://<account>.blob.core.windows.net``; Azurite is
    ``http://<host>:10000/<account>``."""
    azure_account_key: str | None = None
    """A Shared Key (from a Secret) for clusters outside AKS and for Azurite; ``None`` signs in
    with AKS Workload Identity."""

    public_broker_host: str | None = None
    public_broker_port: int | None = None
    """Broker host and port as integrations reach them (shown on the integration page);
    default to those of ``broker_url``."""

    public_api_url: str = "http://localhost:8000"
    """Base URL of the API as consumers of outbound events reach it; used to build the
    artifact and transcript download URLs in ``task.result``."""

    # Security
    secret_key: str = ""
    """Fernet key used to encrypt secrets at rest (``Fernet.generate_key()``): the primary key."""
    secret_key_previous: str = ""
    """Keys being rotated out, comma-separated: they decrypt and verify download signatures,
    never encrypt or sign (docs/kubernetes.md §10.1)."""
    session_secret: str = ""
    """Signs the admin session cookie. Defaults to a value derived from ``secret_key``, so
    sessions end when the primary key changes."""
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
    """The admin, integration, hook and download API."""
    internal_port: int = 8001
    """``/internal/*`` only: what run pods reach (docs/kubernetes.md §9)."""

    cache_dir: Path = Path("/var/cache/exradius")
    """Config-repository mirrors of this pod (an ``emptyDir``; re-fetched after a restart)."""

    # Worker
    worker_concurrency: int = 4
    """Prefetch count of the task consumer: how many Runs one worker has in flight."""
    runtime_image_claude_code: str = Field(default_factory=lambda: f"exradius-runtime-claude-code:{package_version()}")
    """Image holding the Claude Code runtime bundle, mounted into run pods as an image volume
    (deploy/runtime-claude-code.Dockerfile)."""
    project_default_image: str = Field(default_factory=lambda: f"exradius-project-default:{package_version()}")
    """Workstation image of projects without an image, and of provider probes."""
    namespace: str = "exradius"
    """The stack's namespace (the worker's own, from the downward API)."""
    runs_namespace: str = "exradius-runs"
    """Where the worker creates Run objects and per-run Secrets."""
    operator_lease: str = "exradius-operator"
    """The operator's leader-election Lease in ``namespace``, read for the status page."""
    kube_api_url: str | None = None
    """The Kubernetes API; ``None`` is the in-cluster address (``KUBERNETES_SERVICE_HOST``)."""
    builds_namespace: str = "exradius-builds"
    """Where the worker creates Build objects and their token Secrets (docs/kubernetes.md §12)."""
    builds_registry: str = ""
    """Prefix of built images: ``kind-registry:5000``, ``<account>.dkr.ecr.<region>.amazonaws.com``.
    The build pods push there and the nodes pull from there."""
    builds_cache: bool = True
    """Keep each image's layer cache in the registry at ``<image>:buildcache``."""
    builds_credentials: Literal["none", "ecr", "gcr", "acr"] = "none"
    """How the worker signs in to ``builds_registry`` to read built service images' configs
    (their ``HEALTHCHECK``), with the pod's identity: ``ecr`` (boto3's chain), ``gcr``
    (Artifact Registry, GKE Workload Identity), ``acr`` (AKS Workload Identity). Values
    ``builds.credentialHelper``, which also picks the build pods' credential helper."""
    insecure_registries: str = ""
    """Registries reached over plain HTTP, comma-separated (the kind registry); the worker
    reads services' image configs there (docs/kubernetes.md §7.4)."""

    # The egress proxy (docs/kubernetes.md §11)
    egress_url: str = "http://exradius-egress.exradius.svc.cluster.local:3129"
    """The proxy's injecting reverse proxy, as run pods reach it: the worker points
    ``ANTHROPIC_BASE_URL`` and each repository's git route there, the api each HTTP
    connection with secret headers."""
    egress_allow: str = ""
    """Hosts every run may reach in the agent phase, comma-separated (values
    ``egress.allow``), on top of each config's ``network.allow``."""
    egress_service_account: str = "system:serviceaccount:exradius:exradius-egress"
    """The proxy's identity: ``/internal/egress/*`` accepts only its ServiceAccount token."""
    egress_audience: str = "exradius-api"
    """The audience that token is issued for, checked with a TokenReview."""

    # HashiCorp Vault, for secret stores of kind ``vault`` (docs/config-repo.md §7): one per
    # install, signed in to with the pod's ServiceAccount through Vault's Kubernetes auth
    # method. Exradius stores no Vault credential. Values ``vault.*``.
    vault_address: str | None = None
    """``https://vault.example.com:8200``; ``None``: no Vault, stores of kind vault cannot be made."""
    vault_auth_mount: str = "kubernetes"
    """The path the Kubernetes auth method is enabled at."""
    vault_role: str = "exradius"
    """The Kubernetes auth role the api and the worker sign in as; its policy reads the stores' paths."""
    vault_namespace: str | None = None
    """A Vault Enterprise or HCP Vault namespace (``X-Vault-Namespace``)."""
    vault_ca_file: Path | None = None
    """A PEM bundle that verifies Vault's certificate; ``None``: the system's CAs."""
    vault_token_file: Path = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
    """The ServiceAccount token sent to Vault, read again at every sign-in (the kubelet
    rotates it): the pod's own, or a projected one with Vault's audience (values ``vault.audience``)."""

    @field_validator(
        "s3_endpoint_url", "s3_access_key", "s3_secret_key", "broker_username", "broker_password", "kube_api_url",
        "gcs_endpoint", "azure_account", "azure_endpoint", "azure_account_key", "vault_address", "vault_namespace",
        "vault_ca_file",
        mode="before",
    )
    @classmethod
    def _empty_is_unset(cls, value: str | None) -> str | None:
        return value or None

    @field_validator("database_url")
    @classmethod
    def _asyncpg(cls, value: str) -> str:
        for prefix in ("postgresql://", "postgres://"):
            if value.startswith(prefix):
                return "postgresql+asyncpg://" + value.removeprefix(prefix)
        return value

    @property
    def effective_broker_url(self) -> str:
        """``broker_url`` with ``broker_username``/``broker_password`` in its user info."""
        if self.broker_username is None and self.broker_password is None:
            return self.broker_url
        parts = urlsplit(self.broker_url)
        user = quote(self.broker_username if self.broker_username is not None else unquote(parts.username or ""), safe="")
        password = self.broker_password if self.broker_password is not None else unquote(parts.password or "")
        host = parts.hostname or "localhost"
        if ":" in host:
            host = f"[{host}]"
        netloc = f"{user}:{quote(password, safe='')}@{host}" + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    @property
    def egress_allow_hosts(self) -> list[str]:
        return [host.strip() for host in self.egress_allow.split(",") if host.strip()]

    @property
    def insecure_registry_hosts(self) -> list[str]:
        return [host.strip() for host in self.insecure_registries.split(",") if host.strip()]

    def runtime_image(self, runtime: str) -> str:
        """The bundle image of a runtime (``EXRADIUS_RUNTIME_IMAGE_<NAME>``)."""
        return getattr(self, f"runtime_image_{runtime}")

    @property
    def previous_secret_keys(self) -> list[str]:
        return [key.strip() for key in self.secret_key_previous.split(",") if key.strip()]

    @property
    def download_signing_key(self) -> bytes:
        """Signs the download URLs in ``task.result`` (derived from ``secret_key``)."""
        if not self.secret_key:
            raise RuntimeError("EXRADIUS_SECRET_KEY must be set")
        return _download_key(self.secret_key)

    @property
    def download_verify_keys(self) -> list[bytes]:
        """The signing key, then those derived from the previous keys: URLs published before
        a rotation keep verifying while their key is still configured."""
        return [self.download_signing_key, *(_download_key(key) for key in self.previous_secret_keys)]

    @property
    def effective_session_secret(self) -> str:
        if self.session_secret:
            return self.session_secret
        if not self.secret_key:
            raise RuntimeError("EXRADIUS_SECRET_KEY (or EXRADIUS_SESSION_SECRET) must be set")
        import hashlib

        return hashlib.sha256(b"exradius-session:" + self.secret_key.encode()).hexdigest()


def _download_key(secret_key: str) -> bytes:
    import hashlib

    return hashlib.sha256(b"exradius-download:" + secret_key.encode()).digest()


@lru_cache
def get_settings() -> Settings:
    return Settings()
