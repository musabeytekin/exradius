"""Phase 3: services as native sidecars, the HEALTHCHECK mapping, the project volume with
mirrors and caches, and Verify's service steps (docs/kubernetes.md §7.4, §7.6, §13)."""

from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest

from exradius.resolve import StoreValues
from exradius.schemas.project_config import parse_config
from exradius.worker import git_identity, run_objects
from exradius.worker.diagnose import diagnose
from exradius.worker.registry import ImageConfigs, parse_reference, ready_from_healthcheck
from exradius.runner import workspace
from tests.conftest import BASE_CONFIG, commit_config, git
from tests.test_run_manager import (  # noqa: F401 - fixtures
    add_store, cluster, execute, manager, published, results, set_config, states, submit_task,
)
from tests.test_api import admin, client, control, setup_configuration  # noqa: F401 - fixtures
from tests.test_verification import get, names_and_states, new_verification, verify, verifier  # noqa: F401

MINIMAL = "version: 1\nproject: {slug: p}\nagents:\n  a: {}\n"

SERVICES = """\
secret_store: billing
services:
  postgres:
    image: postgres:16
    env:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: "${secret:DB_PASSWORD}"
    ports: [5432]
    ready: {command: [pg_isready, -h, 127.0.0.1, -U, app]}
    memory: 512m
  cache:
    image: valkey/valkey:8.1-alpine
    ports: [6379]
    ready: {tcp: 6379}
    command: [valkey-server, --save, ""]
  mail:
    image: axllent/mailpit
workstation:
  cache: [~/.cache/uv]
"""


def errors(text: str) -> list[str]:
    result = parse_config(text)
    assert result.config is None
    return result.errors


# The config check (docs/config-repo.md §2) ------------------------------------------------------


def test_services_share_localhost_so_ports_are_unique_across_them():
    assert errors(MINIMAL + "services:\n  a: {image: x, ports: [5432]}\n  b: {image: y, ports: [6379, 5432]}\n") == [
        "services.b.ports: 5432 is also a port of service a; services share the run's localhost, "
        "so each port is one service's"]
    assert parse_config(MINIMAL + "services:\n  a: {image: x, ports: [5432]}\n  b: {image: y, ports: [6379]}\n").config


def test_localhost_is_reserved_and_names_fit_a_container_name():
    assert errors(MINIMAL + "services:\n  localhost: {image: x}\n") == ["services.localhost: the name 'localhost' is reserved"]
    long = "s" * 56
    assert errors(MINIMAL + f"services:\n  {long}: {{image: x}}\n") == [
        f"services.{long}: service names are at most 55 characters"]
    assert parse_config(MINIMAL + f"services:\n  {'s' * 55}: {{image: x}}\n").config


# The Run spec ---------------------------------------------------------------------------------


def task_objects(config_text: str, **kwargs):
    config = parse_config(BASE_CONFIG + config_text).config
    assert config is not None
    return config, run_objects.task_run(
        run_id=uuid.uuid4(), namespace="exradius-runs", task_id="T-1", project="billing-platform", config=config,
        secrets=StoreValues("billing", {"DB_PASSWORD": "hunter2"}), runtime="claude_code", runtime_image="bundle:1",
        workstation_image="ws:1", provider_env={}, credential_variables=frozenset(),
        identity=git_identity.Identity(name="Bot", email="bot@x", agent="bug-fixer", task_id="T-1"),
        token="tok", **kwargs,
    )


def test_services_in_the_run_spec_are_secret_free():
    _, objects = task_objects(SERVICES, service_images={},
                              healthchecks={"mail": ["CMD-SHELL", "wget -q -O- http://127.0.0.1:8025/livez"]})
    spec = objects.run["spec"]
    assert spec["services"] == [
        {"name": "postgres", "image": "postgres:16", "ports": [5432], "memory": "512Mi",
         "env": [{"name": "POSTGRES_USER", "value": "app"},
                 {"name": "POSTGRES_PASSWORD", "secretKey": "service.postgres.POSTGRES_PASSWORD"}],
         "ready": {"exec": ["pg_isready", "-h", "127.0.0.1", "-U", "app"]}},
        {"name": "cache", "image": "valkey/valkey:8.1-alpine", "ports": [6379], "env": [],
         "command": ["valkey-server", "--save", ""], "ready": {"tcp": 6379}},
        {"name": "mail", "image": "axllent/mailpit", "ports": [], "env": [],
         "ready": {"exec": ["/bin/sh", "-c", "wget -q -O- http://127.0.0.1:8025/livez"]}},
    ]
    assert objects.secret["service.postgres.POSTGRES_PASSWORD"] == "hunter2"
    assert "hunter2" not in json.dumps(objects.run)
    # The workstation carries the secret its services use, under its own name.
    assert {"name": "DB_PASSWORD", "secretKey": "env.DB_PASSWORD"} in spec["workstation"]["env"]
    assert spec["workstation"]["caches"] == [{"path": "/home/runner/.cache/uv", "name": "0-cache_uv"}]


