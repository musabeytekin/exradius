import hashlib
import json
import uuid
from typing import Any

import pytest

from exradius.runner.prompt import OPERATING_RULES, build_resume_message, build_system_prompt, build_task_message
from exradius.runner.tools import SUSPEND_MESSAGE, ExradiusTools
from exradius.schemas.internal import (
    StoredArtifact,
    AttachmentPath,
    DeliveredAnswer,
    OutcomeReport,
    RunSpecResponse,
    TranscriptEvent,
)


class FakeApi:
    def __init__(self):
        self.info_requests: list[tuple[str, str | None]] = []
        self.results: list[tuple[Any, list[str]]] = []
        self.uploads = {}

    async def get_spec(self) -> RunSpecResponse:  # pragma: no cover - unused here
        raise NotImplementedError

    async def report_session(self, session_id: str) -> None:
        pass

    async def append_transcript(self, events: list[TranscriptEvent]) -> None:
        pass

    async def create_info_request(self, question: str, context: str | None) -> str:
        self.info_requests.append((question, context))
        return f"req-{len(self.info_requests)}"

    async def submit_result(self, output: Any, artifacts: list[str]) -> None:
        self.results.append((output, artifacts))

    async def upload_artifact(self, name, content):
        identifier = uuid.uuid4()
        saved = StoredArtifact(id=identifier, name=name, content_type="application/octet-stream",
                               size=len(content), sha256=hashlib.sha256(content).hexdigest(), selected=False)
        self.uploads[str(identifier)] = (saved, content)
        return saved

    async def list_artifacts(self):
        return [item[0] for item in self.uploads.values()]

    async def get_artifact(self, artifact_id):
        return self.uploads[artifact_id][0]

    async def report_outcome(self, outcome: OutcomeReport) -> None:
        pass

    async def report(self, status: str) -> None:
        pass

    async def wait_answers(self, wait: float):  # pragma: no cover - unused here
        raise NotImplementedError


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


async def test_upload_then_select_stored_file(tools, api, tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "report.md").write_text("# Report\n")
    result = await tools.artifact_upload({"path": "reports/report.md"})
    saved = json.loads(text_of(result))
    assert api.results == []
    assert api.uploads[saved["id"]][1] == b"# Report\n"
    assert json.loads(text_of(await tools.artifact_list({})))[0]["id"] == saved["id"]
    assert json.loads(text_of(await tools.artifact_get({"artifact_id": saved["id"]})))["size"] == 9
    result = await tools.submit_result({"output": {"fixed": True}, "artifact_ids": [saved["id"]]})
    assert "1 artifact" in text_of(result)
    assert api.results == [({"fixed": True}, [saved["id"]])]
    assert not tools.suspend_requested


async def test_submit_result_can_be_called_again(tools, api):
    await tools.submit_result({"output": 1})
    await tools.submit_result({"output": 2})
    assert [r[0] for r in api.results] == [1, 2]  # the API keeps the last one


@pytest.mark.parametrize("path", ["../outside.txt", "/etc/hosts", "missing.txt"])
async def test_upload_rejects_bad_paths(tools, api, tmp_path, path):
    (tmp_path.parent / "outside.txt").write_text("secret")
    result = await tools.artifact_upload({"path": path})
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
        assert sorted(t.name for t in listed.tools) == ["artifact_get", "artifact_list", "artifact_upload", "request_information", "submit_result"]
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


def test_task_message_is_only_the_ticket():
    message = build_task_message(
        spec(
            context={"customer": "ACME", "since": "Monday"},
            attachments=[AttachmentPath(path=".exradius/attachments/screenshot.png", content_type="image/png")],
        )
    )
    assert message == (
        "# Task t\nFix the negative totals.\n\n"
        '# Context\n{\n  "customer": "ACME",\n  "since": "Monday"\n}\n\n'
        "# Attachments\n- .exradius/attachments/screenshot.png (image/png)"
    )
    assert "careful engineer" not in message and "submit_result" not in message


def test_task_message_omits_empty_sections_and_internals():
    message = build_task_message(spec())
    assert message == "# Task t\nFix the negative totals."
    assert "workspace" not in message.lower() and "exradius" not in message.lower()


def test_system_prompt_is_rules_then_role():
    assert build_system_prompt(spec()) == OPERATING_RULES + "\n\nYou are a careful engineer."
    assert build_system_prompt(spec(agent_prompt="  ")) == OPERATING_RULES
    assert OPERATING_RULES.startswith("You are working unattended on a task")
    assert "request_information" in OPERATING_RULES and "submit_result" in OPERATING_RULES
    # The run directory is thrown away: work must leave as a pushed branch or attached files.
    assert "discarded afterwards" in OPERATING_RULES
    assert "whose name starts with the task id" in OPERATING_RULES
    assert "continue on an existing branch (a pull request under review)" in OPERATING_RULES
    assert "attach a patch of your commits" in OPERATING_RULES
    assert "Refer to files by their path inside the repository" in OPERATING_RULES
    assert OPERATING_RULES.endswith("never by the directory you run in.")
    assert "exradius" not in OPERATING_RULES.lower()


