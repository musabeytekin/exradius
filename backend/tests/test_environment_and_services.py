"""docs/environment-and-services.md: the environment section, services, the agent's model,
the delivery warning and the run user's passwd."""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from docker.errors import NotFound
from sqlalchemy import select

from exradius.api import provider_routes
from exradius.config import Settings
from exradius.environment import (
    NOT_AVAILABLE,
    RepoFact,
    ServiceFact,
    build_environment_text,
    can_push,
    project_environment_text,
    public_url,
)
from exradius.models import EnvVar, Project, ProjectService, ProjectVerification, Repository, Run, Secret, Task
from exradius.runner.prompt import OPERATING_RULES, build_system_prompt
from exradius.runtimes import RunSpec
from exradius.runtimes.claude_code import ClaudeCodeRuntime, write_default_settings
from exradius.schemas.internal import RunSpecResponse
from exradius.states import State
from exradius.worker import prepare
from exradius.worker.containers import (
    INSPECT_CLONES_SCRIPT,
    SERVICE_CAPS,
    ContainerManager,
    ServiceFailed,
    ServiceSpec,
    listening_ports,
)
from exradius.worker.delivery import delivery_warnings
from exradius.worker.verify import ProjectVerifier
from tests.conftest import git
from tests.test_api import admin, client, control, make_run, setup_configuration, sync_db  # noqa: F401
from tests.test_run_manager import (  # noqa: F401 - fixtures
    containers,
    manager,
    published,
    results,
    runner,
    states,
    submit_task,
)

# The environment section ---------------------------------------------------------------------

REPOS = [
    RepoFact("bank-of-anthos", "main", "https://x-access-token:ghp_SECRET@github.com/musabeytekin/bank-of-anthos.git", True),
    RepoFact("platform-config", "main", "https://github.com/acme/platform-config.git?token=QSECRET#frag", False),
    RepoFact("legacy", "develop", "git@github.com:acme/legacy.git", False),
]
SERVICES = [ServiceFact("postgres", "postgres:16", (5432,)), ServiceFact("redis", "redis:7", (6379,)),
            ServiceFact("mailpit", "axllent/mailpit", (1025, 8025)), ServiceFact("worker", "acme/worker:1")]

EXPECTED = """\
# Your environment

You run unattended in a Linux container built from the image bank-of-anthos-dev:latest.
Each folder in the working directory is a git clone of one repository:

- bank-of-anthos: branch main, origin https://github.com/musabeytekin/bank-of-anthos.git, pushing allowed
- platform-config: branch main, origin https://github.com/acme/platform-config.git, pushing NOT allowed
- legacy: branch develop, origin git@github.com:acme/legacy.git, pushing NOT allowed

Not available: a Docker daemon, Kubernetes, sudo, installing packages. Outbound network access is available.

Services running for this task, reachable by host name from this container:
- postgres (postgres:16) on port 5432
- redis (redis:7) on port 6379
- mailpit (axllent/mailpit) on ports 1025, 8025
- worker (acme/worker:1)

The working directory is deleted when the task ends. Only what you push, attach to the result,
or write in the result survives.

Notes from the project's maintainers:
Run ./mvnw -o; the Maven cache is warm.
Ask before touching the ledger schema."""


def test_environment_text_matches_the_documented_layout():
    notes = "\n  Run ./mvnw -o; the Maven cache is warm.\nAsk before touching the ledger schema.\n\n"
    text = build_environment_text(image="bank-of-anthos-dev:latest", repositories=REPOS, services=SERVICES, notes=notes)
    assert text == EXPECTED.replace("\nRun ./mvnw", "\nRun ./mvnw")  # notes are stripped, else verbatim
    for secret in ("ghp_SECRET", "x-access-token", "QSECRET", "frag"):
        assert secret not in text
    assert "Exradius" not in text and "exradius" not in text


def test_environment_text_without_services_notes_or_repositories():
    text = build_environment_text(image="python:3.14", repositories=[], services=[], notes="   ")
    assert text == (
        "# Your environment\n\n"
        "You run unattended in a Linux container built from the image python:3.14.\n"
        "The working directory contains no repositories.\n\n"
        f"{NOT_AVAILABLE}\n\n"
        "The working directory is deleted when the task ends. Only what you push, attach to the result,\n"
        "or write in the result survives."
    )
    assert "Services running" not in text and "Notes from" not in text


@pytest.mark.parametrize(
    ("url", "public"),
    [
        ("https://user:pass@git.example/a.git", "https://git.example/a.git"),
        ("https://token@git.example:8443/a.git", "https://git.example:8443/a.git"),
        ("http://git.example/a.git?private_token=x", "http://git.example/a.git"),
        ("ssh://git@git.example/a.git", "ssh://git.example/a.git"),
        ("git@github.com:acme/a.git", "git@github.com:acme/a.git"),
        ("/srv/git/a.git", "/srv/git/a.git"),
    ],
)
def test_public_url_strips_credentials(url, public):
    assert public_url(url) == public


def test_pushing_needs_permission_a_secret_and_https():
    assert can_push("https://h/a.git", True, True)
    assert not can_push("https://h/a.git", True, False)
    assert not can_push("https://h/a.git", False, True)
    assert not can_push("git@h:a.git", True, True)


