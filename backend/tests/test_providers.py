"""Model providers (docs/providers.md): API rules, injection, intake, tests, migration and
the admin password."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from exradius.api import provider_routes
from exradius.api.app import create_app
from exradius.models import Agent, Provider, ProviderTest, Secret
from exradius.providers import MIGRATED_NAME, bootstrap_provider, fingerprint
from exradius.runner import main as runner_main
from exradius.runner.environment import RunnerEnvironment
from exradius.runtimes import AuthMethod, ProbeResult, ProviderCredential, ProviderKind, RuntimeCapabilities, SettingField
from exradius.runtimes.base import PROBE_PREFIX
from exradius.runtimes.claude_code import OAUTH_NOTICE, ClaudeCodeRuntime
from exradius.worker.containers import ContainerExit
from exradius.worker.intake import accept_task
from exradius.worker.provider_test import ProviderTester, parse_probe
from tests.test_api import admin, client, control, sync_db  # noqa: F401 - fixtures
from tests.test_run_manager import containers, manager, published, runner, states, submit_task  # noqa: F401

API_KEY = {"slug": "anthropic", "name": "Anthropic", "kind": "anthropic", "auth_method": "api_key", "credential": "sk-ant-1"}
OAUTH = {"slug": "subscription", "name": "Subscription", "kind": "anthropic", "auth_method": "oauth_token",
         "credential": "sk-ant-oat-1"}


def create(admin, **fields):
    response = admin.post("/api/providers", json={**API_KEY, **fields})
    assert response.status_code == 201, response.text
    return response.json()


def providers(admin) -> dict[str, dict]:
    return {p["slug"]: p for p in admin.get("/api/providers").json()}


@pytest.fixture
def anthropic_api():
    """Answers ``GET /v1/models``: 200 for ``sk-good``, 401 otherwise."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers["x-api-key"] != "sk-good":
            return httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
        models = [{"id": f"claude-model-{i}", "type": "model"} for i in range(7)]
        return httpx.Response(200, json={"data": models, "has_more": False})

    provider_routes.TRANSPORT = httpx.MockTransport(handler)
    yield seen
    provider_routes.TRANSPORT = None


# Runtime capability ------------------------------------------------------------------------


def test_runtimes_declare_their_providers(admin):
    assert admin.get("/api/system/runtimes").json() == [
        {
            "name": "claude_code",
            "label": "Claude Code",
            "providers": [
                {
                    "kind": "anthropic",
                    "label": "Anthropic",
                    "auth_methods": [
                        {"name": "api_key", "label": "API key", "credential_label": "API key", "settings": [], "notice": None},
                        {"name": "oauth_token", "label": "Claude subscription token (claude setup-token)",
                         "credential_label": "OAuth token", "settings": [], "notice": OAUTH_NOTICE},
                    ],
                    "models": [
                        {"id": "claude-opus-5-5", "description": "Most capable; best for complex, long-running engineering work."},
                        {"id": "claude-sonnet-5-5", "description": "Balanced speed and capability for everyday coding tasks."},
                        {"id": "claude-haiku-5-5", "description": "Fastest and cheapest; good for small, well-defined tasks."},
                    ],
                }
            ],
        }
    ]
    assert OAUTH_NOTICE == (
        "Anthropic restricts subscription tokens to personal use of Claude Code; products built on the "
        "Agent SDK should use an API key."
    )


def test_claude_code_injection_mapping():
    env = ClaudeCodeRuntime.provider_environment
    assert env(ProviderCredential("anthropic", "api_key", "sk-1")) == {"ANTHROPIC_API_KEY": "sk-1"}
    assert env(ProviderCredential("anthropic", "oauth_token", "oat-1")) == {"CLAUDE_CODE_OAUTH_TOKEN": "oat-1"}
    with pytest.raises(ValueError):
        env(ProviderCredential("anthropic_bedrock", "keys", "x"))


# CRUD and the model rules --------------------------------------------------------------------


