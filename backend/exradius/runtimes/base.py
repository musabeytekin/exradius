"""The pluggable runtime interface (architecture §7)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol


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
    connections: list[ConnectionSpec]  # includes the built-in exradius server
    env: dict[str, str]


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


class Runtime(Protocol):
    name: str

    async def start(self, spec: RunSpec, events: EventSink) -> RunOutcome: ...

    async def resume(
        self, spec: RunSpec, session_id: str, message: str, events: EventSink
    ) -> RunOutcome: ...