async def test_project_environment_text_from_the_database(sessions, box, configured):
    async with sessions() as session:
        project = await session.scalar(select(Project))
        project.image, project.notes = "bank-of-anthos-dev:latest", "Use the warm Maven cache."
        token = Secret(project_id=project.id, key="GH", value_encrypted=box.encrypt("ghp_x"))
        session.add(token)
        await session.flush()
        session.add_all([
            Repository(project_id=project.id, name="b-repo", url="https://u:ghp_x@github.com/acme/b.git",
                       auth_secret_id=token.id, push_allowed=True),
            Repository(project_id=project.id, name="a-repo", url="https://github.com/acme/a.git"),
            ProjectService(project_id=project.id, name="redis", image="redis:7", ports=[6379], position=1),
            ProjectService(project_id=project.id, name="postgres", image="postgres:16", ports=[5432], position=0),
        ])
        await session.commit()
        text = await project_environment_text(session, project.id, "exradius-project-default:0.1.0")
    assert "built from the image bank-of-anthos-dev:latest." in text
    assert "- a-repo: branch main, origin https://github.com/acme/a.git, pushing NOT allowed\n" \
           "- b-repo: branch main, origin https://github.com/acme/b.git, pushing allowed" in text
    assert "- postgres (postgres:16) on port 5432\n- redis (redis:7) on port 6379" in text
    assert text.endswith("Notes from the project's maintainers:\nUse the warm Maven cache.")
    assert "ghp_x" not in text


# Prompt layout and the run spec ----------------------------------------------------------------


def spec(**fields) -> RunSpecResponse:
    return RunSpecResponse(run_id="r", task_id="T-1", runtime="claude_code", mode="start", agent_prompt="You fix bugs.",
                           instructions="Fix it.", **fields)


def test_system_prompt_is_rules_then_environment_then_role():
    prompt = build_system_prompt(spec(environment_text="# Your environment\n\nfacts\n"))
    assert prompt == f"{OPERATING_RULES}\n\n# Your environment\n\nfacts\n\nYou fix bugs."
    assert build_system_prompt(spec()) == f"{OPERATING_RULES}\n\nYou fix bugs."  # older API: no section


def test_internal_spec_carries_environment_and_model(client, admin, sync_db):
    _, run_id = make_run(admin, sync_db)
    project = sync_db.scalar(select(Project))
    agent_id = sync_db.scalar(select(Task.agent_id))
    assert admin.patch(f"/api/projects/{project.id}", json={"notes": "Be careful."}).status_code == 200
    assert admin.patch(f"/api/agents/{agent_id}", json={"model": "claude-sonnet-5-5"}).json()["model"] == "claude-sonnet-5-5"
    body = client.get(f"/internal/runs/{run_id}/spec", headers={"Authorization": "Bearer tok"}).json()
    assert body["model"] == "claude-sonnet-5-5"
    assert body["environment_text"].startswith("# Your environment\n\nYou run unattended")
    assert "- api: branch main, origin https://git.example/api.git, pushing NOT allowed" in body["environment_text"]
    assert body["environment_text"].endswith("Notes from the project's maintainers:\nBe careful.")


def test_resolved_view_returns_environment_and_model(admin):
    cfg = setup_configuration(admin)
    pid, aid = cfg["project"]["id"], cfg["agent"]["id"]
    admin.post(f"/api/projects/{pid}/services", json={"name": "postgres", "image": "postgres:16", "ports": [5432]})
    resolved = admin.get(f"/api/projects/{pid}/agents/{aid}/resolved").json()
    assert resolved["model"] is None
    assert resolved["image"].startswith("exradius-project-default:")
    assert "- postgres (postgres:16) on port 5432" in resolved["environment_text"]
    assert "Notes from" not in resolved["environment_text"]


# Notes and model on the API --------------------------------------------------------------------


def test_project_notes(admin):
    project = admin.post("/api/projects", json={"slug": "p", "name": "P", "notes": "Hello"}).json()
    assert project["notes"] == "Hello"
    assert admin.patch(f"/api/projects/{project['id']}", json={"notes": None}).json()["notes"] == ""
    assert admin.post("/api/projects", json={"slug": "q", "name": "Q"}).json()["notes"] == ""


def test_agent_model(admin):
    body = {"slug": "a", "name": "A", "identity_email": "a@agents.example"}
    agent = admin.post("/api/agents", json={**body, "model": "claude-opus-5-5"}).json()
    assert agent["model"] == "claude-opus-5-5"
    aid = agent["id"]
    assert admin.patch(f"/api/agents/{aid}", json={"name": "B"}).json()["model"] == "claude-opus-5-5"
    assert admin.patch(f"/api/agents/{aid}", json={"model": "  "}).json()["model"] is None  # blank = default
    assert admin.patch(f"/api/agents/{aid}", json={"model": "claude-opus-4-6[1m]"}).json()["model"] == "claude-opus-4-6[1m]"
    assert admin.patch(f"/api/agents/{aid}", json={"model": None}).json()["model"] is None
    assert admin.patch(f"/api/agents/{aid}", json={"model": "bad model id"}).status_code == 422
    assert admin.post("/api/agents", json={**body, "slug": "b"}).json()["model"] is None


