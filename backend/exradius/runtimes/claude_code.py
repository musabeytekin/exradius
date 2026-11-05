"""Claude Code runtime on the Claude Agent SDK for Python."""

from __future__ import annotations

import dataclasses
import logging
import sys
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
)

from exradius.runtimes.base import ConnectionSpec, EventSink, RunOutcome, RunSpec

log = logging.getLogger(__name__)


def to_jsonable(value: Any) -> Any:
    """Turn SDK message objects into plain JSON values for the transcript."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        data = {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
        return {"type": type(value).__name__, **data}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def mcp_server_config(conn: ConnectionSpec) -> Any:
    if conn.transport == "sdk":
        return conn.server
    if conn.transport == "stdio":
        config: dict[str, Any] = {"type": "stdio", "command": conn.command, "args": list(conn.args)}
        if conn.env:
            config["env"] = dict(conn.env)
        return config
    if conn.transport == "http":
        config = {"type": "http", "url": conn.url}
        if conn.headers:
            config["headers"] = dict(conn.headers)
        return config
    raise ValueError(f"unknown transport {conn.transport!r}")


class ClaudeCodeRuntime:
    name = "claude_code"

    def options(self, spec: RunSpec, resume: str | None = None) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            cwd=spec.working_dir,
            system_prompt={"type": "preset", "preset": "claude_code"},
            mcp_servers={c.name: mcp_server_config(c) for c in spec.connections},
            strict_mcp_config=True,
            # Capability is tool availability: the agent may use everything it is given.
            permission_mode="bypassPermissions",
            resume=resume,
            # Session files live under the state mount so they survive suspend/resume.
            env={**spec.env, "CLAUDE_CONFIG_DIR": str(spec.state_dir / "claude")},
            stderr=lambda line: print(line, file=sys.stderr, flush=True),
        )

    async def start(self, spec: RunSpec, events: EventSink) -> RunOutcome:
        return await self._session(spec, spec.prompt, events, resume=None)

    async def resume(
        self, spec: RunSpec, session_id: str, message: str, events: EventSink
    ) -> RunOutcome:
        return await self._session(spec, message, events, resume=session_id)

    async def _session(
        self, spec: RunSpec, prompt: str, events: EventSink, resume: str | None
    ) -> RunOutcome:
        session_id = resume
        result: ResultMessage | None = None
        interrupted = False
        async with ClaudeSDKClient(self.options(spec, resume)) as client:
            await client.query(prompt)
            async for message in client.receive_response():
                await events.event(to_jsonable(message))
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    new_id = message.data.get("session_id")
                    if new_id and new_id != session_id:
                        session_id = new_id
                        await events.session_started(new_id)
                if isinstance(message, ResultMessage):
                    result = message
                    if message.session_id and message.session_id != session_id:
                        session_id = message.session_id
                        await events.session_started(session_id)
                elif events.suspend_requested() and not interrupted:
                    # request_information was called: stop the session so the run can
                    # wait for the answer.
                    interrupted = True
                    await client.interrupt()

        final = result.result if result else None
        if events.suspend_requested():
            return RunOutcome("waiting_for_input", session_id, final, None)
        if result is None:
            return RunOutcome("failed", session_id, None, "session ended without a result")
        if result.is_error:
            detail = "; ".join(result.errors or []) or result.result or result.subtype
            return RunOutcome("failed", session_id, final, f"Claude Code session failed: {detail}")
        return RunOutcome("completed", session_id, final, None)
