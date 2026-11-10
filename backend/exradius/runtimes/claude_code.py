"""Claude Code runtime on the Claude Agent SDK for Python.

The SDK is imported only where a session runs (inside the runtime bundle), so the api and
worker, whose image has no SDK, can still read the capability declaration and the
provider mapping.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from exradius.runtimes.base import (
    AuthMethod,
    ConnectionSpec,
    EventSink,
    ProbeResult,
    ProviderCredential,
    ProviderKind,
    RunOutcome,
    RunSpec,
    RuntimeCapabilities,
)

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions

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


def bundled_cli(runtime_dir: Path | None) -> str | None:
    """``bin/claude`` of the runtime bundle. ``None`` outside a bundle, where the SDK finds
    its own CLI."""
    if runtime_dir is None:
        return None
    cli = runtime_dir / "bin" / "claude"
    if not cli.is_file():
        raise RuntimeError(f"the runtime bundle has no Claude Code binary at {cli}")
    return str(cli)


SETTINGS = {"includeCoAuthoredBy": False, "attribution": {"commit": "", "pr": ""}}
"""Claude Code settings Exradius needs in every session: commits carry the agent's identity
and the Exradius trailers, not Claude Code's co-author line or pull request footer.

Passed as ``--settings`` (the SDK's ``settings`` option), which ranks above the
repository's ``.claude/settings.json`` and ``settings.local.json`` and the user settings in
``CLAUDE_CONFIG_DIR``, key by key, so a repository cannot turn them back on. Only managed
settings (``/etc/claude-code/``, which a project's image could bake) rank higher
(docs/architecture.md §12). All setting sources still load, so the repository's
``CLAUDE.md``, ``AGENTS.md`` and ``.claude/`` commands, agents and skills are there."""


def settings_argument() -> str:
    return json.dumps(SETTINGS, separators=(",", ":"))


PLACEHOLDER = "placeholder"
"""The provider variable's value in a pod: the egress proxy sends the real credential."""

SESSION_VARIABLES = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
"""Claude Code's telemetry, error reporting and update checks go to Anthropic's hosts
directly, not through ``ANTHROPIC_BASE_URL``; the agent phase would refuse each one (and
the task page list it), so a session makes none."""

OAUTH_NOTICE = (
    "Anthropic restricts subscription tokens to personal use of Claude Code; products built "
    "on the Agent SDK should use an API key."
)


class ClaudeCodeRuntime:
    """Sessions start from the environment the runner hands over (``RunSpec.env``: the
    container's, see ``exradius.runner.environment``) plus only ``CLAUDE_CONFIG_DIR``. The
    bundle directory is never put on ``PATH``; the CLI is started by its path."""

    name = "claude_code"
    label = "Claude Code"
    capabilities = RuntimeCapabilities(
        providers=(
            ProviderKind(
                kind="anthropic",
                label="Anthropic",
                auth_methods=(
                    AuthMethod(name="api_key", label="API key", credential_label="API key"),
                    AuthMethod(
                        name="oauth_token",
                        label="Claude subscription token (claude setup-token)",
                        credential_label="OAuth token",
                        notice=OAUTH_NOTICE,
                    ),
                ),
            ),
        )
    )
    credential_variables = frozenset(
        {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_BASE_URL"}
    )
    upstream = "https://api.anthropic.com"

    @classmethod
    def provider_environment(cls, kind: str, auth_method: str, model_url: str) -> dict[str, str]:
        """The workstation's provider variables: Claude Code talks to the egress proxy's
        model route (``model_url``) with a placeholder, which the proxy replaces with the
        credential (docs/kubernetes.md §10). No secret."""
        match (kind, auth_method):
            case ("anthropic", "api_key"):
                return {"ANTHROPIC_BASE_URL": model_url, "ANTHROPIC_API_KEY": PLACEHOLDER}
            case ("anthropic", "oauth_token"):
                return {"ANTHROPIC_BASE_URL": model_url, "CLAUDE_CODE_OAUTH_TOKEN": PLACEHOLDER}
        raise ValueError(f"Claude Code cannot use a {kind}/{auth_method} provider")

    @classmethod
    def provider_route(cls, provider: ProviderCredential) -> tuple[str, dict[str, str]]:
        """Where the proxy's model route goes, and the header it sets there: the key as
        ``x-api-key``, a subscription token as a bearer token (what Claude Code sends
        itself with each)."""
        match (provider.kind, provider.auth_method):
            case ("anthropic", "api_key"):
                return cls.upstream, {"x-api-key": provider.credential}
            case ("anthropic", "oauth_token"):
                return cls.upstream, {"Authorization": f"Bearer {provider.credential}"}
        raise ValueError(f"Claude Code cannot use a {provider.kind}/{provider.auth_method} provider")

    def __init__(self, runtime_dir: Path | None = None):
        self.cli_path = bundled_cli(runtime_dir)

    @staticmethod
    def session_environment(env: dict[str, str], config_dir: Path | str) -> dict[str, str]:
        """What the CLI, and so the agent's shell, gets on top of the runner's cleaned
        process environment: the container's environment plus ``CLAUDE_CONFIG_DIR`` and
        ``SESSION_VARIABLES``."""
        return {**env, **SESSION_VARIABLES, "CLAUDE_CONFIG_DIR": str(config_dir)}

    async def probe(self, env: dict[str, str], model: str | None = None) -> ProbeResult:
        """"Reply with OK" in a session without tools, with ``env`` (the container's
        environment, which carries the provider credential), on ``model`` when given."""
        from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, ResultMessage

        with tempfile.TemporaryDirectory() as cwd:
            options = ClaudeAgentOptions(
                cwd=cwd,
                tools=[],
                strict_mcp_config=True,
                setting_sources=[],
                max_turns=1,
                model=model,
                cli_path=self.cli_path,
                env=self.session_environment(env, f"{cwd}/.claude"),
                stderr=lambda line: print(line, file=sys.stderr, flush=True),
            )
            model = reply = None
            result: ResultMessage | None = None
            async with ClaudeSDKClient(options) as client:
                await client.query("Reply with OK")
                async for message in client.receive_response():
                    if isinstance(message, AssistantMessage):
                        model = model or message.model
                        if message.error:
                            return ProbeResult(False, error=f"Claude Code reported {message.error}")
                    if isinstance(message, ResultMessage):
                        result = message
        if result is None:
            return ProbeResult(False, error="session ended without a result")
        if result.is_error:
            detail = "; ".join(result.errors or []) or result.result or result.subtype
            return ProbeResult(False, error=f"Claude Code session failed: {detail}")
        reply = (result.result or "").strip()
        return ProbeResult(True, detail=f"{model or 'model'} replied {reply[:100]!r}")

    def options(self, spec: RunSpec, resume: str | None = None) -> ClaudeAgentOptions:
        from claude_agent_sdk import ClaudeAgentOptions

        return ClaudeAgentOptions(
            cli_path=self.cli_path,
            model=spec.model or None,
            cwd=spec.working_dir,
            # Claude Code's own system prompt, then the operating rules and the role (§13).
            system_prompt={"type": "preset", "preset": "claude_code", **({"append": spec.system_prompt} if spec.system_prompt else {})},
            mcp_servers={c.name: mcp_server_config(c) for c in spec.connections},
            strict_mcp_config=True,
            # Capability is tool availability: the agent may use everything it is given.
            permission_mode="bypassPermissions",
            resume=resume,
            settings=settings_argument(),
            # Session files live under the state mount so they survive suspend/resume.
            env=self.session_environment(spec.env, self.config_dir(spec)),
            stderr=lambda line: print(line, file=sys.stderr, flush=True),
        )

    @staticmethod
    def config_dir(spec: RunSpec) -> Path:
        return spec.state_dir / "claude"

    async def start(self, spec: RunSpec, events: EventSink) -> RunOutcome:
        return await self._session(spec, spec.prompt, events, resume=None)

    async def resume(
        self, spec: RunSpec, session_id: str, message: str, events: EventSink
    ) -> RunOutcome:
        return await self._session(spec, message, events, resume=session_id)

    async def _session(
        self, spec: RunSpec, prompt: str, events: EventSink, resume: str | None
    ) -> RunOutcome:
        from claude_agent_sdk import ClaudeSDKClient, ResultMessage, SystemMessage

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