def test_a_built_service_image_and_no_healthcheck():
    _, objects = task_objects("services:\n  db: {image: postgres:16}\n",
                              service_images={"db": "kind-registry:5000/exradius/billing-platform/db:abc"})
    [db] = objects.run["spec"]["services"]
    assert db["image"] == "kind-registry:5000/exradius/billing-platform/db:abc" and "ready" not in db


@pytest.mark.parametrize("test, ready", [
    (["CMD", "pg_isready", "-U", "app"], {"exec": ["pg_isready", "-U", "app"]}),
    (["CMD-SHELL", "curl -f localhost || exit 1"], {"exec": ["/bin/sh", "-c", "curl -f localhost || exit 1"]}),
    (["NONE"], None), ([], None), (None, None), (["CMD"], None),
])
def test_healthcheck_mapping(test, ready):
    assert ready_from_healthcheck(test) == ready


@pytest.mark.parametrize("image, registry, repository, reference", [
    ("postgres:16", "registry-1.docker.io", "library/postgres", "16"),
    ("valkey/valkey:8.1-alpine", "registry-1.docker.io", "valkey/valkey", "8.1-alpine"),
    ("docker.io/pgsty/silo:RELEASE.2026", "registry-1.docker.io", "pgsty/silo", "RELEASE.2026"),
    ("ghcr.io/astral-sh/uv", "ghcr.io", "astral-sh/uv", "latest"),
    ("kind-registry:5000/exradius/plane/minio:abc", "kind-registry:5000", "exradius/plane/minio", "abc"),
    ("redis@sha256:feed", "registry-1.docker.io", "library/redis", "sha256:feed"),
    ("localhost/app:1", "localhost", "app", "1"),
])
def test_image_references(image, registry, repository, reference):
    assert parse_reference(image) == type(parse_reference(image))(registry, repository, reference)


def registry_transport(healthcheck, *, auth: bool):
    """A registry with an index (amd64 and arm64), anonymous bearer tokens when ``auth``."""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "anon"})
        if auth and request.headers.get("authorization") != "Bearer anon":
            return httpx.Response(401, headers={"www-authenticate":
                                                'Bearer realm="https://auth.example/token",service="registry.example"'})
        path = request.url.path
        if path.endswith("/manifests/16"):
            return httpx.Response(200, json={
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {"digest": "sha256:amd", "platform": {"os": "linux", "architecture": "amd64"}},
                    {"digest": "sha256:arm", "platform": {"os": "linux", "architecture": "arm64"}},
                    {"digest": "sha256:att", "platform": {"os": "unknown", "architecture": "unknown"}},
                ]})
        if path.endswith("/manifests/sha256:arm"):
            return httpx.Response(200, json={"mediaType": "application/vnd.oci.image.manifest.v1+json",
                                             "config": {"digest": "sha256:cfg"}})
        if path.endswith("/blobs/sha256:cfg"):
            config = {"Healthcheck": {"Test": healthcheck}} if healthcheck else {}
            return httpx.Response(200, json={"config": config})
        return httpx.Response(404)

    return httpx.MockTransport(handler), seen


async def test_image_configs_follow_the_index_with_an_anonymous_token():
    transport, seen = registry_transport(["CMD", "redis-cli", "ping"], auth=True)
    configs = ImageConfigs(transport=transport, arch="arm64")
    assert await configs.healthcheck("registry.example/library/redis:16") == ["CMD", "redis-cli", "ping"]
    assert any(url.startswith("https://registry.example/v2/library/redis/manifests/sha256:arm") for url in seen)
    calls = len(seen)
    assert await configs.healthcheck("registry.example/library/redis:16") == ["CMD", "redis-cli", "ping"]
    assert len(seen) == calls  # cached


async def test_image_configs_over_http_for_insecure_registries_and_none_on_failure():
    transport, seen = registry_transport(None, auth=False)
    configs = ImageConfigs(["kind-registry:5000"], transport=transport, arch="arm64")
    assert await configs.healthcheck("kind-registry:5000/exradius/p/db:16") is None
    assert seen[0].startswith("http://kind-registry:5000/v2/exradius/p/db/manifests/16")
    # A registry that cannot be read: the service counts as started.
    assert await ImageConfigs(transport=transport).healthcheck("registry.example/missing:1") is None


# Runs with services ------------------------------------------------------------------------------


