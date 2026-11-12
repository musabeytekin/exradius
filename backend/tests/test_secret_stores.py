"""Secret stores bound to projects, their change log and their backends (docs/config-repo.md
§7): a store the project may not use is refused wherever values are resolved, every change
is logged without a value, settings are validated per kind, a store moves between kinds,
and an external store's keys are not written here. Migrations 0009 and 0010 are in
test_migrations.py."""

from __future__ import annotations

import json
import uuid
from collections.abc import Collection

import pytest
from sqlalchemy import delete, select

from exradius.crypto import hash_token
from exradius.models import (
    Project,
    ProjectVerification,
    Provider,
    Run,
    SecretStore,
    SecretStoreChange,
    SecretStoreEntry,
    SecretStoreProject,
    Task,
)
from exradius.project_state import read_config, store_values
from exradius.resolve import StoreError
from exradius.secret_backends import SecretBackends, StoreUnreachable, kinds
from exradius.secret_backends.kinds import PATH_RULE, VaultSettings
from tests.conftest import BASE_CONFIG, add_project, commit_config, make_config_repo
from tests.test_api import API_CONFIG, admin, client, control, make_run, setup_configuration, sync_db  # noqa: F401
from tests.test_egress import EGRESS, provider, reviews  # noqa: F401 - fixtures
from tests.test_run_manager import cluster, execute, manager, published, results, submit_task  # noqa: F401
from tests.test_verification import get, new_verification, verifier, verify  # noqa: F401

NOT_ALLOWED = "secret store billing is not allowed for this project"
SECRET_CONFIG = BASE_CONFIG + "secret_store: billing\nenv:\n  DB: ${secret:DB_PASSWORD}\n"
VAULT_LABEL_409 = "store {name} reads its values from HashiCorp Vault; set and delete them there"


class FakeVault:
    """Stands in for ``VaultClient``: the fields of each (mount, path) secret, and what
    ``mount_problem`` answers."""

    def __init__(self):
        self.secrets: dict[tuple[str, str], dict[str, str]] = {}
        self.problem: str | None = None
        self.down: str | None = None

    def _secret(self, store: str, settings: VaultSettings) -> dict[str, str]:
        if self.down is not None:
            raise StoreUnreachable(store, self.down)
        return self.secrets.get((settings.mount, settings.path), {})

    async def names(self, store: str, settings: VaultSettings, *, retry: bool) -> set[str]:
        return set(self._secret(store, settings))

    async def values(self, store: str, settings: VaultSettings, names: Collection[str], *, retry: bool) -> dict[str, str]:
        secret = self._secret(store, settings)
        return {name: secret[name] for name in names if name in secret}

    async def mount_problem(self, store: str, settings: VaultSettings, *, fresh: bool = False) -> str | None:
        return self.problem

    async def aclose(self) -> None:
        pass


@pytest.fixture
def vault(client) -> FakeVault:
    """The api's install with a Vault."""
    fake = FakeVault()
    state = client.app.state.exradius
    state.backends = SecretBackends(state.box, fake)  # type: ignore[arg-type]
    return fake


async def add_store(sessions, box, name: str, *, allowed: Collection[uuid.UUID] = (), **values: str) -> uuid.UUID:
    async with sessions() as session:
        secret_store = SecretStore(name=name)
        session.add(secret_store)
        await session.flush()
        for project_id in allowed:
            session.add(SecretStoreProject(store_id=secret_store.id, project_id=project_id))
        for key, value in values.items():
            session.add(SecretStoreEntry(store_id=secret_store.id, key=key, value_encrypted=box.encrypt(value)))
        await session.commit()
        return secret_store.id


async def disallow(sessions, store_id: uuid.UUID) -> None:
    async with sessions() as session:
        await session.execute(delete(SecretStoreProject).where(SecretStoreProject.store_id == store_id))
        await session.commit()


def changes(admin, store_id: str) -> list[tuple[str, str | None, str | None]]:
    """The store's log, oldest first, as (action, key, project)."""
    log = admin.get(f"/api/stores/{store_id}/changes").json()
    return [(c["action"], c["key"], c["project"]) for c in reversed(log)]


# 2. A store the project may not use is refused on every path ----------------------------------