def test_create_list_and_write_only_credential(admin, sync_db, box):
    first = create(admin)
    assert first["is_default"] is True  # the first one becomes the default
    assert "credential" not in first and "credential_encrypted" not in first
    assert (first["kind"], first["auth_method"], first["settings"], first["last_test"]) == ("anthropic", "api_key", {}, None)
    second = create(admin, **OAUTH)
    assert second["is_default"] is False
    stored = sync_db.scalar(select(Provider).where(Provider.slug == "anthropic"))
    assert stored.credential_encrypted != "sk-ant-1" and box.decrypt(stored.credential_encrypted) == "sk-ant-1"
    assert admin.get(f"/api/providers/{first['id']}").json()["name"] == "Anthropic"
    assert admin.post("/api/providers", json=API_KEY).status_code == 409  # duplicate slug


def test_create_validates_against_runtime_capabilities(admin, monkeypatch):
    assert admin.post("/api/providers", json={**API_KEY, "kind": "openai"}).status_code == 422
    assert admin.post("/api/providers", json={**API_KEY, "auth_method": "password"}).status_code == 422
    response = admin.post("/api/providers", json={**API_KEY, "settings": {"region": "eu"}})
    assert response.status_code == 422 and "unknown setting 'region'" in response.json()["detail"]
    assert admin.post("/api/providers", json={**API_KEY, "credential": ""}).status_code == 422


def test_exactly_one_default(admin, sync_db):
    first, second, third = create(admin), create(admin, slug="b", name="B"), create(admin, slug="c", name="C")
    assert admin.post(f"/api/providers/{third['id']}/default").status_code == 204
    assert {slug: p["is_default"] for slug, p in providers(admin).items()} == {"anthropic": False, "b": False, "c": True}
    assert admin.post(f"/api/providers/{third['id']}/default").status_code == 204  # idempotent
    assert sync_db.query(Provider).filter(Provider.is_default.is_(True)).count() == 1
    assert admin.post(f"/api/providers/{uuid.uuid4()}/default").status_code == 404


def test_delete_refusals(admin):
    default, other = create(admin), create(admin, **OAUTH)
    agent = admin.post("/api/agents", json={"slug": "a", "name": "A", "identity_email": "a@agents.example", "provider_id": other["id"]}).json()

    refused = admin.delete(f"/api/providers/{other['id']}")
    assert refused.status_code == 409 and "used by agents a" in refused.json()["detail"]
    refused = admin.delete(f"/api/providers/{default['id']}")
    assert refused.status_code == 409 and "default" in refused.json()["detail"]

    admin.patch(f"/api/agents/{agent['id']}", json={"provider_id": None})  # back to the default
    assert admin.delete(f"/api/providers/{other['id']}").status_code == 204
    # The default can go once it is the only provider.
    assert admin.delete(f"/api/providers/{default['id']}").status_code == 204
    assert admin.get("/api/providers").json() == []


def test_agent_count_includes_agents_on_the_default(admin):
    default, other = create(admin), create(admin, **OAUTH)
    admin.post("/api/agents", json={"slug": "a", "name": "A", "identity_email": "a@agents.example"})
    admin.post("/api/agents", json={"slug": "b", "name": "B", "identity_email": "b@agents.example", "provider_id": other["id"]})
    admin.post("/api/agents", json={"slug": "c", "name": "C", "identity_email": "c@agents.example", "provider_id": default["id"]})
    counts = {slug: p["agent_count"] for slug, p in providers(admin).items()}
    assert counts == {"anthropic": 2, "subscription": 1}