async def test_a_task_run_carries_its_services(manager, sessions, store, cluster, published, configured, box, accept):
    from tests.fakes import runner

    await set_config(configured, SERVICES)
    await add_store(sessions, box, "billing", DB_PASSWORD="hunter2")
    manager.image_configs.healthchecks["axllent/mailpit"] = ["CMD", "/mailpit", "readyz"]
    run_id = await submit_task(accept, published)
    cluster.behaviours.append(runner(sessions))
    await execute(manager, cluster, run_id)
    assert states(published)[-1] == "completed"
    [created] = cluster.created
    services = created["spec"]["services"]
    assert [s["name"] for s in services] == ["postgres", "cache", "mail"]
    assert services[2]["ready"] == {"exec": ["/mailpit", "readyz"]}
    assert manager.image_configs.asked == ["axllent/mailpit"]  # only where `ready` is not set


async def test_a_service_that_never_became_ready_fails_the_run_with_its_log(
        manager, sessions, store, cluster, published, configured, box, accept):
    await set_config(configured, SERVICES)
    await add_store(sessions, box, "billing", DB_PASSWORD="hunter2")
    cluster.prepare_failure = {"container": "service-cache", "name": "Start service cache", "reason": "NotReady",
                               "message": "did not become ready within 120s waiting for port 6379",
                               "log": "1:M Ready to accept connections tcp"}
    run_id = await submit_task(accept, published)
    await execute(manager, cluster, run_id)
    assert states(published) == ["queued", "preparing", "failed"]
    [result] = results(published)
    assert result["error"] == ("service cache (valkey/valkey:8.1-alpine) did not become ready within 120s waiting for "
                               "port 6379\n1:M Ready to accept connections tcp")


async def test_a_service_killed_under_its_declared_memory(manager, sessions, store, cluster, published, configured, box,
                                                          accept):
    await set_config(configured, SERVICES)
    await add_store(sessions, box, "billing", DB_PASSWORD="hunter2")
    cluster.prepare_failure = {"container": "service-postgres", "name": "Start service postgres", "exitCode": 137,
                               "reason": "OOMKilled", "message": "exited with code 137 before it was ready",
                               "log": "Killed"}
    run_id = await submit_task(accept, published)
    await execute(manager, cluster, run_id)
    [result] = results(published)
    assert result["error"] == ("service postgres (postgres:16) exceeded its declared memory (512m): exited with code 137 "
                               "before it was ready\nKilled")
    assert diagnose("Start service postgres", result["error"]) == (
        "Service postgres exceeded its declared memory (512m) and was killed: raise its `memory` in the config.")


# Verify ---------------------------------------------------------------------------------------------


async def test_verify_starts_the_services_before_setup(verifier, sessions, cluster, configured, box, remote_repo):
    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + SERVICES + (
        "  setup: [make migrate]\nrepositories:\n  - {name: app, url: %s}\n" % remote_repo)})
    await add_store(sessions, box, "billing", DB_PASSWORD="hunter2")
    cluster.tool_reports = {"docs": {"ok": True, "tools": [{"name": "search"}], "error": None, "log": ""}}
    vid = await new_verification(sessions, configured["project"].id)
    await verify(verifier, vid)
    verification = await get(sessions, vid)
    assert verification.state == "passed", verification.error
    assert names_and_states(verification) == [
        ("Read config", "passed"), ("Prepare workspace", "passed"), ("Clone app (default branch)", "passed"),
        ("Start service postgres", "passed"), ("Start service cache", "passed"), ("Start service mail", "passed"),
        ("Setup: make migrate", "passed"), ("List tools docs", "passed"),
    ]
    steps = {s["name"]: s for s in verification.steps}
    assert steps["Start service postgres"]["log"] == "ready (command pg_isready -h 127.0.0.1 -U app)"
    assert steps["Start service cache"]["log"] == "ready (port 6379)"
    assert steps["Start service mail"]["log"] == "ready (its process to start (no readiness check))"
    [created] = cluster.created
    assert [s["name"] for s in created["spec"]["services"]] == ["postgres", "cache", "mail"]


async def test_verify_diagnoses_a_service_that_never_became_ready(verifier, sessions, cluster, configured, box):
    await set_config(configured, SERVICES)
    await add_store(sessions, box, "billing", DB_PASSWORD="hunter2")
    cluster.verify_failure = {"container": "service-cache", "reason": "NotReady", "log": "listening on 127.0.0.1",
                              "message": "did not become ready within 120s waiting for port 6379"}
    vid = await new_verification(sessions, configured["project"].id)
    await verify(verifier, vid)
    verification = await get(sessions, vid)
    assert verification.state == "failed"
    step = next(s for s in verification.steps if s["name"] == "Start service cache")
    assert step["state"] == "failed" and step["log"] == (
        "service cache (valkey/valkey:8.1-alpine) did not become ready within 120s waiting for port 6379\n"
        "listening on 127.0.0.1")
    assert verification.diagnosis.startswith("Service cache never became ready within 120s: check its `env`")
    assert "needs a `ready` command instead" in verification.diagnosis
    assert [s["state"] for s in verification.steps if s["name"].startswith("Setup")] == []  # none in this config


