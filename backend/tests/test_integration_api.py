"""The config hook, the directory, its notice and integration tokens (docs/integration-api.md).

The broker is ``FakeControl`` (tests/test_api.py): ``control.directory`` holds every
``directory.changed`` notice the API published."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from exradius import directory
from exradius.api import hooks
from exradius.api.app import create_app
from exradius.api.hooks import Rereads
from exradius.config_repo import read_snapshot
from exradius.models import DirectoryState, IntegrationToken, Project, utcnow
from exradius.project_state import remember
from tests.conftest import BASE_CONFIG, commit_config, make_config_repo
from exradius.worker.verify import ProjectVerifier
from tests.test_api import admin, client, control, sync_db  # noqa: F401 - fixtures
from tests.test_run_manager import containers, manager, published, submit_task  # noqa: F401 - fixtures
from tests.test_verification import new_verification

PLANE = """\
version: 1
project: {slug: plane, name: Plane}
secret_store: plane
env:
  API_KEY: ${secret:API_KEY}
agents:
  scout:
    role: You investigate.
  developer:
    name: Developer
    role: You develop.
"""
"""Agents out of slug order, one without a name; a required key, ``API_KEY``."""

PLANE_DIRECTORY = {"slug": "plane", "name": "Plane", "runnable": False, "repositories": [], "agents": [
    {"slug": "developer", "name": "Developer"}, {"slug": "scout", "name": "Scout"},
]}


def wait_for(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def login(client) -> None:
    assert client.post("/api/auth/login", json={"username": "admin", "password": "correct horse"}).status_code == 200


def add_plane(admin, tmp_path, config: str = PLANE) -> tuple[dict, object]:
    remote = make_config_repo(tmp_path, {"exradius.yaml": config}, name=f"plane-{uuid.uuid4().hex[:6]}")
    created = admin.post("/api/projects", json={"config_url": str(remote)})
    assert created.status_code == 201, created.text
    return created.json(), remote


def plane_store(admin) -> dict:
    return admin.post("/api/stores", json={"name": "plane"}).json()


def notices(control) -> list[list[str]]:
    return [n["payload"]["projects"] for n in control.directory]


def new_token(admin, name: str = "jira") -> str:
    created = admin.post("/api/integration-tokens", json={"name": name})
    assert created.status_code == 201, created.text
    return created.json()["token"]


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# The config hook (§2) ---------------------------------------------------------------------


def test_hook_url_and_unknown_token(admin, settings, tmp_path):
    project, _ = add_plane(admin, tmp_path)
    assert project["hook_url"].startswith(f"{settings.public_api_url}/api/hooks/config/")
    token = project["hook_url"].rsplit("/", 1)[1]
    assert len(token) >= 43 and project["hook_last_at"] is None
    assert admin.post("/api/hooks/config/not-a-token").status_code == 404
    assert admin.get(f"/api/projects/{project['id']}").json()["hook_last_at"] is None


def test_hook_rereads_in_the_background(admin, control, settings, tmp_path):
    project, remote = add_plane(admin, tmp_path)
    path = project["hook_url"].removeprefix(settings.public_api_url)
    commit = commit_config(remote, {"exradius.yaml": PLANE.replace("You develop.", "You develop well.")})

    admin.post("/api/auth/logout")  # the hook needs no session, and takes any body and headers
    answer = admin.post(path, content=b'{"ref": "refs/heads/main"}',
                        headers={"content-type": "application/json", "X-GitHub-Event": "push"})
    assert answer.status_code == 202 and answer.content == b""
    assert admin.post(path, content=b"payload=whatever", headers={"content-type": "text/plain"}).status_code == 202
    assert admin.get("/api/projects").status_code == 401

    login(admin)
    read = wait_for(lambda: (p := admin.get(f"/api/projects/{project['id']}").json())["commit"] == commit and p)
    assert read["hook_last_at"] is not None
    admin.portal.call(admin.app.state.exradius.rereads.idle)
    assert notices(control) == [["plane"]]  # only the add: a role is not in the directory


def test_replacing_the_hook_token(admin, settings, tmp_path):
    project, _ = add_plane(admin, tmp_path)
    old = project["hook_url"].removeprefix(settings.public_api_url)
    replaced = admin.post(f"/api/projects/{project['id']}/hook-token")
    assert replaced.status_code == 200
    new = replaced.json()["hook_url"].removeprefix(settings.public_api_url)
    assert new != old
    assert admin.post(old).status_code == 404
    assert admin.post(new).status_code == 202
    assert admin.get(f"/api/projects/{project['id']}").json()["hook_url"].endswith(new)
    admin.post("/api/auth/logout")
    assert admin.post(f"/api/projects/{project['id']}/hook-token").status_code == 401


async def test_rereads_coalesce_a_burst():
    started, release = asyncio.Event(), asyncio.Event()
    reads: list[uuid.UUID] = []

    async def read(project_id: uuid.UUID) -> None:
        reads.append(project_id)
        started.set()
        await release.wait()

    rereads = Rereads(read)
    plane, other = uuid.uuid4(), uuid.uuid4()
    rereads.request(plane)
    await started.wait()
    for _ in range(10):  # a burst of pushes while the first read runs
        rereads.request(plane)
    rereads.request(other)  # another project is not held up by it
    await asyncio.sleep(0)
    assert reads == [plane, other]
    release.set()
    await rereads.idle()
    assert reads == [plane, other, plane]  # one more read, not ten
    assert not rereads.running(plane)

    rereads.request(plane)  # a later push reads again
    await rereads.idle()
    assert reads.count(plane) == 3


async def test_a_failed_read_does_not_stop_the_next():
    reads = []

    async def read(project_id):
        reads.append(project_id)
        if len(reads) == 1:
            raise RuntimeError("git host down")

    rereads = Rereads(read)
    plane = uuid.uuid4()
    rereads.request(plane)
    rereads.request(plane)  # due after the first
    await rereads.idle()
    assert reads == [plane, plane]


def test_hook_burst_through_the_api(admin, settings, tmp_path, monkeypatch):
    """Calls during a read queue one more read; the rest are absorbed."""
    project, _ = add_plane(admin, tmp_path)
    path = project["hook_url"].removeprefix(settings.public_api_url)
    gate: dict = {}
    calls = []

    async def slow_reread(session, state, row):
        calls.append(row.id)
        gate.setdefault("open", asyncio.Event())
        await gate["open"].wait()

    monkeypatch.setattr(hooks, "_reread", slow_reread)
    assert admin.post(path).status_code == 202
    wait_for(lambda: calls)
    for _ in range(5):
        assert admin.post(path).status_code == 202
    assert len(calls) == 1
    admin.portal.call(lambda: gate["open"].set())
    admin.portal.call(admin.app.state.exradius.rereads.idle)
    assert len(calls) == 2


# The directory notice (§3) ----------------------------------------------------------------


def test_notices_follow_the_directory(admin, control, tmp_path):
    project, remote = add_plane(admin, tmp_path)
    assert notices(control) == [["plane"]]
    [notice] = control.directory
    assert set(notice) == {"version", "type", "timestamp", "payload"}  # no task fields
    assert (notice["version"], notice["type"]) == (1, "directory_changed")
    assert notice["timestamp"].endswith("Z")

    def reread():
        assert admin.post(f"/api/projects/{project['id']}/reread").status_code == 200

    commit_config(remote, {"exradius.yaml": PLANE.replace("You investigate.", "You dig.")})
    reread()
    assert len(control.directory) == 1  # a role is not in the directory

    commit_config(remote, {"exradius.yaml": PLANE + "  reviewer:\n    role: You review.\n"})
    reread()
    assert notices(control) == [["plane"], ["plane"]]  # a new agent
    reread()
    assert len(control.directory) == 2  # nothing changed

    store = plane_store(admin)
    assert len(control.directory) == 2  # the key is still missing
    assert admin.put(f"/api/stores/{store['id']}/keys/API_KEY", json={"value": "k"}).status_code == 200
    assert len(control.directory) == 3  # runnable flipped
    assert admin.put(f"/api/stores/{store['id']}/keys", json={"values": {"API_KEY": "k2", "OTHER": "x"}}).status_code == 200
    assert len(control.directory) == 3  # still runnable
    assert admin.delete(f"/api/stores/{store['id']}/keys/API_KEY").status_code == 204
    assert len(control.directory) == 4  # not runnable again

    second, _ = add_plane(admin, tmp_path, PLANE.replace("slug: plane", "slug: saleor").replace("name: Plane", "name: Saleor"))
    assert notices(control)[-1] == ["saleor"]
    assert admin.delete(f"/api/projects/{second['id']}").status_code == 204
    assert notices(control)[-1] == ["saleor"]
    assert len(control.directory) == 6


def test_failed_notice_is_published_by_the_next_check(admin, control, tmp_path):
    project, _ = add_plane(admin, tmp_path)
    store = plane_store(admin)
    control.healthy = False
    assert admin.put(f"/api/stores/{store['id']}/keys/API_KEY", json={"value": "k"}).status_code == 200
    assert notices(control) == [["plane"]]  # not published, and not stored either
    control.healthy = True
    assert admin.post(f"/api/projects/{project['id']}/reread").status_code == 200  # same commit
    assert notices(control) == [["plane"], ["plane"]]


async def test_check_stores_only_what_it_published(sessions, configured):
    published = []

    async def down(event):
        raise ConnectionError("broker down")

    async def up(event):
        published.append(event)

    assert await directory.check(sessions, down) is False
    async with sessions() as session:
        state = await session.get(DirectoryState, directory.STATE_ID)
        assert state is None or state.directory is None

    assert await directory.check(sessions, up) is True
    [event] = published
    assert (event.routing_key, event.payload.projects) == ("directory.changed", ["billing-platform"])
    async with sessions() as session:
        state = await session.get(DirectoryState, directory.STATE_ID)
        assert state.directory == await directory.build(session) and state.published_at is not None
    assert await directory.check(sessions, up) is False  # unchanged
    assert len(published) == 1


def test_startup_check(settings, store, control, sync_db, tmp_path):
    remote = make_config_repo(tmp_path, {"exradius.yaml": PLANE})
    project = Project(id=uuid.uuid4(), config_url=str(remote), slug="plane")
    remember(project, read_snapshot(settings.data_dir, project.id, project.config_url))
    sync_db.add(project)
    sync_db.commit()

    for _ in range(2):  # the second start finds the directory as published
        app = create_app(settings, store=store, broker=control)
        with TestClient(app) as c:
            c.portal.call(_finished, app.state.directory_startup_check)
    assert notices(control) == [["plane"]]


async def _finished(task: asyncio.Task) -> None:
    await task


# The directory (§4) and integration tokens (§5) -----------------------------------------------


def test_directory(admin, client, tmp_path):
    add_plane(admin, tmp_path, PLANE.replace("slug: plane", "slug: zeta").replace("name: Plane", "name: Zeta"))
    add_plane(admin, tmp_path)
    broken, remote = add_plane(admin, tmp_path, PLANE.replace("slug: plane", "slug: broken"))
    commit_config(remote, {"exradius.yaml": PLANE.replace("slug: plane", "slug: broken") + "notez: x\n"})
    admin.post(f"/api/projects/{broken['id']}/reread")
    token = new_token(admin)

    answer = client.get("/api/integration/v1/directory", headers=bearer(token))
    assert answer.status_code == 200 and answer.headers["content-type"] == "application/json"
    assert answer.json() == {"projects": [
        {"slug": "broken", "name": "broken", "runnable": False, "repositories": [], "agents": []},
        PLANE_DIRECTORY,
        {**PLANE_DIRECTORY, "slug": "zeta", "name": "Zeta"},
    ]}
    assert "role" not in answer.text and "commit" not in answer.text and "reason" not in answer.text


REPOS = """\
repositories:
  - name: web
    url: https://x-access-token:ghp_secret@github.com/acme/plane-web.git
    branch: main
  - name: api
    url: git@github.com:acme/plane-api.git