def test_last_test_is_cleared_when_credential_or_settings_change(admin, anthropic_api, monkeypatch):
    provider = create(admin, credential="sk-good")
    pid = provider["id"]
    assert admin.post(f"/api/providers/{pid}/test").json()["ok"] is True
    assert providers(admin)["anthropic"]["last_test"]["ok"] is True

    # Renames keep the result.
    admin.patch(f"/api/providers/{pid}", json={"name": "Renamed", "slug": "renamed"})
    assert providers(admin)["renamed"]["last_test"]["ok"] is True
    # A new credential clears it.
    admin.patch(f"/api/providers/{pid}", json={"credential": "sk-new"})
    assert providers(admin)["renamed"]["last_test"] is None

    # So does a change of settings (with a capability that declares one).
    admin.patch(f"/api/providers/{pid}", json={"credential": "sk-good"})
    admin.post(f"/api/providers/{pid}/test")
    with_region = RuntimeCapabilities(providers=(
        ProviderKind("anthropic", "Anthropic", (
            AuthMethod("api_key", "API key", "API key", settings=(SettingField("region", "Region", required=False),)),
        )),
    ))
    monkeypatch.setattr(ClaudeCodeRuntime, "capabilities", with_region)
    assert admin.patch(f"/api/providers/{pid}", json={"name": "Again"}).json()["last_test"]["ok"] is True
    changed = admin.patch(f"/api/providers/{pid}", json={"settings": {"region": "eu"}}).json()
    assert changed["settings"] == {"region": "eu"} and changed["last_test"] is None


def test_changing_auth_method_needs_a_new_credential(admin):
    pid = create(admin)["id"]
    response = admin.patch(f"/api/providers/{pid}", json={"auth_method": "oauth_token"})
    assert response.status_code == 422
    changed = admin.patch(f"/api/providers/{pid}", json={"auth_method": "oauth_token", "credential": "oat"}).json()
    assert changed["auth_method"] == "oauth_token"


# Tests --------------------------------------------------------------------------------------


def test_api_key_test(admin, anthropic_api):
    good = create(admin, credential="sk-good")
    result = admin.post(f"/api/providers/{good['id']}/test")
    assert result.status_code == 200
    assert result.json() == {
        "ok": True,
        "detail": "models: claude-model-0, claude-model-1, claude-model-2, claude-model-3, claude-model-4 and 2 more",
        "error": None,
    }
    request = anthropic_api[-1]
    assert request.url.path == "/v1/models" and request.headers["anthropic-version"] == "2023-06-01"
    stored = providers(admin)["anthropic"]["last_test"]
    assert stored["ok"] is True and stored["detail"].startswith("models: ") and stored["tested_at"]

    bad = create(admin, slug="bad", name="Bad", credential="sk-bad")
    result = admin.post(f"/api/providers/{bad['id']}/test").json()
    assert result == {"ok": False, "detail": None, "error": "HTTP 401: invalid x-api-key"}
    assert providers(admin)["bad"]["last_test"]["error"] == "HTTP 401: invalid x-api-key"


def test_oauth_test_is_handed_to_a_worker(admin, control, sync_db):
    provider = create(admin, **OAUTH)
    response = admin.post(f"/api/providers/{provider['id']}/test")
    assert response.status_code == 202
    test_id = response.json()["test_id"]
    assert control.provider_tests == [test_id]
    test = admin.get(f"/api/providers/{provider['id']}/tests/{test_id}").json()
    assert (test["state"], test["provider_id"], test["detail"], test["error"]) == ("pending", provider["id"], None, None)
    assert admin.get(f"/api/providers/{uuid.uuid4()}/tests/{test_id}").status_code == 404

    control.healthy = False
    response = admin.post(f"/api/providers/{provider['id']}/test")
    assert response.status_code == 503
    failed = sync_db.scalars(select(ProviderTest).where(ProviderTest.state == "failed")).all()
    assert len(failed) == 1 and "broker" in failed[0].error


# Agents and status ----------------------------------------------------------------------------


