import base64
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from exradius import downloads
from exradius.api.app import create_app
from exradius.crypto import hash_token
from exradius.models import Artifact, GitCredential, InfoRequest, Project, Run, SecretStoreEntry, Task, TranscriptChunk
from tests.conftest import commit_config, make_config_repo


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


API_CONFIG = """\
version: 1
project: {slug: billing-platform, name: Billing, description: Invoices.}
secret_store: billing
repositories:
  - name: api
    url: https://git.example/api.git
    branch: main
    credential: ${secret:GIT_TOKEN}
workstation:
  setup: [make deps]
env:
  LOG_LEVEL: debug
connections:
  docs:
    url: https://mcp.example/mcp
    headers: {Authorization: "Bearer ${secret:DOCS_KEY}"}
  sentry:
    command: npx
    args: [-y, sentry-mcp, --read-only]
    env: {SENTRY_TOKEN: "${secret:SENTRY_TOKEN}"}
agents:
  bug-fixer:
    name: Bug fixer
    role: You fix bugs.
    identity: {email: bug-fixer@agents.example}
    connections: [docs, sentry]
"""


def setup_configuration(admin, root: Path, config: str = API_CONFIG, values: dict | None = None) -> dict:
    """A project read from a config repository under ``root``, and its store with keys."""
    remote = make_config_repo(root, {"exradius.yaml": config}, name=f"cfg-{uuid.uuid4().hex[:6]}")
    project = admin.post("/api/projects", json={"config_url": str(remote)})
    assert project.status_code == 201, project.text
    store = next((x for x in admin.get("/api/stores").json() if x["name"] == "billing"), None) \
        or admin.post("/api/stores", json={"name": "billing"}).json()
    values = values if values is not None else {"GIT_TOKEN": "ghp_secret", "DOCS_KEY": "docs-key", "SENTRY_TOKEN": "sentry"}
    for key, value in values.items():
        assert admin.put(f"/api/stores/{store['id']}/keys/{key}", json={"value": value}).status_code == 200
    return {"project": admin.get(f"/api/projects/{project.json()['id']}").json(), "store": store, "remote": remote}


def test_projects_are_added_by_url(admin, tmp_path):
    remote = make_config_repo(tmp_path, {"exradius.yaml": API_CONFIG})
    created = admin.post("/api/projects", json={"config_url": str(remote), "config_branch": " "})
    assert created.status_code == 201, created.text
    project = created.json()
    assert (project["slug"], project["name"], project["description"]) == ("billing-platform", "Billing", "Invoices.")
    assert project["config_branch"] is None and project["branch"] == "main" and project["path"] == "exradius.yaml"
    assert len(project["commit"]) == 40 and project["raw"] == API_CONFIG
    assert project["agents"] == ["bug-fixer"] and project["config"]["env"] == {"LOG_LEVEL": "debug"}
    # Nothing in the store yet.
    assert project["required_keys"] == [{"name": n, "present": False} for n in ("DOCS_KEY", "GIT_TOKEN", "SENTRY_TOKEN")]
    assert project["missing_keys"] == ["DOCS_KEY", "GIT_TOKEN", "SENTRY_TOKEN"]
    assert (project["health"], project["runnable"], project["store_exists"]) == ("missing_secrets", False, False)
    assert project["reason"] == "secret store billing does not exist"

    store = admin.post("/api/stores", json={"name": "billing"}).json()
    for key in ("DOCS_KEY", "GIT_TOKEN"):
        admin.put(f"/api/stores/{store['id']}/keys/{key}", json={"value": "v"})
    project = admin.get(f"/api/projects/{project['id']}").json()
    assert project["store_id"] == store["id"] and project["missing_keys"] == ["SENTRY_TOKEN"]
    assert project["reason"] == "missing secret SENTRY_TOKEN in store billing"
    admin.put(f"/api/stores/{store['id']}/keys/SENTRY_TOKEN", json={"value": "v"})
    project = admin.get(f"/api/projects/{project['id']}").json()
    assert (project["health"], project["runnable"], project["reason"]) == ("ok", True, None)
    assert [p["slug"] for p in admin.get("/api/projects").json()] == ["billing-platform"]

    # The same slug twice is refused.
    again = make_config_repo(tmp_path, {"exradius.yaml": API_CONFIG}, name="again")
    assert admin.post("/api/projects", json={"config_url": str(again)}).status_code == 409
    # Unreadable repositories, branches and files answer 422 with the error.
    missing = admin.post("/api/projects", json={"config_url": str(tmp_path / "nope.git")})
    assert missing.status_code == 422 and "could not read the config repository" in missing.json()["detail"][0]
    branch = admin.post("/api/projects", json={"config_url": str(again), "config_branch": "dev"})
    assert branch.json()["detail"] == ["branch 'dev' does not exist in the config repository"]
    empty = make_config_repo(tmp_path, {"README": "x"}, name="empty")
    detail = admin.post("/api/projects", json={"config_url": str(empty)}).json()["detail"]
    assert detail[0].startswith("no exradius.yaml or .exradius/exradius.yaml on branch main")
    assert not list((admin_data_dir(admin) / "config-repos").glob("*.git"))[1:]  # only the project's mirror