async def test_store_values_refuses_a_project_the_store_does_not_allow(sessions, box, settings, configured):
    commit_config(configured["remote"], {"exradius.yaml": SECRET_CONFIG})
    project = configured["project"]
    store_id = await add_store(sessions, box, "billing", DB_PASSWORD="hunter2")
    async with sessions() as session:
        config = (await read_config(session, box, settings.cache_dir, project)).config
        with pytest.raises(StoreError) as refused:
            await store_values(session, SecretBackends(box), project.id, config)
        assert str(refused.value) == NOT_ALLOWED
        session.add(SecretStoreProject(store_id=store_id, project_id=project.id))
        await session.commit()
        resolved = await store_values(session, SecretBackends(box), project.id, config)
        assert resolved.values == {"DB_PASSWORD": "hunter2"}


async def test_a_store_the_project_may_not_use_is_rejected_at_intake(sessions, box, settings, configured, accept,
                                                                     tmp_path):
    """Allowing the store to another project does not let this one use it."""
    commit_config(configured["remote"], {"exradius.yaml": SECRET_CONFIG})
    other_remote = make_config_repo(tmp_path / "other", {"exradius.yaml": SECRET_CONFIG.replace(
        "slug: billing-platform", "slug: other")})
    other = await add_project(sessions, settings.cache_dir, other_remote)
    await add_store(sessions, box, "billing", allowed=[other.id], DB_PASSWORD="hunter2")

    intake = await accept(json.dumps({"version": 1, "task_id": "T-1", "project": "billing-platform",
                                      "agent": "bug-fixer", "instructions": "Fix it."}).encode())
    assert intake.run_id is None
    [event] = intake.events
    assert event.envelope.payload["state"] == "rejected"
    assert event.envelope.payload["reason"] == NOT_ALLOWED
    async with sessions() as session:
        assert await session.get(Task, "T-1") is None


async def test_a_store_the_project_may_not_use_fails_the_run_in_preparing(manager, sessions, box, cluster, published,
                                                                          configured, accept):
    """The project was removed from the store between intake and preparing."""
    commit_config(configured["remote"], {"exradius.yaml": SECRET_CONFIG})
    store_id = await add_store(sessions, box, "billing", allowed=[configured["project"].id], DB_PASSWORD="hunter2")
    run_id = await submit_task(accept, published)
    await disallow(sessions, store_id)

    await execute(manager, cluster, run_id)
    [result] = results(published)
    assert result["state"] == "failed" and result["error"] == NOT_ALLOWED
    assert cluster.created == [] and cluster.secrets == {}  # no Run, no per-run Secret


async def test_a_store_the_project_may_not_use_fails_verify(verifier, sessions, box, cluster, configured):
    commit_config(configured["remote"], {"exradius.yaml": SECRET_CONFIG})
    await add_store(sessions, box, "billing", DB_PASSWORD="hunter2")
    vid = await new_verification(sessions, configured["project"].id)
    await verify(verifier, vid)

    verification = await get(sessions, vid)
    assert verification.state == "failed" and verification.error == f"Read config: {NOT_ALLOWED}"
    [step] = verification.steps
    assert step["log"].splitlines()[0] == NOT_ALLOWED
    # The project learns nothing about the store's keys: all of its own count as missing.
    assert step["log"].splitlines()[-1] == "missing keys: DB_PASSWORD (store billing, which does not allow this project)"
    assert cluster.created == []


def test_a_store_the_project_may_not_use_is_refused_by_the_run_spec(client, admin, sync_db, tmp_path):
    _, run_id = make_run(admin, sync_db, tmp_path)
    auth = {"Authorization": "Bearer tok"}
    assert client.get(f"/internal/runs/{run_id}/spec", headers=auth).status_code == 200
    store_id = admin.get("/api/stores").json()[0]["id"]
    admin.patch(f"/api/stores/{store_id}", json={"allowed_projects": []})

    refused = client.get(f"/internal/runs/{run_id}/spec", headers=auth)
    assert refused.status_code == 422 and refused.json()["detail"] == NOT_ALLOWED