def test_provider_catalog_lists_models(admin):
    [runtime] = admin.get("/api/system/runtimes").json()
    models = runtime["providers"][0]["models"]
    assert [m["id"] for m in models] == ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    assert all(m["description"] for m in models)
    provider = admin.post("/api/providers", json={"slug": "anthropic", "name": "Anthropic", "kind": "anthropic",
                                                   "auth_method": "api_key", "credential": "sk-good"}).json()
    assert [m["id"] for m in provider["models"]] == [m["id"] for m in models]


def test_provider_test_with_a_model(admin):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/v1/models/claude-opus-5-5":
            return httpx.Response(200, json={"id": "claude-opus-5-5-20261101", "display_name": "Claude Opus 5.5"})
        return httpx.Response(404, json={"error": {"type": "not_found_error", "message": "model: nope"}})

    provider_routes.TRANSPORT = httpx.MockTransport(handler)
    try:
        provider = admin.post("/api/providers", json={"slug": "anthropic", "name": "Anthropic", "kind": "anthropic",
                                                       "auth_method": "api_key", "credential": "sk-good"}).json()
        ok = admin.post(f"/api/providers/{provider['id']}/test", json={"model": "claude-opus-5-5"}).json()
        assert ok["ok"] is True and "claude-opus-5-5-20261101" in ok["detail"]
        assert admin.get(f"/api/providers/{provider['id']}").json()["last_test"]["ok"] is True
        bad = admin.post(f"/api/providers/{provider['id']}/test", json={"model": "nope"}).json()
        assert bad["ok"] is False and "model 'nope'" in bad["error"] and "HTTP 404" in bad["error"]
        # A wrong model id says nothing against the credential: last_test is kept.
        assert admin.get(f"/api/providers/{provider['id']}").json()["last_test"]["ok"] is True
        assert seen == ["/v1/models/claude-opus-5-5", "/v1/models/nope"]
    finally:
        provider_routes.TRANSPORT = None


def test_oauth_provider_test_carries_the_model(admin, control, sync_db):
    from exradius.models import ProviderTest

    provider = admin.post("/api/providers", json={"slug": "sub", "name": "Sub", "kind": "anthropic",
                                                   "auth_method": "oauth_token", "credential": "oat"}).json()
    created = admin.post(f"/api/providers/{provider['id']}/test", json={"model": "claude-haiku-5-5"})
    assert created.status_code == 202
    test = sync_db.get(ProviderTest, uuid.UUID(created.json()["test_id"]))
    assert test.model == "claude-haiku-5-5"
    assert admin.post(f"/api/providers/{provider['id']}/test").status_code == 202  # body stays optional


# Services: API ---------------------------------------------------------------------------------


def test_service_crud_and_validation(admin):
    pid = admin.post("/api/projects", json={"slug": "p", "name": "P"}).json()["id"]
    base = f"/api/projects/{pid}/services"
    pg = admin.post(base, json={"name": "postgres", "image": "postgres:16", "ports": [5432],
                                "env": {"POSTGRES_PASSWORD": "${secret:DB_PASSWORD}", "POSTGRES_USER": "app"},
                                "ready": {"command": ["pg_isready", "-U", "app"]}})
    assert pg.status_code == 201, pg.text
    pg = pg.json()
    assert (pg["position"], pg["ready"], pg["env"]["POSTGRES_PASSWORD"]) == (0, {"command": ["pg_isready", "-U", "app"]},
                                                                             "${secret:DB_PASSWORD}")
    redis = admin.post(base, json={"name": "redis", "image": "redis:7", "ports": [6379], "ready": {"tcp": 6379}}).json()
    assert redis["position"] == 1
    plain = admin.post(base, json={"name": "mail-pit2", "image": "axllent/mailpit"}).json()
    assert plain["ready"] is None and plain["ports"] == []

    for bad in (
        {"name": "Postgres", "image": "x"},
        {"name": "1db", "image": "x"},
        {"name": "db_1", "image": "x"},
        {"name": "db", "image": ""},
        {"name": "db", "image": "x", "ports": [5432, 5432]},
        {"name": "db", "image": "x", "ports": [70000]},
        {"name": "db", "image": "x", "ready": {"tcp": 1, "command": ["x"]}},
        {"name": "db", "image": "x", "ready": {"command": []}},
        {"name": "db", "image": "x", "ready": {"http": "/"}},
        {"name": "db", "image": "x", "env": {"BAD KEY": "1"}},
    ):
        assert admin.post(base, json=bad).status_code == 422, bad
    assert admin.post(base, json={"name": "api", "image": "x"}).status_code == 422  # the API's host name
    assert admin.post(base, json={"name": "postgres", "image": "x"}).status_code == 409

    patched = admin.patch(f"{base}/{redis['id']}", json={"image": "redis:7.4", "ready": None})
    assert patched.json()["image"] == "redis:7.4" and patched.json()["ready"] is None
    assert admin.patch(f"{base}/{redis['id']}", json={"name": "postgres"}).status_code == 409
    assert admin.patch(f"{base}/{redis['id']}", json={"name": "cache"}).json()["name"] == "cache"

    order = admin.put(f"{base}/order", json={"ids": [plain["id"], redis["id"], pg["id"]]})
    assert [s["name"] for s in order.json()] == ["mail-pit2", "cache", "postgres"]
    assert [s["name"] for s in admin.get(base).json()] == ["mail-pit2", "cache", "postgres"]
    assert admin.put(f"{base}/order", json={"ids": [plain["id"]]}).status_code == 422

    other = admin.post("/api/projects", json={"slug": "o", "name": "O"}).json()["id"]
    assert admin.patch(f"/api/projects/{other}/services/{pg['id']}", json={"image": "x"}).status_code == 404
    assert admin.delete(f"{base}/{pg['id']}").status_code == 204
    assert [s["name"] for s in admin.get(base).json()] == ["mail-pit2", "cache"]


