import base64
import json

from sqlalchemy import select

from exradius.models import Event, InfoRequest, Project, Run, SecretStore, SecretStoreEntry, Task
from exradius.states import State
from exradius.worker.intake import accept_answer
from tests.conftest import BASE_CONFIG, commit_config


def task_body(**overrides) -> bytes:
    body = {
        "version": 1,
        "task_id": "JIRA-1",
        "project": "billing-platform",
        "agent": "bug-fixer",
        "instructions": "Fix it.",
        "context": {"k": "v"},
        "correlation": {"ticket": 1},
        "attachments": [
            {"name": "stack.txt", "content_type": "text/plain", "content_base64": base64.b64encode(b"trace").decode()},
            {"name": "shot.png", "content_type": "image/png", "url": "https://example.com/shot.png"},
        ],
    }
    body.update(overrides)
    return json.dumps(body).encode()


async def test_accepts_and_persists_task(sessions, store, configured, accept):
    intake = await accept(task_body())
    assert intake.run_id is not None
    [event] = intake.events
    envelope = event.envelope
    assert envelope.routing_key == "task.status"
    assert envelope.payload == {"state": "queued", "previous": None, "reason": None}
    assert (envelope.project, envelope.agent, envelope.correlation) == ("billing-platform", "bug-fixer", {"ticket": 1})
    assert envelope.run_id == str(intake.run_id)

    async with sessions() as session:
        task = await session.get(Task, "JIRA-1")
        run = await session.get(Run, intake.run_id)
        assert task.state == run.state == State.QUEUED
        assert task.context == {"k": "v"}
        by_name = {a.name: a for a in task.attachments}
        assert store.objects[by_name["stack.txt"].object_key][0] == b"trace"
        assert by_name["shot.png"].source_url == "https://example.com/shot.png"
        stored = (await session.scalars(select(Event))).all()
        assert [e.payload["payload"]["state"] for e in stored] == ["queued"]


async def rejection_reason(intake) -> str:
    assert intake.run_id is None
    [event] = intake.events
    assert event.envelope.payload["state"] == "rejected"
    assert event.envelope.run_id is None
    return event.envelope.payload["reason"]


async def test_duplicate_task_id_is_rejected_without_touching_the_task(sessions, store, configured, accept):
    first = await accept(task_body())
    second = await accept(task_body(instructions="other"))
    assert "duplicate" in await rejection_reason(second)
    async with sessions() as session:
        task = await session.get(Task, "JIRA-1")
        assert task.instructions == "Fix it." and task.state == State.QUEUED
        assert len((await session.scalars(select(Run))).all()) == 1
    assert first.run_id


async def test_unknown_project(sessions, store, configured, accept):
    reason = await rejection_reason(await accept(task_body(project="nope")))
    assert "unknown project" in reason


async def test_unknown_agent(sessions, store, configured, accept):
    reason = await rejection_reason(await accept(task_body(agent="nope")))
    assert reason == "unknown agent 'nope' in project 'billing-platform' (it defines bug-fixer, scout)"
    async with sessions() as session:
        assert await session.get(Task, "JIRA-1") is None


async def test_the_config_is_read_afresh(sessions, store, configured, accept):
    """An agent added to the file is known to the next task without a re-read."""
    commit = commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + "  helper: {}\n"})
    intake = await accept(task_body(agent="helper"))
    assert intake.run_id is not None
    async with sessions() as session:
        project = await session.get(Project, configured["project"].id)
        assert project.snapshot_commit == commit  # cached for the panel


async def test_config_error_rejects(sessions, store, configured, accept):
    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG.replace("notes:", "notez:")})
    reason = await rejection_reason(await accept(task_body()))
    assert reason == "config error: notez: unknown key"


SECRET_CONFIG = BASE_CONFIG + "secret_store: billing\nenv:\n  DB: ${secret:DB_PASSWORD}\n  T: ${secret:TOKEN}\n"


async def test_missing_store_and_keys_reject(sessions, store, configured, accept, box):
    commit_config(configured["remote"], {"exradius.yaml": SECRET_CONFIG})
    assert await rejection_reason(await accept(task_body())) == "secret store billing does not exist"
    async with sessions() as session:
        secret_store = SecretStore(name="billing")
        session.add(secret_store)
        await session.flush()
        session.add(SecretStoreEntry(store_id=secret_store.id, key="TOKEN", value_encrypted=box.encrypt("t")))
        await session.commit()
        store_id = secret_store.id
    assert await rejection_reason(await accept(task_body())) == "missing secret DB_PASSWORD in store billing"
    async with sessions() as session:
        session.add(SecretStoreEntry(store_id=store_id, key="DB_PASSWORD", value_encrypted=box.encrypt("p")))
        await session.commit()
    assert (await accept(task_body())).run_id is not None


async def test_unknown_provider_rejects(sessions, store, configured, accept):
    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG.replace("    role: You investigate.", "    role: x\n    provider: other")})
    reason = await rejection_reason(await accept(task_body(agent="scout")))
    assert reason == "unknown provider 'other'"


async def test_unreadable_config_rejects(sessions, store, configured, accept):
    commit_config(configured["remote"], {"exradius.yaml": None, "README": "x"})
    reason = await rejection_reason(await accept(task_body()))
    assert reason.startswith("config error: no exradius.yaml or .exradius/exradius.yaml on branch main")


async def test_invalid_message_with_task_id(sessions, store, configured, accept):
    reason = await rejection_reason(await accept(task_body(version=2)))
    assert reason.startswith("invalid task")


async def test_unparseable_messages_are_dropped(sessions, store, configured, accept):
    assert (await accept(b"not json")).events == []
    assert (await accept(b'{"version": 1}')).events == []


async def make_waiting_run(sessions, store, accept):
    intake = await accept(task_body(attachments=[]))
    async with sessions() as session:
        run = await session.get(Run, intake.run_id)
        run.state = run.task.state = State.WAITING_FOR_INPUT
        request = InfoRequest(run_id=run.id, question="Which region?")
        session.add(request)
        await session.commit()
        return run.id, request.id


def answer_body(request_id, answer="eu-west-1", task_id="JIRA-1", **extra) -> bytes:
    return json.dumps(
        {"version": 1, "task_id": task_id, "request_id": str(request_id), "answer": answer, **extra}
    ).encode()


async def test_first_answer_resumes_later_ones_are_stored(sessions, store, configured, accept):
    run_id, request_id = await make_waiting_run(sessions, store, accept)
    first = await accept_answer(
        sessions, store,
        answer_body(request_id, attachments=[{"name": "log.txt", "content_base64": base64.b64encode(b"log").decode()}]),
    )
    assert first.run_id == run_id
    second = await accept_answer(sessions, store, answer_body(request_id, answer="us-east-1"))
    assert second.run_id is None
    async with sessions() as session:
        request = await session.get(InfoRequest, request_id)
        assert request.answer == "eu-west-1" and request.answered_at is not None
        assert request.answer_attachments[0]["name"] == "log.txt"
        assert [late["answer"] for late in request.late_answers] == ["us-east-1"]


async def test_answers_for_unknown_requests_are_dropped(sessions, store, configured, accept):
    _, request_id = await make_waiting_run(sessions, store, accept)
    assert (await accept_answer(sessions, store, answer_body("not-a-uuid"))).run_id is None
    assert (await accept_answer(sessions, store, answer_body(request_id, task_id="OTHER"))).run_id is None
    assert (await accept_answer(sessions, store, b"{}")).run_id is None