def test_a_store_the_project_may_not_use_is_refused_by_the_verify_spec(client, admin, sync_db, tmp_path):
    cfg = setup_configuration(admin, tmp_path)
    verification = ProjectVerification(project_id=uuid.UUID(cfg["project"]["id"]), state="running", steps=[],
                                       token_hash=hash_token("vtok"))
    sync_db.add(verification)
    sync_db.commit()
    path, auth = f"/internal/verifications/{verification.id}/spec", {"Authorization": "Bearer vtok"}
    assert client.get(path, headers=auth).status_code == 200
    admin.patch(f"/api/stores/{cfg['store']['id']}", json={"allowed_projects": []})

    refused = client.get(path, headers=auth)
    assert refused.status_code == 422 and refused.json()["detail"] == NOT_ALLOWED


def test_a_store_the_project_may_not_use_is_refused_by_a_task_runs_egress_policy(client, admin, sync_db, tmp_path,
                                                                                 reviews, provider):
    cfg = setup_configuration(admin, tmp_path)
    project = cfg["project"]
    sync_db.add(Task(id="JIRA-1", project_id=uuid.UUID(project["id"]), agent="bug-fixer", instructions="Fix it.",
                     state="running"))
    run = Run(id=uuid.uuid4(), task_id="JIRA-1", state="running", token_hash=hash_token("tok"),
              config=project["config"], config_commit=project["commit"],
              provider_id=sync_db.query(Provider).one().id)
    sync_db.add(run)
    sync_db.commit()
    path = f"/internal/egress/runs/run-{run.id}"
    assert client.get(path, headers=EGRESS).status_code == 200
    admin.patch(f"/api/stores/{cfg['store']['id']}", json={"allowed_projects": []})

    refused = client.get(path, headers=EGRESS)
    assert refused.status_code == 409 and refused.json()["detail"] == NOT_ALLOWED
    assert "ghp_secret" not in refused.text


def test_a_store_the_project_may_not_use_is_refused_by_a_verify_runs_egress_policy(client, admin, sync_db, tmp_path,
                                                                                   reviews):
    cfg = setup_configuration(admin, tmp_path)
    verification = ProjectVerification(project_id=uuid.UUID(cfg["project"]["id"]), state="running", steps=[])
    sync_db.add(verification)
    sync_db.commit()
    path = f"/internal/egress/runs/verify-{verification.id}"
    assert client.get(path, headers=EGRESS).status_code == 200
    admin.patch(f"/api/stores/{cfg['store']['id']}", json={"allowed_projects": []})

    refused = client.get(path, headers=EGRESS)
    assert refused.status_code == 409 and refused.json()["detail"] == NOT_ALLOWED


def test_a_project_whose_store_does_not_allow_it_learns_nothing_about_its_keys(admin, tmp_path):
    cfg = setup_configuration(admin, tmp_path)
    admin.patch(f"/api/stores/{cfg['store']['id']}", json={"allowed_projects": []})
    project = admin.get(f"/api/projects/{cfg['project']['id']}").json()
    assert (project["health"], project["runnable"], project["reason"]) == ("store_not_allowed", False, NOT_ALLOWED)
    assert project["store_exists"] is True and project["store_allowed"] is False
    # The store holds every key, yet none is reported present.
    assert project["required_keys"] == [{"name": n, "present": False} for n in ("DOCS_KEY", "GIT_TOKEN", "SENTRY_TOKEN")]
    assert project["missing_keys"] == ["DOCS_KEY", "GIT_TOKEN", "SENTRY_TOKEN"]


def test_a_new_store_allows_no_project_unless_told(admin, tmp_path):
    remote = make_config_repo(tmp_path, {"exradius.yaml": API_CONFIG})
    slug = admin.post("/api/projects", json={"config_url": str(remote)}).json()["slug"]
    store = admin.post("/api/stores", json={"name": "billing"}).json()
    assert store["allowed_projects"] == [] and store["used_by"] == [slug]
    unknown = admin.patch(f"/api/stores/{store['id']}", json={"allowed_projects": [slug, "nope"]})
    assert unknown.status_code == 422 and unknown.json()["detail"] == "no project 'nope'"
    assert admin.get(f"/api/stores/{store['id']}").json()["allowed_projects"] == []  # nothing was written
    assert admin.post("/api/stores", json={"name": "other", "allowed_projects": ["nope"]}).status_code == 422
    assert [s["name"] for s in admin.get("/api/stores").json()] == ["billing"]


# 3. The change log -------------------------------------------------------------------------------