# Services: Docker, with a fake client ----------------------------------------------------------


class FakeServiceContainer:
    def __init__(self, docker, cid, image, kwargs, healthcheck=None, statuses=None, execs=None):
        self.docker, self.id, self.image, self.kwargs = docker, cid, image, kwargs
        self.labels = kwargs.get("labels") or {}
        self.attrs = {"Config": {"Healthcheck": healthcheck}, "State": {}}
        self.statuses = list(statuses or [])
        self.execs = execs or (lambda command: (0, b""))
        self.exec_calls: list[list[str]] = []
        self.removed_with = None
        self.status = "running"
        self.reload()

    def reload(self):
        state = self.statuses.pop(0) if len(self.statuses) > 1 else (self.statuses[0] if self.statuses else {})
        self.attrs["State"] = {"Status": "running", "Running": True, **state}
        self.status = self.attrs["State"]["Status"]

    def exec_run(self, command, demux=False):
        self.exec_calls.append(command)
        code, output = self.execs(command)
        return SimpleNamespace(exit_code=code, output=output)

    def logs(self, tail=None):
        return b"FATAL: password authentication failed\n"

    def remove(self, force=False, v=False):
        self.removed_with = {"force": force, "v": v}
        self.docker.container_store.pop(self.id, None)


class FakeServiceDocker:
    def __init__(self):
        self.container_store: dict[str, FakeServiceContainer] = {}
        self.network_store: dict[str, dict] = {}
        self.behaviour: dict[str, dict] = {}  # image -> FakeServiceContainer kwargs
        outer = self

        def matches(labels, filters):
            wanted = (filters or {}).get("label") or []
            wanted = [wanted] if isinstance(wanted, str) else wanted
            return all(labels.get(k) == v for k, _, v in (w.partition("=") for w in wanted))

        class Containers:
            def run(self, image, **kwargs):
                cid = f"svc{len(outer.container_store) + 1}"
                container = FakeServiceContainer(outer, cid, image, kwargs, **outer.behaviour.get(image, {}))
                outer.container_store[cid] = container
                return container

            def list(self, all=False, filters=None):
                return [c for c in outer.container_store.values() if matches(c.labels, filters)]

        class Networks:
            def get(self, name):
                if name not in outer.network_store:
                    raise NotFound(name)
                return SimpleNamespace(name=name, attrs={"Labels": outer.network_store[name]},
                                       remove=lambda: outer.network_store.pop(name))

            def create(self, name, driver=None, labels=None):
                outer.network_store[name] = labels or {}
                return self.get(name)

            def list(self, filters=None):
                return [self.get(n) for n, labels in outer.network_store.items() if matches(labels, filters)]

        self.containers, self.networks = Containers(), Networks()
        self.api = SimpleNamespace(create_endpoint_config=lambda aliases=None: {"Aliases": aliases})


@pytest.fixture
def docker_fake():
    return FakeServiceDocker()


@pytest.fixture
def docker_manager(settings, docker_fake):
    manager = ContainerManager(settings, client=docker_fake)
    clock = {"now": 0.0}
    manager.clock = lambda: clock["now"]
    manager.sleep = lambda seconds: clock.__setitem__("now", clock["now"] + seconds)
    return manager


async def test_service_starts_hardened_on_the_run_network(docker_manager, docker_fake):
    network = await docker_manager.create_run_network("run-1")
    assert network == "exradius-run-run-1"
    assert docker_fake.network_store[network] == {"exradius.run_id": "run-1", "exradius.kind": "network"}
    started = await docker_manager.start_service(
        run_id="run-1", network=network, service=ServiceSpec("postgres", "postgres:16", {"POSTGRES_PASSWORD": "pw"})
    )
    container = docker_fake.container_store[started.container_id]
    kwargs = container.kwargs
    assert kwargs["labels"] == {"exradius.run_id": "run-1", "exradius.kind": "service", "exradius.service": "postgres"}
    assert kwargs["cap_drop"] == ["ALL"] and kwargs["cap_add"] == SERVICE_CAPS
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["network"] == network and kwargs["networking_config"] == {network: {"Aliases": ["postgres"]}}
    assert kwargs["environment"] == {"POSTGRES_PASSWORD": "pw"}
    assert "mounts" not in kwargs and "ports" not in kwargs and "volumes" not in kwargs
    assert started.check == "running" and started.seconds >= 1

    again = await docker_manager.start_service(run_id="run-1", network=network, service=ServiceSpec("postgres", "postgres:16"))
    assert again.container_id == started.container_id and again.check == "already running"  # resume keeps it

    assert await docker_manager.service_run_ids() == {"run-1"}
    assert await docker_manager.remove_run_services("run-1") == 1
    assert container.removed_with == {"force": True, "v": True}  # anonymous volumes go too
    assert docker_fake.network_store == {} and docker_fake.container_store == {}


