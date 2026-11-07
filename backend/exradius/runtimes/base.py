"""The pluggable runtime interface (architecture §7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, Protocol


@dataclass
class ConnectionSpec:
    """One MCP server available to the agent.

    ``transport`` is ``stdio`` or ``http`` for configured connections and ``sdk`` for
    servers hosted in-process by the runner (the built-in ``exradius`` server), in which
    case ``server`` holds the runtime-specific server object.
    """

    name: str
    transport: Literal["stdio", "http", "sdk"]
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    server: Any = None


@dataclass
class RunSpec:
    run_id: str
    working_dir: Path
    state_dir: Path
    prompt: str
    """The first user message: the ticket (task, context, attachments)."""
    connections: list[ConnectionSpec]  # includes the built-in exradius server
    env: dict[str, str]
    system_prompt: str = ""
    """Appended to the runtime's own system prompt: the operating rules, the environment
    section and the agent's role (architecture §13). Applies to resumed sessions too."""
    model: str | None = None
    """The agent's model; ``None`` leaves the runtime's default."""


@dataclass
class RunOutcome:
    status: Literal["completed", "waiting_for_input", "failed"]
    session_id: str | None
    final_message: str | None
    error: str | None


class EventSink(Protocol):
    """How a runtime reports back to the harness while it runs."""

    async def event(self, payload: dict[str, Any]) -> None:
        """Record one event of the runtime's stream in the transcript."""
        ...

    async def session_started(self, session_id: str) -> None:
        """Remember the runtime session id so the run can be resumed later."""
        ...

    def suspend_requested(self) -> bool:
        """True once the agent asked for information and the session must stop."""
        ...


# Providers (docs/providers.md §2.1) -------------------------------------------------------


@dataclass(frozen=True)
class SettingField:
    """A non-secret setting of an auth method (region, project, base URL ...)."""

    name: str
    label: str
    required: bool = True


@dataclass(frozen=True)
class AuthMethod:
    name: str
    label: str
    credential_label: str
    settings: tuple[SettingField, ...] = ()
    notice: str | None = None


@dataclass(frozen=True)
class ProviderKind:
    kind: str
    label: str
    auth_methods: tuple[AuthMethod, ...]

    def auth_method(self, name: str) -> AuthMethod | None:
        return next((m for m in self.auth_methods if m.name == name), None)


@dataclass(frozen=True)
class RuntimeCapabilities:
    providers: tuple[ProviderKind, ...]

    def provider_kind(self, kind: str) -> ProviderKind | None:
        return next((p for p in self.providers if p.kind == kind), None)

    def accepts(self, kind: str, auth_method: str) -> bool:
        provider = self.provider_kind(kind)
        return provider is not None and provider.auth_method(auth_method) is not None


@dataclass
class ProviderCredential:
    """A provider with its credential decrypted, at container start only."""

    kind: str
    auth_method: str
    credential: str
    settings: dict[str, Any] = field(default_factory=dict)


PROBE_PREFIX = "EXRADIUS_PROBE "
"""The provider probe prints its result as one line ``EXRADIUS_PROBE {"ok": ..., ...}``."""


@dataclass
class ProbeResult:
    ok: bool
    detail: str | None = None
    error: str | None = None


class Runtime(Protocol):
    name: str
    label: ClassVar[str]
    capabilities: ClassVar[RuntimeCapabilities]
    """The provider kinds and auth methods this runtime can inject."""
    credential_variables: ClassVar[frozenset[str]]
    """Every environment variable any provider mapping may set; removed from the project
    environment so a run gets only its provider's credential."""

    @classmethod
    def provider_environment(cls, provider: ProviderCredential) -> dict[str, str]:
        """The container environment for ``provider`` (providers.md §2.2)."""
        ...

    def __init__(self, runtime_dir: Path | None = None) -> None:
        """``runtime_dir``: the runtime bundle the runner runs from (``None`` outside one)."""

    async def probe(self, env: dict[str, str], model: str | None = None) -> ProbeResult:
        """A minimal session ("Reply with OK") with ``env``, which carries the provider,
        on ``model`` when given."""
        ...

    async def start(self, spec: RunSpec, events: EventSink) -> RunOutcome: ...

    async def resume(
        self, spec: RunSpec, session_id: str, message: str, events: EventSink
    ) -> RunOutcome: ...