def test_rules_match_the_architecture_document():
    import re
    from pathlib import Path

    doc = (Path(__file__).parents[2] / "docs" / "architecture.md").read_text()
    section = doc[doc.index("## 13. Operating rules"):]
    assert re.search(r"```\n(.*?)\n```", section, re.S).group(1) == OPERATING_RULES


def test_claude_code_options_append_the_system_prompt(tmp_path):
    from exradius.runtimes import RunSpec
    from exradius.runtimes.claude_code import ClaudeCodeRuntime

    run_spec = RunSpec(run_id="r", working_dir=tmp_path, state_dir=tmp_path, prompt=build_task_message(spec()),
                       connections=[], env={}, system_prompt=build_system_prompt(spec()))
    options = ClaudeCodeRuntime().options(run_spec)
    assert options.system_prompt == {
        "type": "preset", "preset": "claude_code", "append": OPERATING_RULES + "\n\nYou are a careful engineer.",
    }
    resumed = ClaudeCodeRuntime().options(run_spec, resume="sess-1")
    assert resumed.system_prompt == options.system_prompt and resumed.resume == "sess-1"


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


@pytest.mark.parametrize("mode", ["start", "resume"])
async def test_runner_starts_sessions_with_rules_in_the_system_prompt(api, tmp_path, monkeypatch, mode):
    """What the runner hands the runtime: the ticket as the message, rules and role as the
    system prompt; a resume sends the answer message and keeps the same system prompt."""
    from exradius.runner import main as runner_main
    from exradius.runtimes import RunOutcome

    seen = {}

    class FakeRuntime:
        async def start(self, run_spec, events):
            seen.update(message=run_spec.prompt, system=run_spec.system_prompt)
            return RunOutcome("completed", "s1", "done", None)

        async def resume(self, run_spec, session_id, message, events):
            seen.update(message=message, system=run_spec.system_prompt, session=session_id)
            return RunOutcome("completed", session_id, "done", None)

    the_spec = spec(mode=mode, session_id="s1" if mode == "resume" else None,
                    context={"customer": "ACME"},
                    answers=[DeliveredAnswer(request_id="q1", question="Which region?", answer="eu", attachments=[])]
                    if mode == "resume" else [])

    async def get_spec():
        return the_spec

    api.get_spec = get_spec
    monkeypatch.setattr(runner_main, "get_runtime", lambda name, runtime_dir=None: FakeRuntime())
    outcome = await runner_main.execute(api, "r", tmp_path, tmp_path)
    assert outcome.status == "completed"
    assert seen["system"] == OPERATING_RULES + "\n\nYou are a careful engineer."
    if mode == "start":
        assert seen["message"] == '# Task t\nFix the negative totals.\n\n# Context\n{\n  "customer": "ACME"\n}'
    else:
        assert seen["message"] == build_resume_message(the_spec.answers) and seen["session"] == "s1"
    assert "careful engineer" not in seen["message"] and "unattended" not in seen["message"]


async def test_upload_failure_allows_result_without_files(tools, api, tmp_path):
    error = await tools.artifact_upload({"path": "report.pdf"})
    assert error["is_error"]
    assert str(tmp_path / "report.pdf") in text_of(error)
    assert "relative to" in text_of(error)
    assert not (await tools.submit_result({"output": "Upload failed."})).get("is_error")
    assert api.results == [("Upload failed.", [])]


async def test_upload_rejects_symlinks_and_non_regular_files(tools, api, tmp_path):
    import os

    (tmp_path / "report").write_text("safe")
    (tmp_path / "link").symlink_to(tmp_path / "report")
    os.mkfifo(tmp_path / "pipe")
    for name in ("link", "pipe", "."):
        assert (await tools.artifact_upload({"path": name}))["is_error"]
    assert api.uploads == {}


async def test_upload_honors_size_limit_before_reading(tools, tmp_path, monkeypatch):
    from exradius.runner import artifact_files

    monkeypatch.setattr(artifact_files, "MAX_FILE_BYTES", 3)
    (tmp_path / "report").write_bytes(b"1234")
    assert (await tools.artifact_upload({"path": "report"}))["is_error"]


async def test_old_path_parameter_is_not_silently_ignored(tools, api):
    assert (await tools.submit_result({"output": "done", "artifacts": ["report.pdf"]}))["is_error"]
    assert api.results == []