VALUES = {
    "GIT_TOKEN": "ghp_AUDIT-first-7f3c91",
    "DOCS_KEY": "docs-AUDIT-second-52ab0e",
    "SENTRY_TOKEN": "sentry-AUDIT-third-0d84c2",
}
REPLACED = "ghp_AUDIT-replaced-e6197a"


def test_every_store_change_is_logged_and_no_entry_holds_a_value(admin, sync_db, tmp_path, vault):
    seen: list[str] = []

    def call(method: str, path: str, **kwargs):
        response = getattr(admin, method)(path, **kwargs)
        assert response.status_code < 400, response.text
        seen.append(response.text)
        return response

    remote = make_config_repo(tmp_path, {"exradius.yaml": API_CONFIG})
    project = call("post", "/api/projects", json={"config_url": str(remote)}).json()
    slug = project["slug"]
    store = call("post", "/api/stores", json={"name": "billing", "allowed_projects": [slug]}).json()
    sid = store["id"]
    call("put", f"/api/stores/{sid}/keys/GIT_TOKEN", json={"value": VALUES["GIT_TOKEN"]})
    call("put", f"/api/stores/{sid}/keys", json={"values": {k: VALUES[k] for k in ("DOCS_KEY", "SENTRY_TOKEN")}})
    call("put", f"/api/stores/{sid}/keys/GIT_TOKEN", json={"value": REPLACED})
    call("delete", f"/api/stores/{sid}/keys/SENTRY_TOKEN")
    call("patch", f"/api/stores/{sid}", json={"allowed_projects": []})
    call("patch", f"/api/stores/{sid}", json={"allowed_projects": [slug]})
    call("get", f"/api/stores/{sid}")
    call("get", f"/api/stores/{sid}/keys")
    call("get", f"/api/projects/{project['id']}")
    # Leaving internal deletes the values kept here, each one logged.
    call("patch", f"/api/stores/{sid}", json={"kind": "vault", "settings": {"path": "teams/billing"}})
    call("patch", f"/api/stores/{sid}", json={"settings": {"mount": "kv", "path": "teams/billing"}})

    assert changes(admin, sid) == [
        ("store_created", None, None),
        ("project_allowed", None, slug),
        ("key_set", "GIT_TOKEN", None),
        ("key_set", "DOCS_KEY", None),
        ("key_set", "SENTRY_TOKEN", None),
        ("key_set", "GIT_TOKEN", None),
        ("key_deleted", "SENTRY_TOKEN", None),
        ("project_removed", None, slug),
        ("project_allowed", None, slug),
        ("key_deleted", "DOCS_KEY", None),
        ("key_deleted", "GIT_TOKEN", None),
        ("settings_changed", None, None),
        ("settings_changed", None, None),
    ]
    log = call("get", f"/api/stores/{sid}/changes").json()
    assert {c["actor"] for c in log} == {"admin"}
    by_action = {}
    for change in reversed(log):
        by_action.setdefault(change["action"], []).append(change)
    assert by_action["store_created"][0]["detail"] == {"kind": "internal", "settings": {}}
    assert [c["detail"] for c in by_action["key_deleted"]] == [
        None, {"reason": "the store moved to HashiCorp Vault"}, {"reason": "the store moved to HashiCorp Vault"}]
    assert [c["detail"] for c in by_action["settings_changed"]] == [
        {"before": {"kind": "internal", "settings": {}},
         "after": {"kind": "vault", "settings": {"mount": "secret", "path": "teams/billing"}}},
        {"before": {"kind": "vault", "settings": {"mount": "secret", "path": "teams/billing"}},
         "after": {"kind": "vault", "settings": {"mount": "kv", "path": "teams/billing"}}},
    ]

    # Deleting the project removes it from the store, and says why; the store's log outlives it.
    call("delete", f"/api/projects/{project['id']}")
    assert changes(admin, sid)[-1] == ("project_removed", None, slug)
    assert admin.get(f"/api/stores/{sid}/changes").json()[0]["detail"] == {"reason": "project deleted"}
    call("delete", f"/api/stores/{sid}")
    assert admin.get(f"/api/stores/{sid}/changes").status_code == 404
    rows = sync_db.scalars(select(SecretStoreChange).where(SecretStoreChange.store_id == uuid.UUID(sid))
                           .order_by(SecretStoreChange.id)).all()
    assert len(rows) == 15 and rows[-1].action == "store_deleted"
    assert {(r.store, r.actor) for r in rows} == {("billing", "admin")}

    # No value anywhere: not in the stored log, not in any answer.
    every_row = json.dumps([
        {c.name: getattr(row, c.name) for c in SecretStoreChange.__table__.columns}
        for row in sync_db.scalars(select(SecretStoreChange)).all()
    ], default=str)
    for value in [*VALUES.values(), REPLACED]:
        assert value not in every_row
        assert not any(value in text for text in seen), value


