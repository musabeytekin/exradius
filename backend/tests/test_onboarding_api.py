"""Admin API additions for the frontend (docs/onboarding.md §4)."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, delete, update
from sqlalchemy.orm import Session

from exradius.api import task_routes
from exradius.models import Event, ProjectVerification, TranscriptChunk, Worker
from tests.conftest import commit_config, make_config_repo
from tests.test_api import API_CONFIG, admin, client, control, make_run, setup_configuration, sync_db  # noqa: F401

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


# Verification (API side; the worker side is in test_verification.py) ---------------------------


def finish(sync_db, vid: str, state: str = "passed", commit: str | None = None) -> None:
    """What the worker does at the end of a verification."""
    sync_db.execute(
        update(ProjectVerification).where(ProjectVerification.id == uuid.UUID(vid))
        .values(state=state, commit=commit, ended_at=datetime.now(UTC))
    )
    sync_db.commit()


def test_adding_a_project_verifies_it(admin, control, tmp_path):
    project = setup_configuration(admin, tmp_path)["project"]
    [vid] = control.verified
    assert project["verification"] == {"id": vid, "state": "pending", "commit": None, "ended_at": None,
                                       "diagnosis": None}
    assert project["freshness"] == "running"

    with_errors = setup_configuration(admin, tmp_path / "errors", API_CONFIG.replace(
        "billing-platform", "with-errors").replace("\nenv:", "\nenvs:"))["project"]
    assert with_errors["health"] == "config_errors" and with_errors["freshness"] == "running"
    assert len(control.verified) == 2

    remote = make_config_repo(tmp_path / "unreadable", {"README.md": "nothing here\n"})
    assert admin.post("/api/projects", json={"config_url": str(remote)}).status_code == 422
    assert len(control.verified) == 2


def test_one_open_verification_per_project(admin, control, sync_db, tmp_path):
    project = setup_configuration(admin, tmp_path)["project"]
    [auto] = control.verified
    response = admin.post(f"/api/projects/{project['id']}/verify")
    assert response.status_code == 202 and response.json()["verification_id"] == auto  # the open one
    assert control.verified == [auto]
    detail = admin.get(f"/api/projects/{project['id']}/verifications/{auto}").json()
    assert detail["state"] == "pending" and detail["steps"] == [] and detail["commit"] is None
    assert detail["diagnosis"] is None

    finish(sync_db, auto, "running")
    assert admin.post(f"/api/projects/{project['id']}/verify").json()["verification_id"] == auto
    finish(sync_db, auto, "passed", project["commit"])
    vid = admin.post(f"/api/projects/{project['id']}/verify").json()["verification_id"]
    assert vid != auto and control.verified == [auto, vid]
    listed = admin.get(f"/api/projects/{project['id']}/verifications", params={"limit": 1}).json()
    assert [v["id"] for v in listed] == [vid]  # newest first
    other = setup_configuration(admin, tmp_path / "other", API_CONFIG.replace("billing-platform", "other"))["project"]
    assert admin.get(f"/api/projects/{other['id']}/verifications/{vid}").status_code == 404


def test_reread_verifies_a_new_commit_only(admin, control, sync_db, tmp_path):
    cfg = setup_configuration(admin, tmp_path)
    pid, first = cfg["project"]["id"], cfg["project"]["commit"]
    [auto] = control.verified
    finish(sync_db, auto, "passed", first)
    assert admin.get(f"/api/projects/{pid}").json()["freshness"] == "current"

    unchanged = admin.post(f"/api/projects/{pid}/reread").json()
    assert unchanged["commit"] == first and control.verified == [auto]
    assert unchanged["freshness"] == "current" and unchanged["verification"]["id"] == auto

    second = commit_config(cfg["remote"], {"exradius.yaml": API_CONFIG + "notes: new\n"})
    reread = admin.post(f"/api/projects/{pid}/reread").json()
    assert reread["commit"] == second and len(control.verified) == 2
    assert reread["freshness"] == "running" and reread["verification"]["id"] == control.verified[1]


def test_freshness(admin, control, sync_db, tmp_path):
    cfg = setup_configuration(admin, tmp_path)
    pid, first = cfg["project"]["id"], cfg["project"]["commit"]
    [auto] = control.verified

    def fresh():
        return admin.get(f"/api/projects/{pid}").json()

    finish(sync_db, auto, "failed", first)
    assert fresh()["freshness"] == "failed"
    finish(sync_db, auto, "passed", first)
    assert fresh()["freshness"] == "current" and fresh()["verification"]["commit"] == first
    commit_config(cfg["remote"], {"exradius.yaml": API_CONFIG + "notes: new\n"})
    control.healthy = False  # the new commit's verification cannot be handed over
    reread = admin.post(f"/api/projects/{pid}/reread")
    assert reread.status_code == 200 and reread.json()["freshness"] == "failed"
    sync_db.execute(delete(ProjectVerification).where(ProjectVerification.state == "failed"))
    sync_db.commit()
    assert fresh()["freshness"] == "stale"  # passed, at an older commit
    sync_db.execute(delete(ProjectVerification))
    sync_db.commit()
    assert fresh()["freshness"] == "never" and fresh()["verification"] is None
    assert [p["freshness"] for p in admin.get("/api/projects").json()] == ["never"]


def test_verify_with_broker_down(admin, control, sync_db, tmp_path):
    control.healthy = False
    project = setup_configuration(admin, tmp_path)["project"]  # adding still works
    # One when the project was added, one when its last key arrived (§8): both failed.
    found = admin.get(f"/api/projects/{project['id']}/verifications").json()
    assert len(found) == 2 and all(v["state"] == "failed" and "broker" in v["error"] for v in found)
    assert project["freshness"] == "failed"
    assert admin.post(f"/api/projects/{project['id']}/verify").status_code == 503
    latest = admin.get(f"/api/projects/{project['id']}/verifications").json()
    assert len(latest) == 3 and latest[0]["state"] == "failed" and "broker" in latest[0]["error"]


# The resolved agent --------------------------------------------------------------------------------


RESOLVED_CONFIG = API_CONFIG.replace(
    "connections: [docs, sentry]",
    "connections: [docs, sentry, github, files]\n    model: claude-sonnet-5-5\n    provider: main",
).replace(
    "connections:\n  docs:",
    "connections:\n  github:\n    catalog: github\n    config:\n      token: ${secret:GITHUB_TOKEN}\n"
    "  files:\n    catalog: filesystem\n  docs:",
) + "  scout: {}\n"


def test_resolved_agent_shows_placeholders_not_values(admin, tmp_path):
    cfg = setup_configuration(admin, tmp_path, RESOLVED_CONFIG, values={"GITHUB_TOKEN": "ghp_live", "DOCS_KEY": "docs-key"})
    pid = cfg["project"]["id"]
    response = admin.get(f"/api/projects/{pid}/agents/bug-fixer/resolved")
    assert response.status_code == 200
    for value in ("ghp_live", "docs-key"):
        assert value not in response.text
    resolved = response.json()
    assert (resolved["name"], resolved["role"], resolved["model"], resolved["runtime"]) == (
        "Bug fixer", "You fix bugs.", "claude-sonnet-5-5", "claude_code")
    assert resolved["provider"] == "main" and resolved["resolved_provider"] is None
    assert resolved["provider_error"] == "unknown provider 'main'"
    assert resolved["identity"] == {"name": "Bug fixer", "email": "bug-fixer@agents.example"}
    assert resolved["commit"] == cfg["project"]["commit"] and resolved["image"] == "exradius-project-default:0.1.0"
    assert resolved["environment_text"].startswith("# Your environment")
    connections = {c["name"]: c for c in resolved["connections"]}
    assert list(connections) == ["docs", "sentry", "github", "files"]
    assert connections["github"] == {
        "name": "github", "catalog": "github", "transport": "http", "command": None, "args": [],
        "url": "https://api.githubcopilot.com/mcp/", "env": {}, "headers": {"Authorization": "Bearer $GITHUB_TOKEN"},
        "secrets": ["GITHUB_TOKEN"],
    }
    assert connections["files"]["args"][-1] == "/work" and connections["files"]["secrets"] == []
    assert connections["sentry"]["env"] == {"SENTRY_TOKEN": "$SENTRY_TOKEN"}

    scout = admin.get(f"/api/projects/{pid}/agents/scout/resolved").json()
    assert scout["name"] == "Scout" and scout["role"] == "" and scout["connections"] == []
    assert scout["identity"] == {"name": "Scout", "email": "scout@billing-platform.exradius.local"}
    assert scout["provider"] is None and scout["provider_error"] == "no provider configured"
    assert admin.get(f"/api/projects/{pid}/agents/nobody/resolved").status_code == 404


def test_resolved_agent_needs_a_valid_config(admin, tmp_path):
    cfg = setup_configuration(admin, tmp_path, API_CONFIG.replace("\nenv:", "\nenvs:"))
    assert admin.get(f"/api/projects/{cfg['project']['id']}/agents/bug-fixer/resolved").status_code == 409


def test_project_warns_about_unknown_providers(admin, tmp_path):
    cfg = setup_configuration(admin, tmp_path, RESOLVED_CONFIG)
    assert "agents.bug-fixer.provider: no provider 'main'; tasks for this agent are refused" in cfg["project"]["warnings"]


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


def test_stream_replays_and_ends_after_result(admin, sync_db, fast_stream, tmp_path):
    task_id, run_id = make_run(admin, sync_db, tmp_path)
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


def test_stream_follows_live_updates(admin, sync_db, db_path, fast_stream, tmp_path):
    task_id, run_id = make_run(admin, sync_db, tmp_path)
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




def test_keys_arriving_verify_the_project_again(admin, control, sync_db, tmp_path):
    """docs/run-environment.md §8: a verification that failed for missing keys is rerun once
    the store has them all, under the one-open rule; a current one is left alone."""
    remote = make_config_repo(tmp_path, {"exradius.yaml": API_CONFIG})
    project = admin.post("/api/projects", json={"config_url": str(remote)}).json()
    [auto] = control.verified
    finish(sync_db, auto, "failed", project["commit"])  # the store did not exist

    store = admin.post("/api/stores", json={"name": "billing"}).json()
    assert control.verified == [auto]  # created, but keys are still missing
    assert admin.put(f"/api/stores/{store['id']}/keys", json={"values": {"GIT_TOKEN": "t", "DOCS_KEY": "d"}}).status_code == 200
    assert control.verified == [auto]  # SENTRY_TOKEN is still missing
    assert admin.put(f"/api/stores/{store['id']}/keys/SENTRY_TOKEN", json={"value": "s"}).json()["key"] == "SENTRY_TOKEN"
    assert len(control.verified) == 2  # all present now
    second = control.verified[1]
    assert admin.put(f"/api/stores/{store['id']}/keys/SENTRY_TOKEN", json={"value": "s2"}).status_code == 200
    assert control.verified == [auto, second]  # that one is still open

    finish(sync_db, second, "passed", project["commit"])
    assert admin.put(f"/api/stores/{store['id']}/keys", json={"values": {"EXTRA": "x"}}).status_code == 200
    assert control.verified == [auto, second]  # current: nothing to do


def test_store_created_after_the_project(admin, control, sync_db, tmp_path):
    """A project whose config names a store but needs no key verifies when the store appears."""
    config = "version: 1\nproject: {slug: plain}\nsecret_store: later\nagents:\n  dev: {}\n"
    remote = make_config_repo(tmp_path, {"exradius.yaml": config})
    project = admin.post("/api/projects", json={"config_url": str(remote)}).json()
    [auto] = control.verified
    finish(sync_db, auto, "failed", project["commit"])
    admin.post("/api/stores", json={"name": "later"})
    assert len(control.verified) == 2
