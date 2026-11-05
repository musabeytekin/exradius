import base64
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from exradius import downloads
from exradius.api.app import create_app
from exradius.crypto import hash_token
from exradius.models import Artifact, InfoRequest, Run, Secret, Task, TranscriptChunk


class FakeControl:
    """Stands in for the API's broker publisher."""

    def __init__(self):
        self.cancelled: list[str] = []
        self.verified: list[str] = []
        self.provider_tests: list[str] = []
        self.published: list[tuple[str, dict]] = []
        self.healthy = True

    async def publish(self, queue: str, body: dict) -> None:
        if not self.healthy:
            raise ConnectionError("broker down")
        self.published.append((queue, body))

    async def cancel(self, run_id: str) -> None:
        self.cancelled.append(run_id)

    async def verify(self, verification_id: str) -> None:
        if not self.healthy:
            raise ConnectionError("broker down")
        self.verified.append(verification_id)

    async def test_provider(self, test_id: str) -> None:
        if not self.healthy:
            raise ConnectionError("broker down")
        self.provider_tests.append(test_id)

    async def check(self) -> tuple[bool, str]:
        return (True, "connected") if self.healthy else (False, "ConnectionError: broker down")


@pytest.fixture
def control():
    return FakeControl()


@pytest.fixture
def client(settings, store, control):
    app = create_app(settings, store=store, broker=control)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(client):
    response = client.post("/api/auth/login", json={"username": "admin", "password": "correct horse"})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture
def sync_db(db_path):
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_login_required(client):
    assert client.get("/api/projects").status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": "wrong"}).status_code == 401
    assert client.get("/healthz").json() == {"ok": True}


def test_login_logout(admin):
    assert admin.get("/api/auth/me").json()["username"] == "admin"
    admin.post("/api/auth/logout")
    assert admin.get("/api/auth/me").status_code == 401


def setup_configuration(admin) -> dict:
    project = admin.post(
        "/api/projects", json={"slug": "billing-platform", "name": "Billing", "setup_commands": ["make deps"]}
    ).json()
    pid = project["id"]
    token = admin.post(f"/api/projects/{pid}/secrets", json={"key": "GIT_TOKEN", "value": "ghp_secret"}).json()
    admin.post(f"/api/projects/{pid}/secrets", json={"key": "DOCS_KEY", "value": "project-docs-key"})
    admin.post("/api/secrets", json={"key": "DOCS_KEY_ALT", "value": "alt-docs-key"})
    admin.post("/api/secrets", json={"key": "SENTRY_TOKEN", "value": "global-sentry"})
    repo = admin.post(
        f"/api/projects/{pid}/repositories",
        json={"name": "api", "url": "https://git.example/api.git", "branch": "main", "auth_secret_id": token["id"]},
    )
    assert repo.status_code == 201, repo.text
    admin.post(f"/api/projects/{pid}/env-vars", json={"key": "LOG_LEVEL", "value": "debug"})
    docs = admin.post(
        "/api/connections",
        json={"slug": "docs", "name": "Docs", "transport": "http", "url": "https://mcp.example/mcp",
              "headers": {"Authorization": "Bearer ${secret:DOCS_KEY}"}},
    ).json()
    sentry = admin.post(
        "/api/connections",
        json={"slug": "sentry", "name": "Sentry", "transport": "stdio", "command": "npx",
              "args": ["-y", "sentry-mcp"], "env": {"SENTRY_TOKEN": "${secret:SENTRY_TOKEN}"}},
    ).json()
    agent = admin.post(
        "/api/agents",
        json={"slug": "bug-fixer", "name": "Bug fixer", "prompt": "You fix bugs.",
              "connection_ids": [docs["id"], sentry["id"]]},
    ).json()
    link = admin.post(
        f"/api/projects/{pid}/agents",
        json={"agent_id": agent["id"], "secret_overrides": {"DOCS_KEY": "DOCS_KEY_ALT"},
              "connection_overrides": {"sentry": {"args": ["-y", "sentry-mcp", "--read-only"]}}},
    )
    assert link.status_code == 201, link.text
    return {"project": project, "agent": agent, "docs": docs, "sentry": sentry}


