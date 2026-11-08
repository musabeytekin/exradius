"""The built-in ``exradius`` MCP server (architecture §5.3), hosted in-process by the runner."""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server

from exradius.runner.client import RunApi
from exradius.schemas.internal import ArtifactUpload

SERVER_NAME = "exradius"
SUSPEND_MESSAGE = (
    "Your question was sent. Stop working now; you will be resumed with the answer."
)

REQUEST_INFORMATION_SCHEMA = {
    "type": "object",
    "properties": {
        "question": {"type": "string", "description": "The question to ask."},
        "context": {
            "type": "string",
            "description": "Optional background that helps answer the question.",
        },
    },
    "required": ["question"],
}

SUBMIT_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "output": {"description": "Structured result of the task, any JSON value."},
        "artifacts": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Paths of files in the working directory to attach to the result.",
        },
    },
    "required": ["output"],
}


def _text(text: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["is_error"] = True
    return result


class ArtifactError(ValueError):
    pass


class ExradiusTools:
    def __init__(self, api: RunApi, working_dir: Path):
        self.api = api
        self.working_dir = working_dir.resolve()
        self._suspend = False

    @property
    def suspend_requested(self) -> bool:
        return self._suspend

    def clear_suspend(self) -> None:
        """Before the session resumes with the answers, in the same process."""
        self._suspend = False

    async def request_information(self, args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question") or "").strip()
        if not question:
            return _text("A question is required.", is_error=True)
        context = args.get("context")
        await self.api.create_info_request(question, None if context is None else str(context))
        self._suspend = True
        return _text(SUSPEND_MESSAGE)

    def _artifact(self, path: str) -> ArtifactUpload:
        candidate = (self.working_dir / path).resolve()
        if not candidate.is_relative_to(self.working_dir):
            raise ArtifactError(f"{path}: artifacts must be inside the working directory")
        if not candidate.is_file():
            raise ArtifactError(f"{path}: no such file")
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        return ArtifactUpload(
            name=candidate.name,
            content_type=content_type,
            content_base64=base64.b64encode(candidate.read_bytes()).decode(),
        )

    async def submit_result(self, args: dict[str, Any]) -> dict[str, Any]:
        paths = args.get("artifacts") or []
        if not isinstance(paths, list):
            return _text("'artifacts' must be a list of file paths.", is_error=True)
        try:
            artifacts = [self._artifact(str(p)) for p in paths]
        except ArtifactError as exc:
            return _text(str(exc), is_error=True)
        await self.api.submit_result(args.get("output"), artifacts)
        return _text(f"Result stored with {len(artifacts)} artifact(s).")

    def tools(self) -> list[SdkMcpTool[Any]]:
        return [
            SdkMcpTool(
                name="request_information",
                description=(
                    "Ask the person who assigned this task a question when you cannot continue "
                    "without their answer. After calling it, stop working: you will be resumed "
                    "with the answer."
                ),
                input_schema=REQUEST_INFORMATION_SCHEMA,
                handler=self.request_information,
            ),
            SdkMcpTool(
                name="submit_result",
                description=(
                    "Submit the structured result of the task, optionally with files from the "
                    "working directory as artifacts. May be called more than once; the last "
                    "call wins."
                ),
                input_schema=SUBMIT_RESULT_SCHEMA,
                handler=self.submit_result,
            ),
        ]

    def server(self) -> Any:
        config = create_sdk_mcp_server(SERVER_NAME, tools=self.tools())
        # Keep these tools in the prompt instead of deferring them behind Claude Code's
        # tool search: the agent must always be able to ask and to submit.
        config["alwaysLoad"] = True  # type: ignore[typeddict-unknown-key]
        return config
