import base64
from typing import Any

import pytest

from exradius.runner.prompt import build_prompt, build_resume_message
from exradius.runner.tools import SUSPEND_MESSAGE, ExradiusTools
from exradius.schemas.internal import (
    ArtifactUpload,
    AttachmentPath,
    DeliveredAnswer,
    OutcomeReport,
    RunSpecResponse,
    TranscriptEvent,
)


class FakeApi:
    def __init__(self):
        self.info_requests: list[tuple[str, str | None]] = []
        self.results: list[tuple[Any, list[ArtifactUpload]]] = []

    async def get_spec(self) -> RunSpecResponse:  # pragma: no cover - unused here
        raise NotImplementedError

    async def report_session(self, session_id: str) -> None:
        pass

    async def append_transcript(self, events: list[TranscriptEvent]) -> None:
        pass

    async def create_info_request(self, question: str, context: str | None) -> str:
        self.info_requests.append((question, context))
        return f"req-{len(self.info_requests)}"

    async def submit_result(self, output: Any, artifacts: list[ArtifactUpload]) -> None:
        self.results.append((output, artifacts))

    async def report_outcome(self, outcome: OutcomeReport) -> None:
        pass


@pytest.fixture
def api():
    return FakeApi()


@pytest.fixture
def tools(api, tmp_path):
    return ExradiusTools(api, tmp_path)


def text_of(result: dict) -> str:
    return result["content"][0]["text"]


async def test_request_information_records_and_suspends(tools, api):
    assert not tools.suspend_requested
    result = await tools.request_information({"question": "Did a migration run on Monday?", "context": "logs"})
    assert text_of(result) == SUSPEND_MESSAGE
    assert SUSPEND_MESSAGE == "Your question was sent. Stop working now; you will be resumed with the answer."
    assert api.info_requests == [("Did a migration run on Monday?", "logs")]
    assert tools.suspend_requested


async def test_request_information_context_is_optional(tools, api):
    await tools.request_information({"question": "Why?"})
    assert api.info_requests == [("Why?", None)]


async def test_request_information_requires_question(tools, api):
    result = await tools.request_information({"question": "  "})
    assert result.get("is_error") is True
    assert api.info_requests == [] and not tools.suspend_requested


async def test_submit_result_uploads_files(tools, api, tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "report.md").write_text("# Report\n")
    result = await tools.submit_result({"output": {"fixed": True}, "artifacts": ["reports/report.md"]})
    assert "1 artifact" in text_of(result)
    output, artifacts = api.results[-1]
    assert output == {"fixed": True}
    assert artifacts[0].name == "report.md"
    assert artifacts[0].content_type == "text/markdown"
    assert base64.b64decode(artifacts[0].content_base64) == b"# Report\n"
    assert not tools.suspend_requested


async def test_submit_result_can_be_called_again(tools, api):
    await tools.submit_result({"output": 1})
    await tools.submit_result({"output": 2})
    assert [r[0] for r in api.results] == [1, 2]  # the API keeps the last one


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/hosts", "missing.txt"])
async def test_submit_result_rejects_bad_paths(tools, api, tmp_path, path):
    (tmp_path.parent / "outside.txt").write_text("secret")
    result = await tools.submit_result({"output": None, "artifacts": [path]})
    assert result.get("is_error") is True
    assert api.results == []


async def test_tools_over_mcp(tools, api):
    """The in-process server exposes both tools and dispatches calls to them."""
    from mcp import Client

    server = tools.server()
    assert server["type"] == "sdk" and server["name"] == "exradius"
    assert server["alwaysLoad"] is True  # never deferred behind tool search
    async with Client(server["instance"]) as client:
        listed = await client.list_tools()
        assert sorted(t.name for t in listed.tools) == ["request_information", "submit_result"]
        schema = next(t for t in listed.tools if t.name == "request_information").input_schema
        assert schema["required"] == ["question"]

        result = await client.call_tool("request_information", {"question": "Which region?"})
        assert result.content[0].text == SUSPEND_MESSAGE
        await client.call_tool("submit_result", {"output": {"ok": True}})
    assert api.info_requests == [("Which region?", None)]
    assert api.results == [({"ok": True}, [])]
    assert tools.suspend_requested


def spec(**kwargs) -> RunSpecResponse:
    base = dict(
        run_id="r", task_id="t", runtime="claude_code", mode="start",
        agent_prompt="You are a careful engineer.", instructions="Fix the negative totals.",
    )
    return RunSpecResponse(**{**base, **kwargs})


def test_prompt_layout():
    prompt = build_prompt(
        spec(
            context={"customer": "ACME", "since": "Monday"},
            attachments=[AttachmentPath(path=".exradius/attachments/screenshot.png", content_type="image/png")],
        )
    )
    assert prompt == (
        "You are a careful engineer.\n\n"
        "# Task\nFix the negative totals.\n\n"
        '# Context\n{\n  "customer": "ACME",\n  "since": "Monday"\n}\n\n'
        "# Attachments\n- .exradius/attachments/screenshot.png (image/png)"
    )


def test_prompt_omits_empty_sections_and_internals():
    prompt = build_prompt(spec())
    assert prompt == "You are a careful engineer.\n\n# Task\nFix the negative totals."
    assert "workspace" not in prompt.lower()


def test_resume_message():
    message = build_resume_message(
        [
            DeliveredAnswer(
                request_id="a7c1",
                question="Did a migration run?",
                answer="Yes, Monday 03:00 UTC.",
                attachments=[AttachmentPath(path=".exradius/attachments/a7c1/log.txt", content_type="text/plain")],
            )
        ]
    )
    assert "Did a migration run?" in message
    assert "Yes, Monday 03:00 UTC." in message
    assert ".exradius/attachments/a7c1/log.txt (text/plain)" in message