def test_configuration_crud(admin):
    cfg = setup_configuration(admin)
    pid = cfg["project"]["id"]

    assert [p["slug"] for p in admin.get("/api/projects").json()] == ["billing-platform"]
    assert admin.patch(f"/api/projects/{pid}", json={"name": "Billing v2"}).json()["name"] == "Billing v2"
    assert admin.get(f"/api/projects/{pid}/repositories").json()[0]["name"] == "api"
    assert admin.get(f"/api/projects/{pid}/env-vars").json()[0]["key"] == "LOG_LEVEL"
    connection_ids = admin.get(f"/api/agents/{cfg['agent']['id']}").json()["connection_ids"]
    assert connection_ids == [cfg["docs"]["id"], cfg["sentry"]["id"]]  # ordered by slug
    assert "read-only" in admin.get(f"/api/connections/{cfg['docs']['id']}").json()["notice"]

    # Conflicts and validation
    assert admin.post("/api/projects", json={"slug": "billing-platform", "name": "x"}).status_code == 409
    assert admin.post("/api/projects", json={"slug": "Bad Slug", "name": "x"}).status_code == 422
    assert admin.post("/api/connections", json={"slug": "x", "name": "x", "transport": "stdio"}).status_code == 422
    assert admin.post("/api/connections", json={"slug": "exradius", "name": "x", "transport": "http", "url": "http://x"}).status_code == 422
    assert admin.patch(f"/api/connections/{cfg['docs']['id']}", json={"url": None}).status_code == 422

    # Detach and delete
    assert admin.delete(f"/api/projects/{pid}/agents/{cfg['agent']['id']}").status_code == 204
    assert admin.get(f"/api/projects/{pid}/agents").json() == []
    assert admin.delete(f"/api/agents/{cfg['agent']['id']}").status_code == 204
    assert admin.get(f"/api/agents/{uuid.uuid4()}").status_code == 404


def test_secrets_are_write_only_and_encrypted(admin, sync_db):
    cfg = setup_configuration(admin)
    pid = cfg["project"]["id"]
    listed = admin.get(f"/api/projects/{pid}/secrets").json()
    assert {s["key"] for s in listed} == {"GIT_TOKEN", "DOCS_KEY"}
    assert all(set(s) == {"id", "project_id", "key"} for s in listed)
    assert {s["key"] for s in admin.get("/api/secrets").json()} == {"DOCS_KEY_ALT", "SENTRY_TOKEN"}
    assert admin.post(f"/api/projects/{pid}/secrets", json={"key": "GIT_TOKEN", "value": "x"}).status_code == 409

    stored = sync_db.scalars(select(Secret)).all()
    assert all("ghp_secret" not in s.value_encrypted for s in stored)

    secret_id = next(s["id"] for s in listed if s["key"] == "GIT_TOKEN")
    replaced = admin.put(f"/api/projects/{pid}/secrets/{secret_id}", json={"value": "new"})
    assert replaced.status_code == 200 and "value" not in replaced.json()


def test_connection_test_reports_errors(admin):
    cfg = setup_configuration(admin)
    bad = admin.post(
        "/api/connections",
        json={"slug": "broken", "name": "Broken", "transport": "stdio", "command": "/nonexistent/mcp-server"},
    ).json()
    result = admin.post(f"/api/connections/{bad['id']}/test").json()
    assert result["ok"] is False and result["error"]
    # Without a project, project secrets are not available.
    result = admin.post(f"/api/connections/{cfg['docs']['id']}/test").json()
    assert result == {"ok": False, "tools": [], "error": "secret 'DOCS_KEY' is referenced but not defined"}


def make_run(admin, sync_db, state="running", with_token="tok") -> tuple[str, str]:
    cfg = setup_configuration(admin)
    task = Task(id="JIRA-1", project_id=uuid.UUID(cfg["project"]["id"]), agent_id=uuid.UUID(cfg["agent"]["id"]),
                instructions="Fix it.", context={"k": "v"}, correlation=None, state=state)
    run = Run(id=uuid.uuid4(), task_id="JIRA-1", state=state, token_hash=hash_token(with_token) if with_token else None)
    sync_db.add_all([task, run])
    sync_db.commit()
    return "JIRA-1", str(run.id)