def test_a_store_moved_with_values_keeps_them_out_of_its_log(admin, sync_db, vault):
    """The settings logged before and after a move never carry the values that were deleted."""
    store = admin.post("/api/stores", json={"name": "team"}).json()
    admin.put(f"/api/stores/{store['id']}/keys", json={"values": VALUES})
    moved = admin.patch(f"/api/stores/{store['id']}", json={"kind": "vault", "settings": {"path": "team"}})
    assert moved.status_code == 200
    text = admin.get(f"/api/stores/{store['id']}/changes").text + moved.text
    assert not any(value in text for value in VALUES.values())
    assert sync_db.scalars(select(SecretStoreEntry)).all() == []


# 4. Settings, validated per kind ---------------------------------------------------------------


def test_internal_stores_take_no_settings():
    assert kinds.normalize("internal", {}) == {}
    assert kinds.where("internal", {}) == ""
    with pytest.raises(kinds.SettingsInvalid) as invalid:
        kinds.parse("internal", {"path": "x"})
    assert invalid.value.errors == ["settings.path: unknown key"]


def test_vault_settings_need_a_path_and_default_the_mount():
    assert kinds.normalize("vault", {"path": "teams/bank"}) == {"mount": "secret", "path": "teams/bank"}
    assert kinds.normalize("vault", {"mount": "/kv/", "path": "/teams/bank/"}) == {"mount": "kv", "path": "teams/bank"}
    assert kinds.where("vault", {"mount": "kv", "path": "a/b"}) == "kv/a/b"
    for path in ("team_1/x.y", "user@corp/a-b", "a/b/c"):
        assert kinds.normalize("vault", {"path": path})["path"] == path


@pytest.mark.parametrize(("given", "errors"), [
    ({}, ["settings.path: required"]),
    ({"path": "a", "mounts": "kv"}, ["settings.mounts: unknown key"]),
    ({"path": 3}, ["settings.path: must be a string"]),
    ({"mount": None, "path": "a"}, ["settings.mount: must be a string"]),
    ({"path": ""}, [f"settings.path: {PATH_RULE}"]),
    ({"path": "/"}, [f"settings.path: {PATH_RULE}"]),
    ({"path": "a/../b"}, [f"settings.path: {PATH_RULE}"]),
    ({"path": "a/./b"}, [f"settings.path: {PATH_RULE}"]),
    ({"path": "a//b"}, [f"settings.path: {PATH_RULE}"]),
    ({"path": "a b"}, [f"settings.path: {PATH_RULE}"]),
    ({"mount": "k$v", "path": "a"}, [f"settings.mount: {PATH_RULE}"]),
    ({"mount": "", "token": "s.x"}, [f"settings.mount: {PATH_RULE}", "settings.path: required",
                                     "settings.token: unknown key"]),
])
def test_vault_settings_are_refused_with_every_problem(given, errors):
    with pytest.raises(kinds.SettingsInvalid) as invalid:
        kinds.parse("vault", given)
    assert sorted(invalid.value.errors) == sorted(errors)
    assert kinds.where("vault", given) == ""


def test_an_unknown_kind_is_refused():
    with pytest.raises(kinds.SettingsInvalid) as invalid:
        kinds.parse("aws", {})
    assert invalid.value.errors == ["kind: 'aws' is not a store kind (known: internal, vault)"]


def test_store_kinds_say_what_this_install_can_use(admin):
    listed = {k["kind"]: k for k in admin.get("/api/store-kinds").json()}
    assert listed["internal"] == {"kind": "internal", "label": "Exradius", "external": False, "available": True,
                                  "reason": None, "fields": []}
    vault_kind = listed["vault"]
    assert (vault_kind["external"], vault_kind["available"]) == (True, False)
    assert vault_kind["reason"] == "Vault is not configured for this install (values vault.address)"
    assert [(f["name"], f["required"], f["default"]) for f in vault_kind["fields"]] == [
        ("mount", False, "secret"), ("path", True, None)]