def admin_data_dir(admin) -> Path:
    return admin.app.state.exradius.settings.data_dir


def test_a_file_with_errors_still_creates_the_project(admin, tmp_path):
    remote = make_config_repo(tmp_path, {".exradius/exradius.yaml": API_CONFIG.replace("\nenv:", "\nenvs:")})
    project = admin.post("/api/projects", json={"config_url": str(remote)}).json()
    assert project["slug"] == "billing-platform" and project["path"] == ".exradius/exradius.yaml"
    assert project["errors"] == ["envs: unknown key"] and project["health"] == "config_errors"
    assert project["runnable"] is False and project["reason"] == "config error: envs: unknown key"
    assert project["config"] is None and project["agents"] == []

    commit_config(remote, {".exradius/exradius.yaml": API_CONFIG})
    unchanged = admin.get(f"/api/projects/{project['id']}").json()
    assert unchanged["errors"] == ["envs: unknown key"]  # the cache, until re-read
    reread = admin.post(f"/api/projects/{project['id']}/reread").json()
    assert reread["errors"] == [] and reread["commit"] != project["commit"]

    # A changed slug is an error on the project, which keeps its own.
    commit_config(remote, {".exradius/exradius.yaml": API_CONFIG.replace("slug: billing-platform", "slug: billing")})
    reread = admin.post(f"/api/projects/{project['id']}/reread").json()
    assert reread["slug"] == "billing-platform" and reread["health"] == "config_errors"
    assert reread["errors"][0].startswith("project.slug: the file says 'billing', but this project was added as 'billing-platform'")


def test_patch_branch_and_path_rereads(admin, tmp_path):
    remote = make_config_repo(tmp_path, {"exradius.yaml": API_CONFIG, "other/exradius.yaml": API_CONFIG.replace("Billing,", "Other,")})
    project = admin.post("/api/projects", json={"config_url": str(remote)}).json()
    patched = admin.patch(f"/api/projects/{project['id']}", json={"config_path": "other/exradius.yaml"}).json()
    assert patched["path"] == "other/exradius.yaml" and patched["name"] == "Other"
    patched = admin.patch(f"/api/projects/{project['id']}", json={"config_path": None, "config_branch": "nope"}).json()
    assert patched["commit"] is None and patched["health"] == "config_errors"
    assert patched["errors"] == ["branch 'nope' does not exist in the config repository"]
    assert patched["reason"].startswith("config error: branch 'nope'")


def test_delete_project(admin, tmp_path, sync_db):
    cfg = setup_configuration(admin, tmp_path)
    pid = cfg["project"]["id"]
    mirror = admin_data_dir(admin) / "config-repos" / f"{pid}.git"
    assert mirror.is_dir()
    assert admin.delete(f"/api/projects/{pid}").status_code == 204
    assert not mirror.exists() and admin.get(f"/api/projects/{pid}").status_code == 404
    _, _ = make_run(admin, sync_db, tmp_path / "again")
    pid = admin.get("/api/projects").json()[0]["id"]
    assert admin.delete(f"/api/projects/{pid}").json()["detail"] == "the project has 1 task; it cannot be deleted"