def test_agent_provider_id(admin):
    provider = create(admin)
    agent = admin.post("/api/agents", json={"slug": "a", "name": "A", "identity_email": "a@agents.example"}).json()
    assert agent["provider_id"] is None
    assert admin.post("/api/agents", json={"slug": "b", "name": "B", "identity_email": "b@agents.example", "provider_id": str(uuid.uuid4())}).status_code == 422
    updated = admin.patch(f"/api/agents/{agent['id']}", json={"provider_id": provider["id"]}).json()
    assert updated["provider_id"] == provider["id"]
    assert admin.get(f"/api/agents/{agent['id']}").json()["provider_id"] == provider["id"]
    assert admin.patch(f"/api/agents/{agent['id']}", json={"name": "A2"}).json()["provider_id"] == provider["id"]


def test_status_shows_the_default_provider(admin, anthropic_api):
    provider = create(admin, credential="sk-good")
    create(admin, **OAUTH)
    admin.post(f"/api/providers/{provider['id']}/test")
    status = admin.get("/api/system/status").json()["provider"]
    assert status["configured"] is True and status["count"] == 2
    default = status["default"]
    assert (default["id"], default["name"], default["kind"], default["auth_method"]) == (
        provider["id"], "Anthropic", "anthropic", "api_key",
    )
    assert default["last_test"]["ok"] is True


# Injection and intake -------------------------------------------------------------------------


async def test_api_key_provider_is_injected(manager, sessions, store, containers, published, configured):
    async with sessions() as session:
        project_id = configured["project"].id
        # A project secret with a provider variable's name does not reach the run.
        session.add(Secret(project_id=project_id, key="ANTHROPIC_API_KEY", value_encrypted=manager.box.encrypt("sk-project")))
        session.add(Secret(project_id=project_id, key="CLAUDE_CODE_OAUTH_TOKEN", value_encrypted=manager.box.encrypt("oat-project")))
        await session.commit()
    run_id = await submit_task(sessions, store, published)
    containers.behaviours.append(runner(sessions))
    await manager.execute(run_id)
    env = containers.started[0]["env"]
    assert env["ANTHROPIC_API_KEY"] == "sk-test"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