def test_store_kinds_with_a_vault(admin, vault):
    listed = {k["kind"]: k for k in admin.get("/api/store-kinds").json()}
    assert listed["vault"]["available"] is True and listed["vault"]["reason"] is None


def test_a_vault_store_needs_a_vault_in_the_install(admin, sync_db):
    refused = admin.post("/api/stores", json={"name": "team", "kind": "vault", "settings": {"path": "team"}})
    assert refused.status_code == 422
    assert refused.json()["detail"] == "Vault is not configured for this install (values vault.address)"
    assert sync_db.scalars(select(SecretStore)).all() == []


def test_creating_a_store_validates_its_settings(admin, sync_db, vault):
    assert admin.post("/api/stores", json={"name": "team", "kind": "vault", "settings": {}}).json()["detail"] == [
        "settings.path: required"]
    assert admin.post("/api/stores", json={"name": "team", "kind": "vault",
                                           "settings": {"path": "x", "role": "r"}}).json()["detail"] == [
        "settings.role: unknown key"]
    assert admin.post("/api/stores", json={"name": "team", "settings": {"path": "x"}}).json()["detail"] == [
        "settings.path: unknown key"]
    assert admin.post("/api/stores", json={"name": "team", "kind": "aws"}).json()["detail"] == [
        "kind: 'aws' is not a store kind (known: internal, vault)"]
    vault.problem = "kv1 is a KV version 1 mount; stores of kind vault read KV version 2"
    refused = admin.post("/api/stores", json={"name": "team", "kind": "vault", "settings": {"mount": "kv1", "path": "x"}})
    assert refused.status_code == 422 and refused.json()["detail"] == [f"settings.mount: {vault.problem}"]
    assert sync_db.scalars(select(SecretStore)).all() == []
    assert sync_db.scalars(select(SecretStoreChange)).all() == []

    vault.problem = None
    vault.secrets[("secret", "teams/bank")] = {"API_KEY": "v"}
    created = admin.post("/api/stores", json={"name": "team", "kind": "vault", "settings": {"path": "/teams/bank/"}})
    assert created.status_code == 201, created.text
    body = created.json()
    assert (body["kind"], body["settings"], body["where"], body["external"]) == (
        "vault", {"mount": "secret", "path": "teams/bank"}, "secret/teams/bank", True)
    assert body["keys"] == ["API_KEY"] and body["error"] is None


def test_an_unreachable_vault_store_shows_why(admin, vault):
    store = admin.post("/api/stores", json={"name": "team", "kind": "vault", "settings": {"path": "team"}}).json()
    vault.down = "connection refused"
    shown = admin.get(f"/api/stores/{store['id']}").json()
    assert shown["keys"] == [] and shown["error"] == "secret store team is unreachable: connection refused"
    keys = admin.get(f"/api/stores/{store['id']}/keys")
    assert keys.status_code == 503 and keys.json()["detail"] == shown["error"]


# 5. Moving a store to another kind -------------------------------------------------------------


def test_moving_an_internal_store_to_vault_deletes_its_values_and_keeps_its_projects(admin, sync_db, tmp_path, vault):
    cfg = setup_configuration(admin, tmp_path)
    sid = cfg["store"]["id"]
    vault.secrets[("secret", "teams/billing")] = {"GIT_TOKEN": "g", "DOCS_KEY": "d", "SENTRY_TOKEN": "s"}
    moved = admin.patch(f"/api/stores/{sid}", json={"kind": "vault", "settings": {"path": "teams/billing"}})
    assert moved.status_code == 200, moved.text
    body = moved.json()
    assert (body["kind"], body["external"], body["where"]) == ("vault", True, "secret/teams/billing")
    assert body["keys"] == ["DOCS_KEY", "GIT_TOKEN", "SENTRY_TOKEN"]  # from the vault now
    assert body["allowed_projects"] == ["billing-platform"] and body["used_by"] == ["billing-platform"]
    assert sync_db.scalars(select(SecretStoreEntry)).all() == []
    # The project names the same store and reads it from the vault, without a commit.
    project = admin.get(f"/api/projects/{cfg['project']['id']}").json()
    assert project["store_id"] == sid and (project["health"], project["runnable"]) == ("ok", True)