def test_task_endpoints_and_cancel(admin, sync_db, control):
    task_id, run_id = make_run(admin, sync_db)
    tasks = admin.get("/api/tasks", params={"state": "running", "project": "billing-platform"}).json()
    assert [(t["task_id"], t["run_id"]) for t in tasks] == [(task_id, run_id)]
    assert admin.get("/api/tasks", params={"state": "completed"}).json() == []
    assert admin.get("/api/tasks", params={"agent": "nobody"}).json() == []

    detail = admin.get(f"/api/tasks/{task_id}").json()
    assert detail["run"]["state"] == "running" and detail["instructions"] == "Fix it."

    sync_db.add_all([TranscriptChunk(run_id=uuid.UUID(run_id), seq=i, payload={"n": i}) for i in (1, 2, 3)])
    sync_db.commit()
    chunks = admin.get(f"/api/tasks/{task_id}/transcript", params={"after_seq": 1}).json()
    assert [c["seq"] for c in chunks] == [2, 3]

    assert admin.post(f"/api/tasks/{task_id}/cancel").status_code == 202
    assert control.cancelled == [run_id]
    assert admin.get("/api/tasks/nope").status_code == 404


def test_cancel_finished_task_conflicts(admin, sync_db):
    task_id, _ = make_run(admin, sync_db, state="completed", with_token=None)
    assert admin.post(f"/api/tasks/{task_id}/cancel").status_code == 409


def test_internal_api_requires_the_run_token(client, admin, sync_db):
    _, run_id = make_run(admin, sync_db)
    base = f"/internal/runs/{run_id}"
    assert client.get(f"{base}/spec").status_code == 401
    assert client.get(f"{base}/spec", headers={"Authorization": "Bearer wrong"}).status_code == 401
    other = f"/internal/runs/{uuid.uuid4()}/spec"
    assert client.get(other, headers={"Authorization": "Bearer tok"}).status_code == 401


def test_internal_spec_resolves_connections_and_secrets(client, admin, sync_db):
    _, run_id = make_run(admin, sync_db)
    auth = {"Authorization": "Bearer tok"}
    spec = client.get(f"/internal/runs/{run_id}/spec", headers=auth).json()
    assert spec["mode"] == "start" and spec["runtime"] == "claude_code"
    assert spec["agent_prompt"] == "You fix bugs." and spec["context"] == {"k": "v"}
    by_name = {c["name"]: c for c in spec["connections"]}
    assert by_name["docs"]["headers"] == {"Authorization": "Bearer alt-docs-key"}  # secret override
    assert by_name["sentry"]["env"] == {"SENTRY_TOKEN": "global-sentry"}  # global fallback
    assert by_name["sentry"]["args"] == ["-y", "sentry-mcp", "--read-only"]  # connection override


def test_internal_run_reporting(client, admin, sync_db, store):
    _, run_id = make_run(admin, sync_db)
    auth = {"Authorization": "Bearer tok"}
    base = f"/internal/runs/{run_id}"

    assert client.post(f"{base}/session", json={"session_id": "sess-1"}, headers=auth).status_code == 204
    batch = {"events": [{"seq": 1, "payload": {"a": 1}}, {"seq": 2, "payload": {"b": 2}}]}
    assert client.post(f"{base}/transcript", json=batch, headers=auth).status_code == 204
    assert client.post(f"{base}/transcript", json=batch, headers=auth).status_code == 204  # retry is idempotent

    created = client.post(f"{base}/info-requests", json={"question": "Which region?"}, headers=auth)
    assert created.status_code == 201
    request_id = created.json()["request_id"]

    files = [{"name": "report.md", "content_type": "text/markdown", "content_base64": base64.b64encode(b"# R").decode()}]
    client.post(f"{base}/result", json={"output": {"v": 1}, "artifacts": files}, headers=auth)
    client.post(f"{base}/result", json={"output": {"v": 2}, "artifacts": files}, headers=auth)  # last wins
    outcome = {"status": "completed", "session_id": "sess-1", "final_message": "Done."}
    assert client.post(f"{base}/outcome", json=outcome, headers=auth).status_code == 204

    sync_db.expire_all()
    run = sync_db.get(Run, uuid.UUID(run_id))
    assert run.runtime_session_id == "sess-1" and run.reported_status == "completed"
    assert run.summary == "Done." and run.output == {"v": 2}
    assert run.state == "running"  # the worker performs the transition, not the API
    assert sync_db.scalar(select(InfoRequest)).id == uuid.UUID(request_id)
    assert len(sync_db.scalars(select(TranscriptChunk)).all()) == 2
    [artifact] = sync_db.scalars(select(Artifact)).all()
    assert store.objects[artifact.object_key][0] == b"# R"

    download = client.get(f"/api/artifacts/{artifact.id}/content")
    assert download.status_code == 200 and download.content == b"# R"