def test_stores_are_write_only_and_encrypted(admin, tmp_path, sync_db):
    cfg = setup_configuration(admin, tmp_path)
    sid = cfg["store"]["id"]
    [listed] = admin.get("/api/stores").json()
    assert listed["keys"] == ["DOCS_KEY", "GIT_TOKEN", "SENTRY_TOKEN"] and listed["used_by"] == ["billing-platform"]
    keys = admin.get(f"/api/stores/{sid}/keys").json()
    assert [set(k) for k in keys] == [{"key", "updated_at"}] * 3
    assert all("ghp_secret" not in e.value_encrypted for e in sync_db.scalars(select(SecretStoreEntry)))
    replaced = admin.put(f"/api/stores/{sid}/keys/GIT_TOKEN", json={"value": "new"})
    assert replaced.status_code == 200 and set(replaced.json()) == {"key", "updated_at"}
    assert admin.put(f"/api/stores/{sid}/keys/bad key", json={"value": "x"}).status_code == 422
    assert admin.post("/api/stores", json={"name": "billing"}).status_code == 409
    assert admin.post("/api/stores", json={"name": "Bad"}).status_code == 422

    assert admin.delete(f"/api/stores/{sid}/keys/GIT_TOKEN").status_code == 204
    assert admin.delete(f"/api/stores/{sid}/keys/GIT_TOKEN").status_code == 404
    assert admin.get(f"/api/projects/{cfg['project']['id']}").json()["missing_keys"] == ["GIT_TOKEN"]
    refused = admin.delete(f"/api/stores/{sid}")
    assert refused.status_code == 409 and refused.json()["detail"] == "the config of billing-platform names this store"
    spare = admin.post("/api/stores", json={"name": "spare"}).json()
    assert admin.delete(f"/api/stores/{spare['id']}").status_code == 204


def test_bulk_set_keys(admin, sync_db):
    sid = admin.post("/api/stores", json={"name": "bulk"}).json()["id"]
    admin.put(f"/api/stores/{sid}/keys/OLD", json={"value": "before"})
    first = admin.put(f"/api/stores/{sid}/keys", json={"values": {"OLD": "after", "NEW_ONE": "v1", "a.b-c": "v2"}})
    assert first.status_code == 200
    assert [k["key"] for k in first.json()] == ["NEW_ONE", "OLD", "a.b-c"]
    assert all(set(k) == {"key", "updated_at"} for k in first.json())
    entries = {e.key: e for e in sync_db.scalars(select(SecretStoreEntry).where(SecretStoreEntry.key.in_(["OLD", "NEW_ONE"])))}
    assert len(entries) == 2 and all("after" not in e.value_encrypted and "v1" not in e.value_encrypted for e in entries.values())


def test_bulk_set_keys_rejects_the_whole_request(admin):
    sid = admin.post("/api/stores", json={"name": "bulk"}).json()["id"]
    refused = admin.put(f"/api/stores/{sid}/keys", json={"values": {"GOOD": "v", "bad key": "x"}})
    assert refused.status_code == 422 and "'bad key'" in refused.json()["detail"]
    assert admin.put(f"/api/stores/{sid}/keys", json={"values": {"GOOD": ""}}).status_code == 422
    assert admin.put(f"/api/stores/{sid}/keys", json={"values": {}}).status_code == 422
    assert admin.get(f"/api/stores/{sid}/keys").json() == []
    assert admin.put(f"/api/stores/{uuid.uuid4()}/keys", json={"values": {"A": "v"}}).status_code == 404


def test_git_credentials_are_write_only(admin, sync_db):
    created = admin.post("/api/git-credentials", json={"host": "GitHub.com", "kind": "token", "value": "ghp_x"})
    assert created.status_code == 201 and set(created.json()) == {"id", "host", "kind", "created_at"}
    assert created.json()["host"] == "github.com"
    assert admin.post("/api/git-credentials", json={"host": "github.com", "kind": "token", "value": "y"}).status_code == 409
    assert admin.post("/api/git-credentials", json={"host": "gitlab.com", "kind": "basic", "value": "nocolon"}).status_code == 422
    assert all("ghp_x" not in c.value_encrypted for c in sync_db.scalars(select(GitCredential)))
    assert [c["host"] for c in admin.get("/api/git-credentials").json()] == ["github.com"]
    assert admin.delete(f"/api/git-credentials/{created.json()['id']}").status_code == 204
    assert admin.get("/api/git-credentials").json() == []