async def test_command_readiness_retries_until_it_passes(docker_manager, docker_fake):
    attempts = []

    def pg_isready(command):
        attempts.append(command)
        return (0, b"accepting connections") if len(attempts) >= 3 else (2, b"no response")

    docker_fake.behaviour["postgres:16"] = {"execs": pg_isready}
    network = await docker_manager.create_run_network("r")
    started = await docker_manager.start_service(
        run_id="r", network=network, service=ServiceSpec("postgres", "postgres:16", ready={"command": ["pg_isready", "-U", "app"]})
    )
    assert attempts == [["pg_isready", "-U", "app"]] * 3
    assert started.check == "command pg_isready -U app" and started.seconds == 2


async def test_tcp_readiness_reads_listening_sockets(docker_manager, docker_fake):
    listening = "  sl  local_address rem_address   st\n   0: 00000000:18EB 00000000:0000 0A 00000000\n"
    calls = []

    def proc_net(command):
        calls.append(command)
        return (0, b"  sl  local_address rem_address   st\n") if len(calls) < 2 else (0, listening.encode())

    docker_fake.behaviour["redis:7"] = {"execs": proc_net}
    network = await docker_manager.create_run_network("r")
    started = await docker_manager.start_service(run_id="r", network=network,
                                                 service=ServiceSpec("redis", "redis:7", ready={"tcp": 6379}))
    assert calls[0] == ["cat", "/proc/net/tcp", "/proc/net/tcp6"] and len(calls) == 2
    assert started.check == "port 6379 listening"


def test_listening_ports():
    text = (
        "  sl  local_address rem_address   st tx_queue\n"
        "   0: 00000000:1538 00000000:0000 0A 00000000:00000000\n"   # 5432 LISTEN
        "   1: 0100007F:1538 0100007F:9C40 01 00000000:00000000\n"   # established
        "   0: 00000000000000000000000000000000:18EB 00000000000000000000000000000000:0000 0A 0\n"  # 6379 v6
    )
    assert listening_ports(text) == {5432, 6379}


async def test_image_healthcheck_is_used_when_ready_is_null(docker_manager, docker_fake):
    docker_fake.behaviour["rabbitmq:4"] = {
        "healthcheck": {"Test": ["CMD", "rabbitmq-diagnostics", "ping"]},
        "statuses": [{"Health": {"Status": "starting"}}, {"Health": {"Status": "starting"}}, {"Health": {"Status": "healthy"}}],
    }
    network = await docker_manager.create_run_network("r")
    started = await docker_manager.start_service(run_id="r", network=network, service=ServiceSpec("rabbitmq", "rabbitmq:4"))
    assert started.check == "image healthcheck"


async def test_service_that_never_becomes_ready_fails_with_its_log(docker_manager, docker_fake):
    docker_fake.behaviour["postgres:16"] = {"execs": lambda command: (2, b"no response")}
    network = await docker_manager.create_run_network("r")
    with pytest.raises(ServiceFailed) as failure:
        await docker_manager.start_service(run_id="r", network=network,
                                           service=ServiceSpec("postgres", "postgres:16", ready={"command": ["pg_isready"]}))
    message = str(failure.value)
    assert message.startswith("service postgres (postgres:16) did not become ready within 120s waiting for command pg_isready")
    assert "exit 2: no response" in message and "FATAL: password authentication failed" in message


async def test_service_that_exits_fails_at_once(docker_manager, docker_fake):
    docker_fake.behaviour["postgres:16"] = {"statuses": [{"Status": "exited", "Running": False, "ExitCode": 1}]}
    network = await docker_manager.create_run_network("r")
    with pytest.raises(ServiceFailed, match=r"service postgres \(postgres:16\) exited with code 1 before it was ready"):
        await docker_manager.start_service(run_id="r", network=network, service=ServiceSpec("postgres", "postgres:16"))


async def test_run_containers_join_both_networks(settings, tmp_path):
    ran = []
    fake = SimpleNamespace(
        containers=SimpleNamespace(run=lambda image, **kw: ran.append(kw) or SimpleNamespace(id="c1")),
        api=SimpleNamespace(create_endpoint_config=lambda aliases=None: {"Aliases": aliases}),
    )
    shared = Settings(**{**settings.model_dump(), "run_network": "exradius"})
    manager = ContainerManager(shared, client=fake)
    paths = prepare.create_run_directory(settings.data_dir, "r1")
    from exradius.worker.bundles import RuntimeMount

    runtime = RuntimeMount("claude_code", "0.1.0", "vol")
    await manager.start_runner(image="img", run_id="r1", paths=paths, env={}, runtime=runtime,
                               service_network="exradius-run-r1")
    assert ran[-1]["network"] == "exradius"
    assert set(ran[-1]["networking_config"]) == {"exradius", "exradius-run-r1"}
    await manager.start_runner(image="img", run_id="r1", paths=paths, env={}, runtime=runtime)
    assert ran[-1]["network"] == "exradius" and "networking_config" not in ran[-1]