"""


def test_directory_lists_repositories(admin, client, control, tmp_path):
    project, remote = add_plane(admin, tmp_path, PLANE + REPOS)
    headers = bearer(new_token(admin))
    [entry] = client.get("/api/integration/v1/directory", headers=headers).json()["projects"]
    assert list(entry) == ["slug", "name", "runnable", "repositories", "agents"]
    assert entry["repositories"] == [  # config order, without userinfo
        {"name": "web", "url": "https://github.com/acme/plane-web.git"},
        {"name": "api", "url": "git@github.com:acme/plane-api.git"},
    ]
    assert "ghp_secret" not in client.get("/api/integration/v1/directory", headers=headers).text

    def reread():
        assert admin.post(f"/api/projects/{project['id']}/reread").status_code == 200

    sent = len(control.directory)
    commit_config(remote, {"exradius.yaml": PLANE + REPOS.replace("branch: main", "branch: develop")})
    reread()
    assert len(control.directory) == sent  # a branch is not in the directory
    commit_config(remote, {"exradius.yaml": PLANE + REPOS.replace("acme/plane-api.git", "acme/plane-backend.git")})
    reread()
    assert len(control.directory) == sent + 1  # re-pointed
    assert notices(control)[-1] == ["plane"]
    commit_config(remote, {"exradius.yaml": PLANE + REPOS.split("  - name: api")[0]})
    reread()
    assert len(control.directory) == sent + 2  # removed


@pytest.mark.parametrize(("url", "listed"), [
    ("https://user:token@github.com/acme/plane.git", "https://github.com/acme/plane.git"),
    ("https://ghp_token@gitlab.example.com:8443/acme/plane.git?x=1", "https://gitlab.example.com:8443/acme/plane.git?x=1"),
    ("ssh://git@github.com/acme/plane.git", "ssh://github.com/acme/plane.git"),
    ("https://github.com/acme/plane.git", "https://github.com/acme/plane.git"),
    ("https://github.com/acme/pl@ne.git", "https://github.com/acme/pl@ne.git"),  # an @ in the path stays
    ("git@github.com:acme/plane.git", "git@github.com:acme/plane.git"),  # scp-like: no password possible
])
def test_repository_urls_lose_userinfo_only(url, listed):
    assert directory.without_userinfo(url) == listed


def test_directory_etag(admin, client, tmp_path):
    project, remote = add_plane(admin, tmp_path)
    headers = bearer(new_token(admin))
    first = client.get("/api/integration/v1/directory", headers=headers)
    tag = first.headers["etag"]
    assert tag == f'"{hashlib.sha256(first.content).hexdigest()}"'

    for value in (tag, f"W/{tag}", f'"other", {tag}', "*"):
        cached = client.get("/api/integration/v1/directory", headers={**headers, "If-None-Match": value})
        assert cached.status_code == 304 and cached.content == b"" and cached.headers["etag"] == tag
    assert client.get("/api/integration/v1/directory", headers={**headers, "If-None-Match": '"other"'}).status_code == 200

    commit_config(remote, {"exradius.yaml": PLANE + "  reviewer:\n    role: You review.\n"})
    admin.post(f"/api/projects/{project['id']}/reread")
    changed = client.get("/api/integration/v1/directory", headers={**headers, "If-None-Match": tag})
    assert changed.status_code == 200 and changed.headers["etag"] != tag
    assert [a["slug"] for a in changed.json()["projects"][0]["agents"]] == ["developer", "reviewer", "scout"]


def test_integration_tokens_are_refused_unless_valid(admin, client, sync_db, tmp_path):
    add_plane(admin, tmp_path)
    url = "/api/integration/v1/directory"
    token = new_token(admin)
    for headers in ({}, {"Authorization": token}, {"Authorization": "Basic YWRtaW46eA=="}, bearer(""),
                    bearer("exr_wrong"), bearer(token + "x")):
        refused = client.get(url, headers=headers)  # the admin session does not count either
        assert refused.status_code == 401 and refused.headers["www-authenticate"] == "Bearer", headers
    assert client.get(f"{url}?sig=whatever").status_code == 401
    assert client.get(url, headers=bearer(token)).status_code == 200

    [row] = admin.get("/api/integration-tokens").json()
    assert admin.delete(f"/api/integration-tokens/{row['id']}").status_code == 204
    refused = client.get(url, headers=bearer(token))
    assert refused.status_code == 401 and refused.json()["detail"] == "the integration token was revoked"
    assert admin.delete(f"/api/integration-tokens/{row['id']}").status_code == 204  # already revoked
    assert admin.delete(f"/api/integration-tokens/{uuid.uuid4()}").status_code == 404


def test_tokens_are_not_admin_credentials(admin, client):
    token = new_token(admin)
    admin.post("/api/auth/logout")
    assert client.get("/api/projects", headers=bearer(token)).status_code == 401
    assert client.get("/api/integration-tokens", headers=bearer(token)).status_code == 401
    assert client.post("/api/integration-tokens", json={"name": "x"}).status_code == 401


def test_token_management(admin, sync_db):
    created = admin.post("/api/integration-tokens", json={"name": "  jira "})
    assert created.status_code == 201
    body = created.json()
    assert body["name"] == "jira" and body["token"].startswith("exr_") and len(body["token"]) == 4 + 43
    assert body["last_used_at"] is None and body["revoked_at"] is None
    row = sync_db.scalar(select(IntegrationToken))
    assert row.token_hash == hashlib.sha256(body["token"].encode()).hexdigest()  # only the hash is stored

    assert admin.post("/api/integration-tokens", json={"name": "jira"}).status_code == 409
    assert admin.post("/api/integration-tokens", json={"name": " "}).status_code == 422
    other = new_token(admin, "slack")
    assert other != body["token"]
    listed = admin.get("/api/integration-tokens")
    assert [t["name"] for t in listed.json()] == ["jira", "slack"]
    assert body["token"] not in listed.text and row.token_hash not in listed.text
    assert all("token" not in t and "token_hash" not in t for t in listed.json())


def test_last_used_is_written_at_most_once_a_minute(admin, client, sync_db):
    token = new_token(admin)
    url = "/api/integration/v1/directory"
    assert client.get(url, headers=bearer(token)).status_code == 200
    first = admin.get("/api/integration-tokens").json()[0]["last_used_at"]
    assert first is not None
    assert client.get(url, headers=bearer(token)).status_code == 200
    assert admin.get("/api/integration-tokens").json()[0]["last_used_at"] == first

    sync_db.execute(update(IntegrationToken).values(last_used_at=utcnow() - timedelta(minutes=2)))
    sync_db.commit()
    assert client.get(url, headers=bearer(token)).status_code == 200
    later = admin.get("/api/integration-tokens").json()[0]["last_used_at"]
    assert later != first and later > first


# The worker (§3): intake, preparing and Verify check too ---------------------------------------


@pytest.mark.parametrize(("project", "agent", "read"), [
    ("billing-platform", "bug-fixer", True),   # accepted
    ("billing-platform", "nobody", True),      # rejected after the read
    ("unknown", "bug-fixer", False),           # rejected before any read
])
async def test_intake_says_whether_it_read_the_config(accept, configured, project, agent, read):
    body = {"version": 1, "task_id": f"T-{uuid.uuid4().hex[:6]}", "project": project, "agent": agent,
            "instructions": "Fix it."}
    assert (await accept(json.dumps(body).encode())).config_read is read


async def test_preparing_and_verify_check_the_directory(manager, sessions, published, configured, accept):
    """A config that changed after intake is noticed when ``preparing`` or Verify reads it."""
    sent = []

    async def check():
        await directory.check(sessions, lambda event: _append(sent, event))

    manager.check_directory = check
    assert await directory.check(sessions, lambda event: _append(sent, event)) is True  # the baseline
    run_id = await submit_task(accept, published)
    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + "  reviewer:\n    role: You review.\n"})
    await manager.execute(run_id)  # the fake containers end the run; preparing read the config first
    assert [e.payload.projects for e in sent] == [["billing-platform"], ["billing-platform"]]

    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG})
    verification = await new_verification(sessions, configured["project"].id)
    await ProjectVerifier(manager).run(verification)
    assert len(sent) == 3


async def _append(sent: list, event) -> None:
    sent.append(event)


async def test_notice_message_on_the_wire():
    """What ``messaging.publish_directory`` hands to the exchange (no broker needed)."""
    from exradius import messaging
    from exradius.schemas.messages import DirectoryChangedPayload, DirectoryEvent

    class Exchange:
        sent = []

        async def publish(self, message, routing_key):
            self.sent.append((message, routing_key))

    exchange = Exchange()
    await messaging.EventPublisher(exchange).publish_directory(
        DirectoryEvent(payload=DirectoryChangedPayload(projects=["plane"]))
    )
    [(message, key)] = exchange.sent
    assert key == "directory.changed"
    assert (message.content_type, message.type) == ("application/json", "directory_changed")
    body = json.loads(message.body)
    assert list(body) == ["version", "type", "timestamp", "payload"]
    assert body["payload"] == {"projects": ["plane"]} and body["timestamp"].endswith("Z")