async def test_agents_own_oauth_provider_is_injected(manager, sessions, store, containers, published, configured, box):
    async with sessions() as session:
        oauth = Provider(slug="sub", name="Sub", kind="anthropic", auth_method="oauth_token",
                         credential_encrypted=box.encrypt("oat-secret"), settings={})
        session.add(oauth)
        await session.flush()
        (await session.get(Agent, configured["agent"].id)).provider_id = oauth.id
        await session.commit()
    run_id = await submit_task(sessions, store, published)
    containers.behaviours.append(runner(sessions))
    await manager.execute(run_id)
    env = containers.started[0]["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "oat-secret"
    assert "ANTHROPIC_API_KEY" not in env
    assert states(published)[-1] == "completed"


async def test_intake_rejects_without_providers(sessions, store, configured):
    async with sessions() as session:
        await session.delete(await session.get(Provider, configured["provider"].id))
        await session.commit()
    body = {"version": 1, "task_id": "T-np", "project": "billing-platform", "agent": "bug-fixer", "instructions": "x"}
    intake = await accept_task(sessions, store, json.dumps(body).encode())
    assert intake.run_id is None
    [event] = intake.events
    assert event.envelope.payload["state"] == "rejected"
    assert event.envelope.payload["reason"] == "no provider configured"


async def test_run_fails_if_the_provider_is_gone_at_container_start(manager, sessions, store, containers, published, configured):
    run_id = await submit_task(sessions, store, published)
    async with sessions() as session:
        await session.delete(await session.get(Provider, configured["provider"].id))
        await session.commit()
    await manager.execute(run_id)
    assert states(published)[-1] == "failed"
    assert containers.started == []
    [result] = [e.payload for e in published if e.type == "result"]
    assert result["state"] == "failed" and "no provider configured" in result["error"]


# The worker's provider test ---------------------------------------------------------------------


def probe_logs(ok=True, detail="claude-x replied 'OK'", error=None) -> str:
    return "starting\n" + PROBE_PREFIX + json.dumps({"ok": ok, "detail": detail, "error": error}) + "\n"


def test_parse_probe():
    assert parse_probe(0, probe_logs()) == (True, "claude-x replied 'OK'", None)
    assert parse_probe(1, probe_logs(False, None, "Claude Code session failed: 401")) == (
        False, None, "Claude Code session failed: 401",
    )
    ok, detail, error = parse_probe(137, "Killed\n")
    assert not ok and detail is None and error.startswith("the probe exited with 137 without a result")


async def new_test(sessions, provider_id) -> uuid.UUID:
    async with sessions() as session:
        provider = await session.get(Provider, provider_id)
        test = ProviderTest(provider_id=provider_id, state="pending", fingerprint=fingerprint(provider))
        session.add(test)
        await session.commit()
        return test.id


@pytest.fixture
async def oauth_provider(sessions, box):
    async with sessions() as session:
        provider = Provider(slug="sub", name="Sub", kind="anthropic", auth_method="oauth_token",
                            credential_encrypted=box.encrypt("oat-secret"), settings={})
        session.add(provider)
        await session.commit()
        return provider.id


async def test_provider_test_passes_and_records_last_test(manager, sessions, containers, oauth_provider, settings):
    probes = []

    async def run_probe(*, image, label, env, timeout, runtime, model=None):
        probes.append({"image": image, "label": label, "env": env, "runtime": runtime.name})
        return ContainerExit(0, probe_logs())

    containers.run_probe = run_probe
    manager.worker_id = "me"
    tester = ProviderTester(manager)
    test_id = await new_test(sessions, oauth_provider)
    assert await tester.claim(test_id, "me") and not await tester.claim(test_id, "other")
    await tester.run(test_id)

    assert probes == [{"image": settings.project_default_image, "label": f"provider-test-{test_id}",
                       "env": {"CLAUDE_CODE_OAUTH_TOKEN": "oat-secret"}, "runtime": "claude_code"}]
    async with sessions() as session:
        test = await session.get(ProviderTest, test_id)
        assert (test.state, test.detail, test.error) == ("passed", "claude-x replied 'OK'", None)
        assert test.started_at and test.ended_at
        provider = await session.get(Provider, oauth_provider)
        assert provider.last_test["ok"] is True and provider.last_test["detail"] == "claude-x replied 'OK'"


async def test_provider_test_failure_is_kept_off_a_changed_provider(manager, sessions, containers, oauth_provider, box):
    test_id = await new_test(sessions, oauth_provider)

    async def run_probe(*, image, label, env, timeout, runtime, model=None):
        async with sessions() as session:  # the credential is replaced while the probe runs
            (await session.get(Provider, oauth_provider)).credential_encrypted = box.encrypt("oat-new")
            await session.commit()
        return ContainerExit(1, probe_logs(False, None, "Claude Code session failed: invalid token"))

    containers.run_probe = run_probe
    await ProviderTester(manager).run(test_id)
    async with sessions() as session:
        test = await session.get(ProviderTest, test_id)
        assert test.state == "failed" and test.error == "Claude Code session failed: invalid token"
        assert (await session.get(Provider, oauth_provider)).last_test is None


async def test_dead_workers_provider_tests_are_failed(manager, sessions, containers, oauth_provider):
    from exradius.worker.containers import RunContainer

    async def is_alive(worker_id):
        return worker_id == "live"

    dead, live = await new_test(sessions, oauth_provider), await new_test(sessions, oauth_provider)
    async with sessions() as session:
        (await session.get(ProviderTest, dead)).worker_id = "dead"
        (await session.get(ProviderTest, dead)).state = "running"
        (await session.get(ProviderTest, live)).worker_id = "live"
        await session.commit()
    containers.existing[f"provider-test-{dead}"] = [RunContainer("probe-1", "probe", True)]
    manager.worker_id = "me"
    assert await ProviderTester(manager).reconcile(is_alive) == [dead]
    assert containers.removed == ["probe-1"]
    async with sessions() as session:
        failed = await session.get(ProviderTest, dead)
        assert failed.state == "failed" and failed.error == "worker restarted during the test"
        assert (await session.get(ProviderTest, live)).state == "pending"


async def test_runner_probe_prints_one_result_line(monkeypatch, capsys):
    seen = {}

    class FakeRuntime:
        async def probe(self, env, model=None):
            seen["env"] = env
            return ProbeResult(True, detail="claude-x replied 'OK'")

    monkeypatch.setattr(runner_main, "get_runtime", lambda name, runtime_dir=None: FakeRuntime())
    environment = RunnerEnvironment(container={"CLAUDE_CODE_OAUTH_TOKEN": "t"}, run_token=None, runtime_dir=None)
    assert await runner_main.probe("claude_code", environment) == 0
    assert seen["env"] == {"CLAUDE_CODE_OAUTH_TOKEN": "t"}
    line = capsys.readouterr().out.strip().splitlines()[-1]
    assert parse_probe(0, line) == (True, "claude-x replied 'OK'", None)


# One-time migration and the admin password ----------------------------------------------------------


async def test_bootstrap_from_the_environment(sessions, settings, box):
    settings.anthropic_api_key = "sk-legacy"
    provider_id = await bootstrap_provider(sessions, settings, box)
    async with sessions() as session:
        provider = await session.get(Provider, provider_id)
        assert (provider.name, provider.kind, provider.auth_method, provider.is_default) == (
            MIGRATED_NAME, "anthropic", "api_key", True,
        )
        assert box.decrypt(provider.credential_encrypted) == "sk-legacy"
    assert await bootstrap_provider(sessions, settings, box) is None  # only while none exists


async def test_bootstrap_prefers_the_global_secret(sessions, settings, box):
    settings.anthropic_api_key = "sk-env"
    async with sessions() as session:
        session.add(Secret(project_id=None, key="ANTHROPIC_API_KEY", value_encrypted=box.encrypt("sk-secret")))
        await session.commit()
    provider_id = await bootstrap_provider(sessions, settings, box)
    async with sessions() as session:
        assert box.decrypt((await session.get(Provider, provider_id)).credential_encrypted) == "sk-secret"


async def test_bootstrap_does_nothing_without_a_legacy_key(sessions, settings, box):
    assert await bootstrap_provider(sessions, settings, box) is None
    async with sessions() as session:
        assert (await session.scalars(select(Provider))).all() == []


def test_api_start_runs_the_migration(settings, store, control):
    settings.anthropic_api_key = "sk-legacy"
    with TestClient(create_app(settings, store=store, broker=control)) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "correct horse"})
        [provider] = c.get("/api/providers").json()
        assert provider["name"] == MIGRATED_NAME and provider["is_default"] is True