# The run user's passwd ----------------------------------------------------------------------------


def test_identity_files_map_the_run_uid_to_runner():
    passwd, group = prepare.identity_files(1000, 1000, "/home/runner")
    assert passwd == "root:x:0:0:root:/root:/bin/sh\nrunner:x:1000:1000::/home/runner:/bin/bash\n"
    assert group == "root:x:0:\nrunner:x:1000:\n"
    passwd, group = prepare.identity_files(0, 0, "/home/runner")
    assert passwd == "root:x:0:0:root:/home/runner:/bin/bash\n" and group == "root:x:0:\n"


async def test_run_and_setup_containers_mount_passwd_read_only(settings):
    ran = []
    fake = SimpleNamespace(containers=SimpleNamespace(run=lambda image, **kw: ran.append(kw) or SimpleNamespace(id="c")))
    manager = ContainerManager(settings, client=fake)
    manager.wait = lambda cid: _async(None)
    paths = prepare.create_run_directory(settings.data_dir, "r2")
    await manager.run_setup_command(image="img", run_id="r2", paths=paths, env={}, command="true")
    mounts = {m["Target"]: m for m in ran[-1]["mounts"]}
    assert mounts["/etc/passwd"]["ReadOnly"] is True and mounts["/etc/group"]["ReadOnly"] is True
    assert mounts["/etc/passwd"]["Source"] == str(paths.etc / "passwd")
    assert (paths.etc / "passwd").read_text().endswith("runner:x:1000:1000::/home/runner:/bin/bash\n")
    assert ran[-1]["user"] == "1000:1000"  # the run user, never root
    assert ran[-1]["environment"]["HOME"] == "/home/runner"


async def _async(value):
    return value


# Services in the run lifecycle ------------------------------------------------------------------


@pytest.fixture
async def with_services(sessions, box, configured):
    async with sessions() as session:
        project = await session.scalar(select(Project))
        project.setup_commands = ["make migrate"]
        session.add(Secret(project_id=project.id, key="DB_PASSWORD", value_encrypted=box.encrypt("s3cret")))
        session.add_all([
            ProjectService(project_id=project.id, name="postgres", image="postgres:16", position=0, ports=[5432],
                           env={"POSTGRES_PASSWORD": "${secret:DB_PASSWORD}", "POSTGRES_USER": "app"},
                           ready={"command": ["pg_isready", "-U", "app"]}),
            ProjectService(project_id=project.id, name="redis", image="redis:7", position=1, ports=[6379]),
            EnvVar(project_id=project.id, key="DATABASE_URL", value="postgres://app:${secret:DB_PASSWORD}@postgres:5432/app"),
        ])
        await session.commit()
        return project


async def test_run_with_services(manager, sessions, store, containers, published, with_services):
    seen = {}

    def check(run, env):
        seen["env"] = env

    containers.behaviours.append(runner(sessions, check=check))
    run_id = await submit_task(sessions, store, published)
    await manager.execute(run_id)

    assert states(published) == ["queued", "preparing", "running", "completed"]
    label = str(run_id)
    # Network, then services in order, before setup; then removal at finalize.
    assert containers.service_log == [("network", label), ("start", "postgres"), ("start", "redis"), ("remove", label)]
    assert containers.setup_networks == [f"exradius-run-{label}"]
    assert containers.started[0]["service_network"] == f"exradius-run-{label}"
    assert seen["env"]["DATABASE_URL"] == "postgres://app:s3cret@postgres:5432/app"  # project env refs resolved
    assert containers.services == {} and containers.networks == set()


async def test_service_env_resolves_secrets(manager, sessions, with_services):
    async with sessions() as session:
        specs = await manager.service_specs(session, with_services.id)
    assert [(s.name, s.env) for s in specs] == [
        ("postgres", {"POSTGRES_PASSWORD": "s3cret", "POSTGRES_USER": "app"}),
        ("redis", {}),
    ]


async def test_service_failure_fails_the_run_in_preparing(manager, sessions, store, containers, published, with_services):
    containers.service_failures["redis"] = "did not become ready within 120s waiting for running"
    run_id = await submit_task(sessions, store, published)
    await manager.execute(run_id)
    assert states(published) == ["queued", "preparing", "failed"]
    [result] = results(published)
    assert "service redis (redis:7) did not become ready within 120s" in result["error"] and "log tail" in result["error"]
    assert containers.setup_commands == [] and containers.started == []
    assert containers.services == {} and containers.networks == set()  # cleaned up at finalize