def test_service_diagnoses():
    assert diagnose("Start service db", "service db (postgres:16) ran out of memory (OOMKilled): exited with code 137 "
                    "before it was ready\nKilled").startswith("Service db (postgres:16) ran out of memory before it was ready")
    assert "two that listen on one port collide" in diagnose(
        "Start service b", "service b (redis:7) exited with code 1 before it was ready\nAddress already in use")
    assert diagnose("Start service db", "the image postgres:99 of service-db cannot be pulled after 3 attempts: "
                    "manifest unknown") == (
        "The image `postgres:99` could not be pulled: check `services.db.image` (name, tag, registry) and, "
        "for a private registry, the install's `runs.imagePullSecrets`.")


# Project volumes --------------------------------------------------------------------------------


def test_deleting_a_project_tells_the_worker(admin, control, tmp_path):
    project = setup_configuration(admin, tmp_path, BASE_CONFIG)["project"]
    assert admin.delete(f"/api/projects/{project['id']}").status_code == 204
    assert control.deleted_projects == ["billing-platform"]


async def test_project_volumes_go_with_their_projects(manager, cluster, configured):
    cluster.add_project_volume("billing-platform")
    cluster.add_project_volume("gone")
    await manager.sweep_project_volumes()
    assert set(cluster.pvcs) == {"exradius-project-billing-platform"}
    await manager.delete_project_volume("billing-platform")
    assert cluster.pvcs == {}


# The runner's prepare and clone with a project volume ---------------------------------------------


def mode(path) -> int:
    return os.stat(path).st_mode & 0o7777


async def test_prepare_makes_the_cache_directories_and_mount_points(tmp_path):
    root, project = tmp_path / "workspace", tmp_path / "project"
    caches = [{"path": "/home/runner/.cache/uv", "name": "0-cache_uv"}, {"path": "/opt/store", "name": "1-opt_store"},
              {"path": "/tmp/build-cache", "name": "2-tmp_build-cache"}]
    environ = {"EXRADIUS_CACHES": json.dumps(caches), "EXRADIUS_PROJECT_DIR": str(project)}
    assert await workspace.prepare("verify", "v-1", "http://api", "tok", root, environ) == 0
    assert (project / "mirrors").is_dir()
    assert all((project / "caches" / c["name"]).is_dir() for c in caches)
    # Mount points below the home and /tmp exist before the kubelet mounts there.
    assert (root / "home" / ".cache" / "uv").is_dir() and (root / "tmp" / "build-cache").is_dir()
    assert not (root / "caches").exists()

    # Without a project volume each cache is a directory of the run's own workspace.
    other = tmp_path / "other"
    assert await workspace.prepare("verify", "v-2", "http://api", "tok", other,
                                   {"EXRADIUS_CACHES": json.dumps(caches[:1])}) == 0
    assert (other / "caches" / "0-cache_uv").is_dir() and (other / "home" / ".cache" / "uv").is_dir()


def test_clone_through_a_mirror_on_the_project_volume(tmp_path, remote_repo, capsys):
    mirrors = tmp_path / "mirrors"
    mirrors.mkdir()
    env = {"EXRADIUS_CLONE_FOLDER": "app", "EXRADIUS_CLONE_URL": str(remote_repo), "EXRADIUS_CLONE_BRANCH": "feature",
           "EXRADIUS_CLONE_INDEX": "0", "EXRADIUS_MIRROR_DIR": str(mirrors)}
    work, state = tmp_path / "w1", tmp_path / "s1"
    work.mkdir()
    assert workspace.clone(work, state, env) == 0
    assert "created the mirror" in capsys.readouterr().out
    assert (mirrors / "app.git" / "HEAD").exists() and (work / "app" / "feature.txt").exists()
    assert git("remote", "get-url", "origin", cwd=work / "app").strip() == str(remote_repo)
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=work / "app").strip() == "feature"

    # A new commit upstream: the next run's clone fetches it into the mirror first.
    seed = tmp_path / "seed"
    git("checkout", "-q", "feature", cwd=seed)
    (seed / "later.txt").write_text("later\n")
    git("add", ".", cwd=seed)
    git("commit", "-q", "-m", "later", cwd=seed)
    git("push", "-q", str(remote_repo), "feature", cwd=seed)
    work2, state2 = tmp_path / "w2", tmp_path / "s2"
    work2.mkdir()
    assert workspace.clone(work2, state2, env) == 0
    assert "fetched the mirror" in capsys.readouterr().out
    assert (work2 / "app" / "later.txt").exists()
    # Objects are copied, not linked: the mirror is on another volume.
    objects = [p for p in (work2 / "app" / ".git" / "objects").rglob("*") if p.is_file()]
    assert objects and all(p.stat().st_nlink == 1 for p in objects)