def test_downloads_accept_admin_session_or_signature(client, admin, sync_db, store, settings):
    _, run_id = make_run(admin, sync_db)
    auth = {"Authorization": "Bearer tok"}
    files = [{"name": "report.md", "content_type": "text/markdown", "content_base64": base64.b64encode(b"# R").decode()}]
    client.post(f"/internal/runs/{run_id}/result", json={"output": None, "artifacts": files}, headers=auth)
    sync_db.add_all([TranscriptChunk(run_id=uuid.UUID(run_id), seq=i, payload={"n": i}) for i in (1, 2)])
    sync_db.commit()
    artifact = sync_db.scalar(select(Artifact))

    admin.post("/api/auth/logout")
    artifact_url = downloads.artifact_url(settings, artifact.id).removeprefix(settings.public_api_url)
    transcript_url = downloads.transcript_url(settings, run_id).removeprefix(settings.public_api_url)

    # Signed URLs from task.result work without a session...
    response = client.get(artifact_url)
    assert response.status_code == 200 and response.content == b"# R"
    assert response.headers["content-type"].startswith("text/markdown")
    # ...the transcript is assembled from chunks while the run is still going...
    response = client.get(transcript_url)
    assert response.status_code == 200
    assert [json.loads(line) for line in response.text.splitlines()] == [{"n": 1}, {"n": 2}]
    # ...and served from the object store once finalisation has stored it.
    store.objects[downloads.transcript_key(run_id)] = (b'{"final": true}\n', "application/x-ndjson")
    assert client.get(transcript_url).text == '{"final": true}\n'

    # Without a session, a missing, wrong or borrowed signature is refused.
    assert client.get(f"/api/artifacts/{artifact.id}/content").status_code == 401
    assert client.get(f"/api/artifacts/{artifact.id}/content?sig=deadbeef").status_code == 401
    borrowed = transcript_url.split("?sig=")[1]
    assert client.get(f"/api/artifacts/{artifact.id}/content?sig={borrowed}").status_code == 401
    assert client.get(f"/api/runs/{run_id}/transcript").status_code == 401


def test_artifact_names_cannot_escape(client, admin, sync_db):
    _, run_id = make_run(admin, sync_db)
    bad = [{"name": "../../etc/x", "content_base64": base64.b64encode(b"x").decode()}]
    response = client.post(
        f"/internal/runs/{run_id}/result", json={"output": None, "artifacts": bad},
        headers={"Authorization": "Bearer tok"},
    )
    assert response.status_code == 422


def test_internal_resume_spec(client, admin, sync_db):
    _, run_id = make_run(admin, sync_db)
    request = InfoRequest(run_id=uuid.UUID(run_id), question="Which region?", answer="eu-west-1",
                          answer_attachments=[{"name": "log.txt", "content_type": "text/plain", "object_key": "k"}])
    sync_db.add(request)
    sync_db.flush()
    run = sync_db.get(Run, uuid.UUID(run_id))
    run.resume_request_ids = [str(request.id)]
    run.runtime_session_id = "sess-1"
    sync_db.commit()
    spec = client.get(f"/internal/runs/{run_id}/spec", headers={"Authorization": "Bearer tok"}).json()
    assert spec["mode"] == "resume" and spec["session_id"] == "sess-1"
    [answer] = spec["answers"]
    assert answer["answer"] == "eu-west-1"
    assert answer["attachments"] == [{"path": f".exradius/attachments/{request.id}/log.txt", "content_type": "text/plain"}]


def test_token_is_rejected_once_run_ended(client, admin, sync_db):
    _, run_id = make_run(admin, sync_db, state="completed")
    assert client.get(f"/internal/runs/{run_id}/spec", headers={"Authorization": "Bearer tok"}).status_code == 401