def test_catalog(admin):
    entries = {e["key"]: e for e in admin.get("/api/catalog").json()}
    assert set(entries) == {"github", "gitlab", "postgres", "slack", "sentry", "linear", "jira", "fetch",
                            "playwright", "filesystem"}
    assert entries["github"]["config"] == [{
        "key": "token", "label": "GitHub token", "required": True, "target": "header",
        "name": "Authorization", "format": "Bearer {}", "default": None,
    }]
    assert entries["filesystem"]["config"][0]["default"] == "/work" and entries["postgres"]["requires"] == "uvx"


def make_run(admin, sync_db, root: Path, state="running", with_token="tok") -> tuple[str, str]:
    cfg = setup_configuration(admin, root)
    project = cfg["project"]
    task = Task(id="JIRA-1", project_id=uuid.UUID(project["id"]), agent="bug-fixer",
                instructions="Fix it.", context={"k": "v"}, correlation=None, state=state)
    run = Run(id=uuid.uuid4(), task_id="JIRA-1", state=state, token_hash=hash_token(with_token) if with_token else None,
              config=project["config"], config_commit=project["commit"])
    sync_db.add_all([task, run])
    sync_db.commit()
    return "JIRA-1", str(run.id)


def test_task_endpoints_and_cancel(admin, sync_db, control, tmp_path):
    task_id, run_id = make_run(admin, sync_db, tmp_path)
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


def test_cancel_finished_task_conflicts(admin, sync_db, tmp_path):
    task_id, _ = make_run(admin, sync_db, tmp_path, state="completed", with_token=None)
    assert admin.post(f"/api/tasks/{task_id}/cancel").status_code == 409


def test_internal_api_requires_the_run_token(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    base = f"/internal/runs/{run_id}"
    assert client.get(f"{base}/spec").status_code == 401
    assert client.get(f"{base}/spec", headers={"Authorization": "Bearer wrong"}).status_code == 401
    other = f"/internal/runs/{uuid.uuid4()}/spec"
    assert client.get(other, headers={"Authorization": "Bearer tok"}).status_code == 401


def test_internal_spec_resolves_connections_and_secrets(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    auth = {"Authorization": "Bearer tok"}
    spec = client.get(f"/internal/runs/{run_id}/spec", headers=auth).json()
    assert spec["mode"] == "start" and spec["runtime"] == "claude_code"
    assert spec["agent_prompt"] == "You fix bugs." and spec["context"] == {"k": "v"}
    assert spec["environment_text"].startswith("# Your environment\n\nYou run unattended in a Linux container built from the image exradius-project-default")
    by_name = {c["name"]: c for c in spec["connections"]}
    assert by_name["docs"]["headers"] == {"Authorization": "Bearer docs-key"}
    assert by_name["sentry"] == {"name": "sentry", "transport": "stdio", "command": "npx",
                                 "args": ["-y", "sentry-mcp", "--read-only"], "url": None,
                                 "env": {"SENTRY_TOKEN": "sentry"}, "headers": {}}


def test_internal_spec_uses_the_runs_config_not_the_projects(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    project = sync_db.scalar(select(Project))
    commit_config(Path(project.config_url), {"exradius.yaml": API_CONFIG.replace("You fix bugs.", "Changed.")})
    admin.post(f"/api/projects/{project.id}/reread")
    spec = client.get(f"/internal/runs/{run_id}/spec", headers={"Authorization": "Bearer tok"}).json()
    assert spec["agent_prompt"] == "You fix bugs."


def test_internal_run_reporting(client, admin, sync_db, store, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
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


def test_downloads_accept_admin_session_or_signature(client, admin, sync_db, store, settings, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
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


def test_artifact_names_cannot_escape(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    bad = [{"name": "../../etc/x", "content_base64": base64.b64encode(b"x").decode()}]
    response = client.post(
        f"/internal/runs/{run_id}/result", json={"output": None, "artifacts": bad},
        headers={"Authorization": "Bearer tok"},
    )
    assert response.status_code == 422


def test_internal_resume_spec(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
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


def test_token_is_rejected_once_run_ended(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path, state="completed")
    assert client.get(f"/internal/runs/{run_id}/spec", headers={"Authorization": "Bearer tok"}).status_code == 401


