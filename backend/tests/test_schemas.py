import base64
import json

import pytest
from pydantic import ValidationError

from exradius.schemas.messages import (
    AnswerMessage,
    EventEnvelope,
    ResultPayload,
    TaskMessage,
    status_payload,
)
from exradius.states import State

TASK = {
    "version": 1,
    "task_id": "JIRA-4821",
    "project": "billing-platform",
    "agent": "bug-fixer",
    "instructions": "Find the cause and fix it.",
    "context": {"any": "json"},
    "attachments": [
        {"name": "screenshot.png", "content_type": "image/png", "url": "https://example.com/s.png"},
        {"name": "stack.txt", "content_type": "text/plain", "content_base64": base64.b64encode(b"trace").decode()},
    ],
    "correlation": {"opaque": "echo"},
}


def test_task_message_from_spec_example():
    message = TaskMessage.model_validate(TASK)
    assert message.task_id == "JIRA-4821"
    assert message.attachments[1].decoded() == b"trace"
    assert message.correlation == {"opaque": "echo"}


def test_unknown_fields_are_ignored():
    message = TaskMessage.model_validate({**TASK, "priority": "high"})
    assert not hasattr(message, "priority")


@pytest.mark.parametrize("version", [0, 2, "1", None])
def test_version_must_be_1(version):
    with pytest.raises(ValidationError):
        TaskMessage.model_validate({**TASK, "version": version})


def test_version_defaults_to_1_when_building_messages():
    assert TaskMessage(task_id="t", project="p", agent="a", instructions="i").version == 1


@pytest.mark.parametrize("field", ["task_id", "project", "instructions"])
def test_required_fields(field):
    data = dict(TASK)
    data.pop(field)
    with pytest.raises(ValidationError):
        TaskMessage.model_validate(data)


@pytest.mark.parametrize(
    "attachment",
    [
        {"name": "a.txt"},  # no source
        {"name": "a.txt", "url": "https://x", "content_base64": "aGk="},  # both sources
        {"name": "a.txt", "content_base64": "not base64!"},
        {"name": "../etc/passwd", "content_base64": "aGk="},
        {"name": "dir/a.txt", "content_base64": "aGk="},
        {"name": "a.txt", "url": "file:///etc/passwd"},
    ],
)
def test_invalid_attachments(attachment):
    with pytest.raises(ValidationError):
        TaskMessage.model_validate({**TASK, "attachments": [attachment]})


def test_attachment_names_unique():
    att = {"name": "a.txt", "content_base64": "aGk="}
    with pytest.raises(ValidationError):
        TaskMessage.model_validate({**TASK, "attachments": [att, att]})


def test_answer_message():
    answer = AnswerMessage.model_validate(
        {"version": 1, "task_id": "JIRA-4821", "request_id": "a7c1", "answer": "Yes", "extra": 1}
    )
    assert answer.attachments == []
    with pytest.raises(ValidationError):
        AnswerMessage.model_validate({"version": 1, "task_id": "x", "request_id": "y"})


def test_status_envelope_serialisation():
    envelope = EventEnvelope(
        task_id="JIRA-4821",
        run_id="r1",
        project="billing-platform",
        agent="bug-fixer",
        correlation={"opaque": "echo"},
        type="status",
        payload=status_payload(State.RUNNING, State.QUEUED),
    )
    data = json.loads(envelope.to_json())
    assert data["version"] == 1
    assert data["payload"] == {"state": "running", "previous": "queued", "reason": None}
    assert data["timestamp"].endswith("Z") and "+" not in data["timestamp"]
    assert envelope.routing_key == "task.status"


@pytest.mark.parametrize(
    ("type_", "payload", "key"),
    [
        ("info_request", {"request_id": "a7c1", "question": "Did a migration run?", "context": None}, "task.info_request"),
        ("result", ResultPayload(state="completed", summary="done").model_dump(), "task.result"),
    ],
)
def test_routing_keys(type_, payload, key):
    assert EventEnvelope(task_id="t", type=type_, payload=payload).routing_key == key


def test_envelope_payload_must_match_type():
    with pytest.raises(ValidationError):
        EventEnvelope(task_id="t", type="result", payload={"state": "running"})
    with pytest.raises(ValidationError):
        EventEnvelope(task_id="t", type="info_request", payload={"question": "no id"})


def test_result_payload_defaults():
    payload = ResultPayload(state="failed", error="boom").model_dump()
    assert payload == {
        "state": "failed",
        "summary": None,
        "output": None,
        "artifacts": [],
        "transcript_url": None,
        "error": "boom",
        "warnings": [],
    }


@pytest.mark.parametrize("url", ["s3://exradius/runs/r/transcript.jsonl", "file:///tmp/x", "runs/r/x"])
def test_result_urls_must_be_http(url):
    with pytest.raises(ValidationError):
        ResultPayload(state="completed", transcript_url=url)
    with pytest.raises(ValidationError):
        ResultPayload(state="completed", artifacts=[{"name": "a", "content_type": "t", "url": url}])
    ok = ResultPayload(state="completed", transcript_url="https://exradius.example.com/api/runs/r/transcript?sig=x")
    assert ok.transcript_url.startswith("https://")


@pytest.mark.parametrize("agent", [None, "absent"])
def test_agent_is_optional(agent):
    """A task without an agent is routed (docs/agent-routing.md §2)."""
    data = dict(TASK)
    if agent is None:
        data["agent"] = None
    else:
        data.pop("agent")
    assert TaskMessage.model_validate(data).agent is None
    with pytest.raises(ValidationError):
        TaskMessage.model_validate({**TASK, "agent": ""})


def test_routing_on_the_status_payload():
    named = status_payload(State.QUEUED, None, routing={"method": "named"})
    assert named["routing"] == {"method": "named"}
    decided = status_payload(State.QUEUED, None, routing={
        "method": "decided", "confidence": 0.48, "guess": True, "probabilities": {"a": 0.6, "b": 0.4}})
    assert decided["routing"] == {"method": "decided", "confidence": 0.48, "guess": True,
                                  "probabilities": {"a": 0.6, "b": 0.4}}
    assert "routing" not in status_payload(State.RUNNING, State.PREPARING)