def test_moving_a_vault_store_back_to_internal_clears_its_settings(admin, vault):
    store = admin.post("/api/stores", json={"name": "team", "kind": "vault", "settings": {"path": "team"}}).json()
    moved = admin.patch(f"/api/stores/{store['id']}", json={"kind": "internal"}).json()
    assert (moved["kind"], moved["settings"], moved["where"], moved["external"], moved["keys"]) == (
        "internal", {}, "", False, [])
    assert admin.put(f"/api/stores/{store['id']}/keys/A", json={"value": "v"}).status_code == 200
    assert changes(admin, store["id"])[-2:] == [("settings_changed", None, None), ("key_set", "A", None)]


def test_new_settings_alone_keep_the_kind(admin, vault):
    store = admin.post("/api/stores", json={"name": "team", "kind": "vault", "settings": {"path": "team"}}).json()
    moved = admin.patch(f"/api/stores/{store['id']}", json={"settings": {"mount": "kv", "path": "other"}}).json()
    assert (moved["kind"], moved["settings"]) == ("vault", {"mount": "kv", "path": "other"})
    # A settings change on an internal store is validated against internal.
    internal = admin.post("/api/stores", json={"name": "plain"}).json()
    refused = admin.patch(f"/api/stores/{internal['id']}", json={"settings": {"path": "x"}})
    assert refused.status_code == 422 and refused.json()["detail"] == ["settings.path: unknown key"]


def test_saving_the_same_kind_and_settings_changes_nothing(admin, vault):
    store = admin.post("/api/stores", json={"name": "team", "kind": "vault", "settings": {"path": "team"}}).json()
    before = changes(admin, store["id"])
    for body in ({"kind": "vault", "settings": {"path": "team"}}, {"settings": {"mount": "secret", "path": "/team"}},
                 {"kind": "vault"}):
        assert admin.patch(f"/api/stores/{store['id']}", json=body).status_code == 200
    assert changes(admin, store["id"]) == before
    plain = admin.post("/api/stores", json={"name": "plain"}).json()
    admin.put(f"/api/stores/{plain['id']}/keys/A", json={"value": "v"})
    before = changes(admin, plain["id"])
    assert admin.patch(f"/api/stores/{plain['id']}", json={"kind": "internal", "settings": {}}).status_code == 200
    assert changes(admin, plain["id"]) == before
    assert [k["key"] for k in admin.get(f"/api/stores/{plain['id']}/keys").json()] == ["A"]


def test_a_refused_move_deletes_no_value(admin, sync_db, tmp_path, vault):
    cfg = setup_configuration(admin, tmp_path)
    sid = cfg["store"]["id"]
    before = changes(admin, sid)
    refused = admin.patch(f"/api/stores/{sid}", json={"kind": "vault", "settings": {"mount": "kv"}})
    assert refused.status_code == 422 and refused.json()["detail"] == ["settings.path: required"]
    vault.problem = "kv is a KV version 1 mount"
    refused = admin.patch(f"/api/stores/{sid}", json={"kind": "vault", "settings": {"mount": "kv", "path": "x"}})
    assert refused.status_code == 422 and refused.json()["detail"] == ["settings.mount: kv is a KV version 1 mount"]
    refused = admin.patch(f"/api/stores/{sid}", json={"kind": "aws"})
    assert refused.status_code == 422
    assert len(sync_db.scalars(select(SecretStoreEntry)).all()) == 3
    assert changes(admin, sid) == before
    assert admin.get(f"/api/stores/{sid}").json()["kind"] == "internal"


def test_a_move_to_vault_without_a_vault_deletes_no_value(admin, sync_db, tmp_path):
    cfg = setup_configuration(admin, tmp_path)
    refused = admin.patch(f"/api/stores/{cfg['store']['id']}", json={"kind": "vault", "settings": {"path": "x"}})
    assert refused.status_code == 422
    assert refused.json()["detail"] == "Vault is not configured for this install (values vault.address)"
    assert len(sync_db.scalars(select(SecretStoreEntry)).all()) == 3


