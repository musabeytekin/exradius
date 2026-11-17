"""The built-in ``exradius`` MCP server (architecture §5.3), hosted in-process by the runner."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server

from exradius.runner.artifact_files import ArtifactError, read_artifact
from exradius.runner.client import RunApi

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
        "artifact_ids": {
            "type": "array",
            "items": {"type": "string"},
            "description": "IDs returned by artifact_upload or artifact_list to attach to the result. Omit for no files.",
        },
    },
    "required": ["output"],
}


def _text(text: str, is_error: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["is_error"] = True
    return result


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

    async def artifact_upload(self, args: dict[str, Any]) -> dict[str, Any]:
        path = args.get("path")
        if not isinstance(path, str) or not path.strip():
            return _text("'path' must be a non-empty file path.", is_error=True)
        try:
            name, content = await asyncio.to_thread(read_artifact, self.working_dir, path)
            saved = await self.api.upload_artifact(name, content)
        except (ArtifactError, RuntimeError) as exc:
            return _text(str(exc), is_error=True)
        return _text(saved.model_dump_json())

    async def artifact_list(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            saved = await self.api.list_artifacts()
        except RuntimeError as exc:
            return _text(str(exc), is_error=True)
        return _text(json.dumps([item.model_dump(mode="json") for item in saved]))

    async def artifact_get(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            artifact_id = str(uuid.UUID(str(args.get("artifact_id"))))
        except ValueError:
            return _text("'artifact_id' must be an artifact UUID.", is_error=True)
        try:
            saved = await self.api.get_artifact(artifact_id)
        except RuntimeError as exc:
            return _text(str(exc), is_error=True)
        return _text(saved.model_dump_json())

    async def submit_result(self, args: dict[str, Any]) -> dict[str, Any]:
        if "artifacts" in args:
            return _text("Upload files with artifact_upload, then pass their IDs as 'artifact_ids'.", is_error=True)
        ids = args.get("artifact_ids", [])
        if not isinstance(ids, list) or len(ids) > 100:
            return _text("'artifact_ids' must be a list of at most 100 artifact UUIDs.", is_error=True)
        try:
            ids = [str(uuid.UUID(str(value))) for value in ids]
        except ValueError:
            return _text("'artifact_ids' must contain artifact UUIDs, not file paths.", is_error=True)
        try:
            await self.api.submit_result(args.get("output"), ids)
        except RuntimeError as exc:
            return _text(str(exc), is_error=True)
        return _text(f"Result stored with {len(set(ids))} artifact(s).")

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
                name="artifact_upload",
                description=(f"Persist a file of any type from {self.working_dir}. Paths are absolute inside "
                             f"{self.working_dir} or relative to it, e.g. repo/report.pdf. "
                             "Returns an artifact ID after storage succeeds. Max 50 MiB; no symlinks. "
                             "Does not submit a result. Retrying the same name and bytes returns the same ID."),
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                handler=self.artifact_upload,
            ),
            SdkMcpTool(
                name="artifact_list", description="List files already stored for this run, including unselected files.",
                input_schema={"type": "object", "properties": {}}, handler=self.artifact_list,
            ),
            SdkMcpTool(
                name="artifact_get", description="Get a stored file's metadata in this run by its artifact ID.",
                input_schema={"type": "object", "properties": {"artifact_id": {"type": "string"}},
                              "required": ["artifact_id"]}, handler=self.artifact_get,
            ),
            SdkMcpTool(
                name="submit_result",
                description=(
                    "Submit the result, optionally selecting previously uploaded artifact IDs. "
                    "The last call replaces the result and selection, but never deletes stored files. "
                    "A result without files is allowed, including after an upload failed."
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