async def test_services_stay_up_while_waiting_and_resume_rejoins(manager, sessions, store, containers, published, with_services):
    from exradius.models import InfoRequest
    from exradius.worker.intake import accept_answer

    containers.behaviours += [runner(sessions, status="waiting_for_input", question="Which tenant?"), runner(sessions)]
    run_id = await submit_task(sessions, store, published)
    await manager.execute(run_id)
    assert states(published)[-1] == "waiting_for_input"
    assert set(containers.services[str(run_id)]) == {"postgres", "redis"}  # still up

    async with sessions() as session:
        request = await session.scalar(select(InfoRequest).where(InfoRequest.run_id == run_id))
    answer = {"version": 1, "task_id": "T-1", "request_id": str(request.id), "answer": "acme"}
    await accept_answer(sessions, store, json.dumps(answer).encode())
    await manager.resume(run_id)
    assert states(published)[-1] == "completed"
    assert containers.started[1]["service_network"] == f"exradius-run-{run_id}"
    assert [e for e in containers.service_log if e[0] == "start"] == [("start", "postgres"), ("start", "redis")]
    assert containers.services == {}


async def test_missing_service_secret_fails_the_run(manager, sessions, store, containers, published, with_services):
    async with sessions() as session:
        service = await session.scalar(select(ProjectService).where(ProjectService.name == "redis"))
        service.env = {"REDIS_PASSWORD": "${secret:NOPE}"}
        await session.commit()
    run_id = await submit_task(sessions, store, published)
    await manager.execute(run_id)
    [result] = results(published)
    assert result["state"] == "failed" and "secret 'NOPE' is referenced but not defined" in result["error"]


async def test_sweep_removes_services_of_finished_and_unknown_runs(manager, sessions, store, containers, published, with_services):
    containers.behaviours.append(runner(sessions, status="waiting_for_input", question="Q?"))
    waiting = await submit_task(sessions, store, published)
    await manager.execute(waiting)
    finished, unknown = str(uuid.uuid4()), str(uuid.uuid4())
    containers.services[unknown] = {"postgres": object()}
    containers.networks.add(f"exradius-run-{finished}")
    containers.networks.add("exradius-run-verify-not-a-uuid")
    await manager.sweep_orphans()
    assert set(containers.services) == {str(waiting)}  # the waiting run keeps its services
    assert containers.networks == {f"exradius-run-{waiting}"}


async def test_reconcile_keeps_services_of_a_reattached_run(manager, sessions, store, containers, published, with_services):
    from exradius.worker.containers import RunContainer

    containers.behaviours.append(runner(sessions, status="waiting_for_input", question="Q?"))
    run_id = await submit_task(sessions, store, published)
    async with sessions() as session:
        run = await session.get(Run, run_id)
        run.state, run.worker_id = State.RUNNING, "dead"
        await session.commit()
    containers.leave_running(run_id, "c-live", runner(sessions, strict=False))
    containers.existing[str(run_id)].append(RunContainer("svc-1", "service", True))
    containers.existing[str(run_id)].append(RunContainer("setup-1", "setup", False))

    async def dead(worker_id):
        return False

    attach = await manager.reconcile(dead)
    assert attach == [(run_id, "c-live")]
    assert containers.removed == ["setup-1"]  # not the service


# Verify --------------------------------------------------------------------------------------------


async def test_verification_starts_and_stops_services(manager, sessions, containers, with_services):
    manager.worker_id = "me"
    verifier = ProjectVerifier(manager)
    async with sessions() as session:
        verification = ProjectVerification(project_id=with_services.id, state="pending", steps=[])
        session.add(verification)
        await session.commit()
        vid = verification.id
    await verifier.run(vid)
    async with sessions() as session:
        verification = await session.get(ProjectVerification, vid)
    assert [(s["name"], s["state"]) for s in verification.steps] == [
        ("Create directory", "passed"),
        ("Prepare attachments folder", "passed"),
        ("Start service postgres", "passed"),
        ("Start service redis", "passed"),
        ("Setup: make migrate", "passed"),
        ("Stop services", "passed"),
        ("Remove directory", "passed"),
    ]
    assert verification.state == "passed"
    assert "postgres:16 is ready after 0.5s (running); host name postgres" in verification.steps[2]["log"]
    assert verification.steps[5]["log"] == "removed 2 service containers and the run network"
    assert containers.setup_networks == [f"exradius-run-verify-{vid}"]
    assert containers.services == {} and containers.networks == set()


async def test_verification_reports_a_failing_service(manager, sessions, containers, with_services):
    containers.service_failures["postgres"] = "exited with code 1 before it was ready"
    verifier = ProjectVerifier(manager)
    async with sessions() as session:
        verification = ProjectVerification(project_id=with_services.id, state="pending", steps=[])
        session.add(verification)
        await session.commit()
        vid = verification.id
    await verifier.run(vid)
    async with sessions() as session:
        verification = await session.get(ProjectVerification, vid)
    states_by_name = {s["name"]: s["state"] for s in verification.steps}
    assert states_by_name["Start service postgres"] == "failed"
    assert states_by_name["Start service redis"] == "skipped" and states_by_name["Setup: make migrate"] == "skipped"
    assert states_by_name["Stop services"] == "passed" and states_by_name["Remove directory"] == "passed"
    assert verification.error == "Start service postgres: service postgres (postgres:16) exited with code 1 before it was ready"


# The delivery warning ------------------------------------------------------------------------------