def test_a_move_and_new_projects_in_one_patch(admin, tmp_path, vault):
    cfg = setup_configuration(admin, tmp_path)
    sid = cfg["store"]["id"]
    moved = admin.patch(f"/api/stores/{sid}", json={"kind": "vault", "settings": {"path": "p"}, "allowed_projects": []})
    assert moved.status_code == 200
    assert moved.json()["allowed_projects"] == [] and moved.json()["kind"] == "vault"
    assert changes(admin, sid)[-2:] == [("settings_changed", None, None), ("project_removed", None, "billing-platform")]


# 6. An external store's keys are set in its backend only ------------------------------------------


def test_writing_a_key_to_an_external_store_is_refused(admin, sync_db, vault):
    store = admin.post("/api/stores", json={"name": "team", "kind": "vault", "settings": {"path": "team"}}).json()
    vault.secrets[("secret", "team")] = {"API_KEY": "v"}
    sid, message = store["id"], VAULT_LABEL_409.format(name="team")
    before = changes(admin, sid)
    for refused in (
        admin.put(f"/api/stores/{sid}/keys/API_KEY", json={"value": "new"}),
        admin.put(f"/api/stores/{sid}/keys", json={"values": {"API_KEY": "new", "OTHER": "x"}}),
        admin.delete(f"/api/stores/{sid}/keys/API_KEY"),
    ):
        assert refused.status_code == 409 and refused.json()["detail"] == message
    assert sync_db.scalars(select(SecretStoreEntry)).all() == []
    assert changes(admin, sid) == before
    assert admin.get(f"/api/stores/{sid}/keys").json() == [{"key": "API_KEY", "updated_at": None}]


def test_writing_a_key_to_a_store_of_an_unknown_kind_is_refused(admin, sync_db):
    """A store of a kind this version does not know (written by a newer one) is never written here."""
    sync_db.add(SecretStore(name="future", kind="aws", settings={"arn": "x"}))
    sync_db.commit()
    store = next(s for s in admin.get("/api/stores").json() if s["name"] == "future")
    assert store["external"] is True
    assert store["error"] == "secret store future is unreachable: this version does not know the store kind 'aws'"
    refused = admin.put(f"/api/stores/{store['id']}/keys/A", json={"value": "v"})
    assert refused.status_code == 409
    assert refused.json()["detail"] == "store future reads its values from aws; set and delete them there"
    assert sync_db.scalars(select(SecretStoreEntry)).all() == []


def test_a_vault_store_whose_install_lost_its_vault_is_unreachable_not_missing(admin, sync_db, tmp_path):
    cfg = setup_configuration(admin, tmp_path, values={})
    store = sync_db.get(SecretStore, uuid.UUID(cfg["store"]["id"]))
    store.kind, store.settings = "vault", {"mount": "secret", "path": "billing"}
    sync_db.commit()
    project = admin.get(f"/api/projects/{cfg['project']['id']}").json()
    reason = "secret store billing is unreachable: Vault is not configured for this install (values vault.address)"
    assert (project["health"], project["reason"], project["store_error"]) == ("store_unreachable", reason, reason)
    assert {k["present"] for k in project["required_keys"]} == {None} and project["missing_keys"] == []
    refused = admin.put(f"/api/stores/{store.id}/keys/GIT_TOKEN", json={"value": "v"})
    assert refused.status_code == 409 and refused.json()["detail"] == VAULT_LABEL_409.format(name="billing")


def test_a_project_reads_an_allowed_vault_store(admin, sync_db, tmp_path, vault):
    remote = make_config_repo(tmp_path, {"exradius.yaml": API_CONFIG})
    project = admin.post("/api/projects", json={"config_url": str(remote)}).json()
    vault.secrets[("secret", "billing")] = {"GIT_TOKEN": "g", "DOCS_KEY": "d"}
    admin.post("/api/stores", json={"name": "billing", "kind": "vault", "settings": {"path": "billing"},
                                    "allowed_projects": [project["slug"]]})
    shown = admin.get(f"/api/projects/{project['id']}").json()
    assert shown["reason"] == "missing secret SENTRY_TOKEN in store billing"
    assert {k["name"]: k["present"] for k in shown["required_keys"]} == {
        "DOCS_KEY": True, "GIT_TOKEN": True, "SENTRY_TOKEN": False}
    assert sync_db.scalars(select(Project)).one().slug == "billing-platform"