def test_change_password(admin):
    wrong = admin.post("/api/auth/password", json={"current_password": "nope", "new_password": "a new password"})
    assert wrong.status_code == 400
    short = admin.post("/api/auth/password", json={"current_password": "correct horse", "new_password": "short"})
    assert short.status_code == 422
    ok = admin.post("/api/auth/password", json={"current_password": "correct horse", "new_password": "a new password"})
    assert ok.status_code == 204
    admin.post("/api/auth/logout")
    assert admin.post("/api/auth/login", json={"username": "admin", "password": "correct horse"}).status_code == 401
    assert admin.post("/api/auth/login", json={"username": "admin", "password": "a new password"}).status_code == 200


def test_admin_password_only_seeds(settings, store, control):
    with TestClient(create_app(settings, store=store, broker=control)) as c:
        c.post("/api/auth/login", json={"username": "admin", "password": "correct horse"})
        assert c.post("/api/auth/password", json={"current_password": "correct horse", "new_password": "changed in panel"}).status_code == 204
    # A restart with EXRADIUS_ADMIN_PASSWORD still set does not reset the password.
    with TestClient(create_app(settings, store=store, broker=control)) as c:
        assert c.post("/api/auth/login", json={"username": "admin", "password": "correct horse"}).status_code == 401
        assert c.post("/api/auth/login", json={"username": "admin", "password": "changed in panel"}).status_code == 200