def test_delivery_warnings_parser():
    output = (
        "dirty\tbank-of-anthos\t3\n"
        "unpushed\tbank-of-anthos\tmain\t0\n"
        "unpushed\tbank-of-anthos\tBANK-204-fix\t2\n"
        "dirty\tplatform-config\t0\n"
        "unpushed\tplatform-config\tBANK-204-docs\t1\n"
        "unpushed\tplatform-config\t\t4\n"
        "garbage line\n"
    )
    assert delivery_warnings(output) == [
        "bank-of-anthos: uncommitted changes in 3 files were discarded with the task directory",
        "bank-of-anthos: 2 commits on BANK-204-fix were not pushed and were discarded with the task directory",
        "platform-config: 1 commit on BANK-204-docs was not pushed and was discarded with the task directory",
        "platform-config: 4 commits on a detached HEAD were not pushed and were discarded with the task directory",
    ]


def test_inspect_script_with_real_git(tmp_path, remote_repo):
    """The check's shell script against real clones (run here with sh instead of a container)."""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".exradius").mkdir()
    for name in ("clean", "pushed", "dirty"):
        git("clone", "-q", str(remote_repo), str(work / name))
    pushed, dirty = work / "pushed", work / "dirty"
    git("checkout", "-q", "-b", "T-1-fix", cwd=pushed)
    (pushed / "a.txt").write_text("a\n")
    git("add", ".", cwd=pushed)
    git("commit", "-q", "-m", "a", cwd=pushed)
    git("push", "-q", "origin", "T-1-fix", cwd=pushed)
    git("checkout", "-q", "-b", "T-1-wip", cwd=dirty)
    for i in range(2):
        (dirty / f"c{i}.txt").write_text(f"{i}\n")
        git("add", ".", cwd=dirty)
        git("commit", "-q", "-m", f"c{i}", cwd=dirty)
    (dirty / "README.md").write_text("changed\n")
    (dirty / "new.txt").write_text("untracked\n")
    script = INSPECT_CLONES_SCRIPT.replace("cd /work", f"cd {work}")
    output = subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True).stdout
    assert delivery_warnings(output) == [
        "dirty: uncommitted changes in 2 files were discarded with the task directory",
        "dirty: 2 commits on T-1-wip were not pushed and were discarded with the task directory",
    ]


async def test_finalize_adds_warnings_to_the_result(manager, sessions, store, containers, published, configured, remote_repo):
    async with sessions() as session:
        project = await session.scalar(select(Project))
        session.add(Repository(project_id=project.id, name="app", url=str(remote_repo), branch="main"))
        await session.commit()
    containers.inspect_output = "dirty\tapp\t1\nunpushed\tapp\tT-1-fix\t2\n"
    containers.behaviours.append(runner(sessions))
    run_id = await submit_task(sessions, store, published)
    await manager.execute(run_id)
    [result] = results(published)
    assert result["state"] == "completed"
    assert result["warnings"] == [
        "app: uncommitted changes in 1 file were discarded with the task directory",
        "app: 2 commits on T-1-fix were not pushed and were discarded with the task directory",
    ]
    assert containers.inspected == [str(run_id)]
    assert not manager.paths(run_id).root.exists()


async def test_finalize_without_clones_skips_the_check(manager, sessions, store, containers, published, configured):
    containers.behaviours.append(runner(sessions))
    run_id = await submit_task(sessions, store, published)
    await manager.execute(run_id)
    assert results(published)[0]["warnings"] == [] and containers.inspected == []


async def test_inspect_clones_container_is_isolated(settings):
    ran = []
    fake = SimpleNamespace(containers=SimpleNamespace(run=lambda image, **kw: ran.append(kw) or SimpleNamespace(id="c")))
    manager = ContainerManager(settings, client=fake)

    async def wait(cid):
        from exradius.worker.containers import ContainerExit

        return ContainerExit(0, "dirty\tapp\t0\n")

    manager.wait = wait
    paths = prepare.create_run_directory(settings.data_dir, "r3")
    result = await manager.inspect_clones(image="img", run_id="r3", paths=paths)
    kwargs = ran[-1]
    assert result.logs == "dirty\tapp\t0\n"
    assert kwargs["network_mode"] == "none" and kwargs["user"] == "1000:1000" and kwargs["read_only"] is True
    assert [(m["Target"], m["ReadOnly"]) for m in kwargs["mounts"]] == [("/work", True)]
    assert set(kwargs["environment"]) == {"HOME", "GIT_OPTIONAL_LOCKS", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"}


# Claude Code runtime: model and settings -----------------------------------------------------------


def test_runtime_passes_the_model(tmp_path):
    runtime = ClaudeCodeRuntime()
    base = dict(run_id="r", working_dir=tmp_path, state_dir=tmp_path, prompt="p", connections=[], env={})
    assert runtime.options(RunSpec(**base, model="claude-sonnet-5-5")).model == "claude-sonnet-5-5"
    assert runtime.options(RunSpec(**base)).model is None


def test_default_settings_turn_off_the_co_author_line(tmp_path):
    config = tmp_path / "claude"
    assert write_default_settings(config) is True
    assert json.loads((config / "settings.json").read_text()) == {"includeCoAuthoredBy": False}
    (config / "settings.json").write_text('{"theme": "dark"}')
    assert write_default_settings(config) is False  # an existing file is the agent's
    assert json.loads((config / "settings.json").read_text()) == {"theme": "dark"}
