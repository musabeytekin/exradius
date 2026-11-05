"""Admin API additions for the frontend (docs/onboarding.md §4)."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from exradius.api import config_routes, task_routes
from exradius.models import Event, TranscriptChunk, Worker
from exradius.schemas.api import ConnectionTestOut
from tests.test_api import admin, client, control, make_run, setup_configuration, sync_db  # noqa: F401

NOW = datetime.now(UTC)


def add_worker(db, worker_id: str, seen_ago: float, images=(), docker_ok=True):
    db.add(
        Worker(
            id=worker_id, hostname=f"host-{worker_id}", started_at=NOW - timedelta(hours=1),
            last_seen_at=datetime.now(UTC) - timedelta(seconds=seen_ago), concurrency=4,
            docker_ok=docker_ok, docker_detail="reachable", images=list(images), version="0.1.0",
            runtimes=[{"name": "claude_code", "version": "0.1.0", "ready": True,
                       "image": "exradius-runtime-claude-code:0.1.0", "error": None}],
        )
    )
    db.commit()


# System -------------------------------------------------------------------------------------


def test_system_status(admin, sync_db, control, settings):
    add_worker(sync_db, "w-live", seen_ago=5)
    add_worker(sync_db, "w-stale", seen_ago=120)
    status = admin.get("/api/system/status").json()
    assert status["database"]["ok"] is True
    assert status["broker"] == {"ok": True, "detail": "connected"}
    assert [w["id"] for w in status["workers"]] == ["w-live"]  # unseen for 60 s: not listed
    worker = status["workers"][0]
    assert (worker["hostname"], worker["concurrency"], worker["docker_ok"]) == ("host-w-live", 4, True)
    assert worker["runtimes"] == [{"name": "claude_code", "version": "0.1.0", "ready": True,
                                   "image": "exradius-runtime-claude-code:0.1.0", "error": None}]
    assert "runner_image_ok" not in worker
    assert status["provider"] == {"configured": False, "default": None, "count": 0}
    assert "anthropic_key" not in status and "object_store" not in status
    assert status["public_api_url"] == settings.public_api_url
    assert status["data_dir"] == str(settings.data_dir)
    assert status["version"]

    control.healthy = False
    broker = admin.get("/api/system/status").json()["broker"]
    assert broker["ok"] is False and "broker down" in broker["detail"]


def test_system_requires_login(client):
    for path in ("/api/system/status", "/api/system/images", "/api/system/runtimes", "/api/system/integration"):
        assert client.get(path).status_code == 401


def test_system_images_from_live_heartbeats(admin, sync_db):
    runner = {"tag": "python:3.14", "created_at": "2026-11-05T10:00:00Z", "size": 1000}
    node = {"tag": "acme/node-project:1", "created_at": "2026-11-05T11:00:00Z", "size": 2000}
    old = {"tag": "acme/old:1", "created_at": None, "size": 3}
    add_worker(sync_db, "w1", seen_ago=1, images=[runner, node])
    add_worker(sync_db, "w2", seen_ago=2, images=[runner])
    add_worker(sync_db, "w3", seen_ago=600, images=[old])
    assert admin.get("/api/system/images").json() == [node, runner]  # sorted, deduplicated, live only


def test_system_runtimes(admin):
    [runtime] = admin.get("/api/system/runtimes").json()
    assert (runtime["name"], runtime["label"]) == ("claude_code", "Claude Code")
    assert [p["kind"] for p in runtime["providers"]] == ["anthropic"]  # details in test_providers.py


def test_system_integration_never_returns_the_password(admin, settings):
    settings.broker_url = "amqp://exradius:sup3r-s3cret@rabbitmq:5672/prod%2Feu"
    response = admin.get("/api/system/integration")
    assert "sup3r-s3cret" not in response.text
    info = response.json()
    assert info["broker"] == {"host": "rabbitmq", "port": 5672, "vhost": "prod/eu", "username": "exradius",
                              "password_set": True}
    assert info["tasks_queue"] == "exradius.tasks" and info["answers_queue"] == "exradius.answers"
    assert info["events_exchange"] == "exradius.events" and info["events_exchange_type"] == "topic"
    assert info["routing_keys"] == ["task.status", "task.info_request", "task.result"]

    settings.public_broker_host, settings.public_broker_port = "mq.example.com", 5671
    settings.broker_url = "amqp://guest@rabbitmq/"
    broker = admin.get("/api/system/integration").json()["broker"]
    assert broker == {"host": "mq.example.com", "port": 5671, "vhost": "/", "username": "guest", "password_set": False}


# Repository check ------------------------------------------------------------------------------


def make_repo(admin, url, branch="main", auth_secret_id=None):
    project = admin.post("/api/projects", json={"slug": f"p-{uuid.uuid4().hex[:6]}", "name": "P"}).json()
    body = {"name": "app", "url": url, "branch": branch, "auth_secret_id": auth_secret_id}
    repo = admin.post(f"/api/projects/{project['id']}/repositories", json=body).json()
    return project["id"], repo["id"]


def test_repository_check_lists_branches(admin, remote_repo):
    pid, rid = make_repo(admin, str(remote_repo))
    result = admin.post(f"/api/projects/{pid}/repositories/{rid}/check").json()
    assert result["ok"] is True and result["error"] is None
    assert result["branches"] == ["feature", "main"] and result["default_branch"] == "main"
    stored = admin.get(f"/api/projects/{pid}/repositories").json()[0]["last_check"]
    assert stored["ok"] is True and stored["branches"] == ["feature", "main"] and stored["checked_at"]

    # Changing the URL invalidates the stored check; renaming does not.
    admin.patch(f"/api/projects/{pid}/repositories/{rid}", json={"name": "renamed"})
    assert admin.get(f"/api/projects/{pid}/repositories").json()[0]["last_check"] is not None
    admin.patch(f"/api/projects/{pid}/repositories/{rid}", json={"url": str(remote_repo) + "/"})
    assert admin.get(f"/api/projects/{pid}/repositories").json()[0]["last_check"] is None


def test_repository_check_reports_a_missing_branch(admin, remote_repo):
    pid, rid = make_repo(admin, str(remote_repo), branch="develop")
    result = admin.post(f"/api/projects/{pid}/repositories/{rid}/check").json()
    assert result["ok"] is False and "develop" in result["error"]
    assert result["branches"] == ["feature", "main"]  # so the form can offer them


def test_repository_check_redacts_credentials(admin):
    project = admin.post("/api/projects", json={"slug": "private", "name": "P"}).json()
    secret = admin.post(f"/api/projects/{project['id']}/secrets", json={"key": "GIT_TOKEN", "value": "ghp_t0ps3cret"}).json()
    url = "https://bob:url-pa55word@127.0.0.1:9/acme/app.git"
    repo = admin.post(
        f"/api/projects/{project['id']}/repositories",
        json={"name": "app", "url": url, "branch": "main", "auth_secret_id": secret["id"]},
    ).json()
    response = admin.post(f"/api/projects/{project['id']}/repositories/{repo['id']}/check")
    result = response.json()
    assert result["ok"] is False and result["error"]
    assert "ghp_t0ps3cret" not in response.text and "url-pa55word" not in response.text
    assert "***@127.0.0.1" in result["error"]


# Verification (API side; the worker side is in test_verification.py) ---------------------------


def test_verify_publishes_and_tracks(admin, control):
    project = admin.post("/api/projects", json={"slug": "v", "name": "V"}).json()
    response = admin.post(f"/api/projects/{project['id']}/verify")
    assert response.status_code == 202
    vid = response.json()["verification_id"]
    assert control.verified == [vid]
    detail = admin.get(f"/api/projects/{project['id']}/verifications/{vid}").json()
    assert detail["state"] == "pending" and detail["steps"] == []
    admin.post(f"/api/projects/{project['id']}/verify")
    listed = admin.get(f"/api/projects/{project['id']}/verifications", params={"limit": 1}).json()
    assert len(listed) == 1 and listed[0]["id"] != vid  # newest first
    other = admin.post("/api/projects", json={"slug": "w", "name": "W"}).json()
    assert admin.get(f"/api/projects/{other['id']}/verifications/{vid}").status_code == 404


def test_verify_with_broker_down(admin, control):
    project = admin.post("/api/projects", json={"slug": "v", "name": "V"}).json()
    control.healthy = False
    assert admin.post(f"/api/projects/{project['id']}/verify").status_code == 503
    [verification] = admin.get(f"/api/projects/{project['id']}/verifications").json()
    assert verification["state"] == "failed" and "broker" in verification["error"]


# Resolved attachment -----------------------------------------------------------------------------


def test_resolved_attachment_shows_references_not_values(admin):
    cfg = setup_configuration(admin)
    pid, aid = cfg["project"]["id"], cfg["agent"]["id"]
    broken = admin.post(
        "/api/connections",
        json={"slug": "jira", "name": "Jira", "transport": "http", "url": "https://jira.example/mcp",
              "headers": {"Authorization": "Basic ${secret:JIRA_AUTH}", "X-Org": "acme"}},
    ).json()
    admin.patch(f"/api/agents/{aid}", json={"connection_ids": [cfg["docs"]["id"], cfg["sentry"]["id"], broken["id"]]})

    response = admin.get(f"/api/projects/{pid}/agents/{aid}/resolved")
    assert response.status_code == 200
    for value in ("project-docs-key", "alt-docs-key", "global-sentry", "ghp_secret"):
        assert value not in response.text
    resolved = {c["slug"]: c for c in response.json()["connections"]}

    docs = resolved["docs"]
    assert docs["headers"] == {"Authorization": "Bearer ${secret:DOCS_KEY}"}  # as written
    assert docs["references"] == [{"name": "DOCS_KEY", "resolves_to": "DOCS_KEY_ALT", "scope": "global"}]
    assert docs["unresolved"] == [] and docs["overridden"] is False

    sentry = resolved["sentry"]
    assert sentry["args"] == ["-y", "sentry-mcp", "--read-only"] and sentry["overridden"] is True
    assert sentry["env"] == {"SENTRY_TOKEN": "${secret:SENTRY_TOKEN}"}
    assert sentry["references"][0]["scope"] == "global"

    jira = resolved["jira"]
    assert jira["unresolved"] == ["JIRA_AUTH"] and jira["headers"]["X-Org"] == "acme"
    assert response.json()["secret_overrides"] == {"DOCS_KEY": "DOCS_KEY_ALT"}


def test_resolved_requires_attachment(admin):
    cfg = setup_configuration(admin)
    loner = admin.post("/api/agents", json={"slug": "loner", "name": "L"}).json()
    assert admin.get(f"/api/projects/{cfg['project']['id']}/agents/{loner['id']}/resolved").status_code == 404


# Connections keep their last test ---------------------------------------------------------------


def test_connection_last_test(admin, monkeypatch):
    connection = admin.post(
        "/api/connections", json={"slug": "fs", "name": "FS", "transport": "stdio", "command": "npx", "args": ["fs-mcp"]}
    ).json()
    assert connection["last_test"] is None

    async def fake_test(spec):
        return ConnectionTestOut(ok=True, tools=[{"name": "read_file", "description": "Read a file"}, {"name": "ls"}])

    monkeypatch.setattr(config_routes, "test_connection", fake_test)
    admin.post(f"/api/connections/{connection['id']}/test")
    last = admin.get(f"/api/connections/{connection['id']}").json()["last_test"]
    assert last["ok"] is True and last["error"] is None and last["tested_at"]
    assert last["tools"] == [{"name": "read_file", "description": "Read a file"}, {"name": "ls", "description": ""}]
    assert admin.get("/api/connections").json()[0]["last_test"]["ok"] is True

    admin.patch(f"/api/connections/{connection['id']}", json={"name": "Files"})  # cosmetic: kept
    assert admin.get(f"/api/connections/{connection['id']}").json()["last_test"] is not None
    admin.patch(f"/api/connections/{connection['id']}", json={"args": ["other-mcp"]})  # config change: cleared
    assert admin.get(f"/api/connections/{connection['id']}").json()["last_test"] is None


def test_failed_test_is_stored(admin):
    bad = admin.post(
        "/api/connections", json={"slug": "bad", "name": "Bad", "transport": "stdio", "command": "/nonexistent/mcp"}
    ).json()
    admin.post(f"/api/connections/{bad['id']}/test")
    last = admin.get(f"/api/connections/{bad['id']}").json()["last_test"]
    assert last["ok"] is False and last["error"] and last["tools"] == []


# Sending tasks and answers ----------------------------------------------------------------------


def test_send_task_publishes_the_envelope(admin, control):
    body = {"task_id": "UI-1", "project": "billing-platform", "agent": "bug-fixer",
            "instructions": "Fix it.", "context": {"a": 1}, "correlation": {"from": "ui"}}
    response = admin.post("/api/tasks", json=body)
    assert response.status_code == 202
    [(queue, message)] = control.published
    assert queue == "exradius.tasks"
    assert message == {**body, "version": 1, "attachments": []}
    assert response.json() == {"queue": "exradius.tasks", "message": message}


@pytest.mark.parametrize(
    "body",
    [
        {"task_id": "UI-1", "project": "p", "agent": "a"},  # no instructions
        {"task_id": "UI-1", "project": "p", "agent": "a", "instructions": "x", "version": 2},
        {"task_id": "UI-1", "project": "p", "agent": "a", "instructions": "x",
         "attachments": [{"name": "../x", "content_base64": "aGk="}]},
    ],
)
def test_send_task_validates(admin, control, body):
    assert admin.post("/api/tasks", json=body).status_code == 422
    assert control.published == []


def test_send_task_with_broker_down(admin, control):
    control.healthy = False
    body = {"task_id": "UI-1", "project": "p", "agent": "a", "instructions": "x"}
    assert admin.post("/api/tasks", json=body).status_code == 503


def test_answer_publishes(admin, control):
    response = admin.post(
        "/api/tasks/JIRA-1/answer",
        json={"request_id": "a7c1", "answer": "Yes.", "attachments": [{"name": "log.txt", "content_base64": "aGk="}]},
    )
    assert response.status_code == 202
    [(queue, message)] = control.published
    assert queue == "exradius.answers"
    assert message["version"] == 1 and message["task_id"] == "JIRA-1" and message["request_id"] == "a7c1"
    assert message["attachments"][0]["name"] == "log.txt"
    assert admin.post("/api/tasks/JIRA-1/answer", json={"answer": "no id"}).status_code == 422
    dup = [{"name": "a", "content_base64": "aGk="}] * 2
    assert admin.post("/api/tasks/JIRA-1/answer", json={"request_id": "x", "answer": "y", "attachments": dup}).status_code == 422


def test_send_requires_login(client):
    assert client.post("/api/tasks", json={}).status_code == 401
    assert client.post("/api/tasks/x/answer", json={}).status_code == 401


# Events and the live stream ------------------------------------------------------------------------


def add_event(db, task_id, type_, state=None, seconds=0, run_id=None):
    payload = {"version": 1, "task_id": task_id, "type": type_,
               "payload": {"state": state} if state else {"request_id": "r", "question": "q"}}
    event = Event(id=uuid.uuid4(), task_id=task_id, run_id=run_id, type=type_, payload=payload,
                  created_at=NOW + timedelta(seconds=seconds))
    db.add(event)
    db.commit()
    return event


def test_task_events_and_tail(admin, sync_db):
    a1 = add_event(sync_db, "A", "status", "queued", seconds=1)
    b1 = add_event(sync_db, "B", "status", "queued", seconds=2)
    a2 = add_event(sync_db, "A", "status", "running", seconds=3)
    a3 = add_event(sync_db, "A", "result", "completed", seconds=4)

    events = admin.get("/api/tasks/A/events").json()
    assert [e["id"] for e in events] == [str(a1.id), str(a2.id), str(a3.id)]
    assert events[0]["task_id"] == "A" and events[0]["payload"]["payload"]["state"] == "queued"
    assert admin.get("/api/tasks/nobody/events").json() == []

    assert [e["id"] for e in admin.get("/api/events", params={"limit": 2}).json()] == [str(a2.id), str(a3.id)]
    since = (NOW + timedelta(seconds=1.5)).isoformat()
    assert [e["id"] for e in admin.get("/api/events", params={"since": since}).json()] == [
        str(b1.id), str(a2.id), str(a3.id)
    ]
    assert [e["id"] for e in admin.get("/api/events", params={"since": since, "limit": 1}).json()] == [str(b1.id)]


def parse_sse(text: str) -> list[dict]:
    messages = []
    for block in text.split("\n\n"):
        fields: dict = {}
        for line in block.splitlines():
            if line.startswith(":"):
                fields.setdefault("comments", []).append(line)
            elif ":" in line:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        if fields:
            messages.append(fields)
    return messages


@pytest.fixture
def fast_stream(monkeypatch):
    monkeypatch.setattr(task_routes, "STREAM_POLL_SECONDS", 0.05)
    monkeypatch.setattr(task_routes, "STREAM_HEARTBEAT_SECONDS", 0.2)


def test_stream_replays_and_ends_after_result(admin, sync_db, fast_stream):
    task_id, run_id = make_run(admin, sync_db)
    rid = uuid.UUID(run_id)
    add_event(sync_db, task_id, "status", "queued", seconds=1, run_id=rid)
    sync_db.add_all([TranscriptChunk(run_id=rid, seq=i, payload={"n": i}) for i in (1, 2)])
    sync_db.commit()
    add_event(sync_db, task_id, "result", "completed", seconds=2, run_id=rid)

    response = admin.get(f"/api/tasks/{task_id}/stream")
    assert response.headers["content-type"].startswith("text/event-stream")
    messages = [m for m in parse_sse(response.text) if "event" in m]
    assert [m["event"] for m in messages] == ["transcript", "transcript", "status", "result"]
    assert json.loads(messages[0]["data"]) == {"run_id": run_id, "seq": 1, "payload": {"n": 1}}
    assert json.loads(messages[3]["data"])["payload"]["state"] == "completed"
    assert all(m.get("id") for m in messages)


def test_stream_follows_live_updates(admin, sync_db, db_path, fast_stream):
    task_id, run_id = make_run(admin, sync_db)
    rid = uuid.UUID(run_id)
    add_event(sync_db, task_id, "status", "running", seconds=1, run_id=rid)

    def later():
        engine = create_engine(f"sqlite:///{db_path}")
        with Session(engine) as db:
            db.add(TranscriptChunk(run_id=rid, seq=1, payload={"type": "AssistantMessage"}))
            db.commit()
            add_event(db, task_id, "info_request", seconds=2, run_id=rid)
            add_event(db, task_id, "result", "completed", seconds=3, run_id=rid)
        engine.dispose()

    timer = threading.Timer(0.5, later)
    timer.start()
    try:
        response = admin.get(f"/api/tasks/{task_id}/stream")
    finally:
        timer.join()
    messages = parse_sse(response.text)
    assert [m["event"] for m in messages if "event" in m] == ["status", "transcript", "info_request", "result"]
    assert any(": heartbeat" in m.get("comments", []) for m in messages)  # sent while waiting


def test_stream_waits_for_a_task_that_is_rejected(admin, sync_db, fast_stream):
    """A task sent from the panel may be rejected before it is ever stored."""
    add_event(sync_db, "never-stored", "status", "rejected", seconds=1)
    messages = [m for m in parse_sse(admin.get("/api/tasks/never-stored/stream").text) if "event" in m]
    assert [json.loads(m["data"])["payload"]["state"] for m in messages] == ["rejected"]


def test_stream_requires_login(client):
    assert client.get("/api/tasks/x/stream").status_code == 401


