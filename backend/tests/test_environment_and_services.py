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
from exradius.crypto import hash_token
from exradius.environment import (
    NOT_AVAILABLE,
    RepoFact,
    ServiceFact,
    build_environment_text,
    can_push,
    config_environment_text,
    public_url,
    service_settings,
    workstation_label,
)
from exradius.models import ProjectVerification, Run, SecretStore, SecretStoreEntry, Task
from exradius.resolve import StoreValues, exported_secrets
from exradius.schemas.project_config import parse_config
from exradius.worker.builds import Images
from exradius.runner.prompt import OPERATING_RULES, build_system_prompt
from exradius.runtimes import RunSpec
from exradius.runtimes.claude_code import ClaudeCodeRuntime
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
from tests.conftest import BASE_CONFIG, commit_config, git
from tests.test_api import API_CONFIG, admin, client, control, make_run, setup_configuration, sync_db  # noqa: F401
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
SERVICES = [ServiceFact("postgres", "postgres:16", (5432,),
                        ("POSTGRES_USER=app", "POSTGRES_DB=app", "POSTGRES_PASSWORD=$DB_PASSWORD")),
            ServiceFact("redis", "redis:7", (6379,)),
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
- postgres (postgres:16) on port 5432, POSTGRES_USER=app, POSTGRES_DB=app, POSTGRES_PASSWORD=$DB_PASSWORD
- redis (redis:7) on port 6379
- mailpit (axllent/mailpit) on ports 1025, 8025
- worker (acme/worker:1)
A value written as $NAME is the value of your environment variable NAME.

The working directory is deleted when the task ends. Only what you push, attach to the result,
or write in the result survives.

Notes from the project's maintainers:
Run ./mvnw -o; the Maven cache is warm.
Ask before touching the ledger schema."""


def test_environment_text_matches_the_documented_layout():
    notes = "\n  Run ./mvnw -o; the Maven cache is warm.\nAsk before touching the ledger schema.\n\n"
    text = build_environment_text(image="the image bank-of-anthos-dev:latest", repositories=REPOS, services=SERVICES, notes=notes)
    assert text == EXPECTED.replace("\nRun ./mvnw", "\nRun ./mvnw")  # notes are stripped, else verbatim
    for secret in ("ghp_SECRET", "x-access-token", "QSECRET", "frag"):
        assert secret not in text
    assert "Exradius" not in text and "exradius" not in text


def test_service_settings_show_plain_values_and_name_secrets():
    env = {"POSTGRES_PASSWORD": "${secret:DB_PASSWORD}", "POSTGRES_USER": "app", "PGPORT": 5432,
           "URL": "postgres://app:${secret:DB_PASSWORD}@postgres/app", "TOKEN": "${secret:GLOBAL_ONLY}"}
    assert service_settings(env, {"DB_PASSWORD"}) == (
        "POSTGRES_USER=app",
        "PGPORT=5432",
        "POSTGRES_PASSWORD=$DB_PASSWORD",
        "URL=postgres://app:$DB_PASSWORD@postgres/app",
        "TOKEN=<a secret you do not have>",  # global secrets are not in the run environment
    )
    assert service_settings({}, set()) == ()


def test_secret_hint_only_when_a_setting_names_a_variable():
    plain = [ServiceFact("redis", "redis:7", (6379,), ("REDIS_ARGS=--save ''",))]
    text = build_environment_text(image="the image i", repositories=[], services=plain, notes="")
    assert "- redis (redis:7) on port 6379, REDIS_ARGS=--save ''\n\nThe working" in text
    assert "environment variable NAME" not in text


def test_environment_text_without_services_notes_or_repositories():
    text = build_environment_text(image="the image python:3.14", repositories=[], services=[], notes="   ")
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


ENV_CONFIG = """\
version: 1
project: {slug: bank}
secret_store: bank
workstation: {image: "bank-of-anthos-dev:latest"}
repositories:
  - {name: b-repo, url: "https://u:ghp_x@github.com/acme/b.git", credential: "${secret:GH}", push: true}
  - {name: a-repo, url: "https://github.com/acme/a.git", branch: main}
services:
  postgres:
    image: postgres:16
    ports: [5432]
    env: {POSTGRES_USER: app, POSTGRES_PASSWORD: "${secret:DB_PASSWORD}", ODD: "${secret:odd.name}"}
  redis: {image: "redis:7", ports: [6379]}
notes: |
  Use the warm Maven cache.
agents:
  dev: {}
"""


def test_environment_text_from_the_config():
    config = parse_config(ENV_CONFIG).config
    text = config_environment_text(config, "exradius-project-default:0.1.0")
    assert "built from the image bank-of-anthos-dev:latest." in text
    # File order; a repository without a branch is on the default one.
    assert "- b-repo: the default branch, origin https://github.com/acme/b.git, pushing allowed\n" \
           "- a-repo: branch main, origin https://github.com/acme/a.git, pushing NOT allowed" in text
    assert "- postgres (postgres:16) on port 5432, POSTGRES_USER=app, POSTGRES_PASSWORD=$DB_PASSWORD, " \
           "ODD=<a secret you do not have>\n" \
           "- redis (redis:7) on port 6379\n" \
           "A value written as $NAME is the value of your environment variable NAME." in text
    assert text.endswith("Notes from the project's maintainers:\nUse the warm Maven cache.")
    assert "ghp_x" not in text
    assert exported_secrets(config) == ["DB_PASSWORD"]  # odd.name is not a variable name


def test_environment_text_names_dockerfiles():
    config = parse_config(ENV_CONFIG.replace('workstation: {image: "bank-of-anthos-dev:latest"}', "workstation: {dockerfile: docker/ws.Dockerfile}")
                          .replace('redis: {image: "redis:7", ports: [6379]}', "redis: {dockerfile: redis/Dockerfile}"),
                          config_dir=".exradius").config
    text = config_environment_text(config, "default")
    assert "You run unattended in a Linux container built from .exradius/docker/ws.Dockerfile.\n" in text
    assert "- redis (built from .exradius/redis/Dockerfile)\n" in text
    assert workstation_label(config, "default") == "built from .exradius/docker/ws.Dockerfile"


# Prompt layout and the run spec ----------------------------------------------------------------


def spec(**fields) -> RunSpecResponse:
    return RunSpecResponse(run_id="r", task_id="T-1", runtime="claude_code", mode="start", agent_prompt="You fix bugs.",
                           instructions="Fix it.", **fields)


def test_system_prompt_is_rules_then_environment_then_role():
    prompt = build_system_prompt(spec(environment_text="# Your environment\n\nfacts\n"))
    assert prompt == f"{OPERATING_RULES}\n\n# Your environment\n\nfacts\n\nYou fix bugs."
    assert build_system_prompt(spec()) == f"{OPERATING_RULES}\n\nYou fix bugs."  # older API: no section


def test_internal_spec_carries_environment_and_model(client, admin, sync_db, tmp_path):
    config = API_CONFIG.replace("    role: You fix bugs.\n", "    role: You fix bugs.\n    model: claude-sonnet-5-5\n") + "notes: Be careful.\n"
    cfg = setup_configuration(admin, tmp_path, config)
    project = cfg["project"]
    run = Run(id=uuid.uuid4(), task_id="T-9", state="running", token_hash=hash_token("tok"),
              config=project["config"], config_commit=project["commit"])
    sync_db.add_all([Task(id="T-9", project_id=uuid.UUID(project["id"]), agent="bug-fixer", instructions="x",
                          state="running"), run])
    sync_db.commit()
    body = client.get(f"/internal/runs/{run.id}/spec", headers={"Authorization": "Bearer tok"}).json()
    assert body["model"] == "claude-sonnet-5-5"
    assert body["environment_text"].startswith("# Your environment\n\nYou run unattended")
    assert "- api: branch main, origin https://git.example/api.git, pushing NOT allowed" in body["environment_text"]
    assert body["environment_text"].endswith("Notes from the project's maintainers:\nBe careful.")


def test_resolved_view_returns_environment_and_model(admin, tmp_path):
    config = API_CONFIG + "services:\n  postgres: {image: 'postgres:16', ports: [5432], env: {POSTGRES_PASSWORD: '${secret:DOCS_KEY}'}}\n"
    cfg = setup_configuration(admin, tmp_path, config)
    resolved = admin.get(f"/api/projects/{cfg['project']['id']}/agents/bug-fixer/resolved").json()
    assert resolved["model"] is None
    assert resolved["image"].startswith("exradius-project-default:")
    assert "- postgres (postgres:16) on port 5432, POSTGRES_PASSWORD=$DOCS_KEY" in resolved["environment_text"]
    assert "docs-key" not in json.dumps(resolved)
    assert "Notes from" not in resolved["environment_text"]


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


def test_runs_default_to_their_own_network_and_the_api_alias(monkeypatch, tmp_path):
    for var in ("EXRADIUS_RUN_NETWORK", "EXRADIUS_RUN_API_URL"):
        monkeypatch.delenv(var, raising=False)
    defaults = Settings(database_url="sqlite+aiosqlite:///x", secret_key="k", admin_username="a",
                        admin_password="p", broker_url="amqp://unused/", data_dir=tmp_path)
    assert defaults.run_network == "exradius-runs"
    assert defaults.run_api_url == "http://exradius-api:8000"


def test_compose_keeps_runs_off_the_stack_network():
    import yaml

    compose = yaml.safe_load((Path(__file__).parents[2] / "deploy" / "docker-compose.yml").read_text())
    assert compose["networks"]["runs"]["name"] == "exradius-runs"
    on_runs = {name for name, svc in compose["services"].items() if "runs" in (svc.get("networks") or {})}
    assert on_runs == {"api"}  # nothing else of the stack (postgres, rabbitmq, the worker) is visible to runs
    assert compose["services"]["api"]["networks"]["runs"]["aliases"] == ["exradius-api"]
    worker = compose["services"]["worker"]["environment"]
    assert worker["EXRADIUS_RUN_NETWORK"] == "exradius-runs"
    assert worker["EXRADIUS_RUN_API_URL"] == "http://exradius-api:8000"


async def test_run_containers_join_the_run_network_and_never_the_stack(settings, tmp_path):
    ran = []
    fake = SimpleNamespace(
        containers=SimpleNamespace(run=lambda image, **kw: ran.append(kw) or SimpleNamespace(id="c1")),
        api=SimpleNamespace(create_endpoint_config=lambda aliases=None: {"Aliases": aliases}),
    )
    manager = ContainerManager(settings, client=fake)
    manager.wait = lambda cid: _async(SimpleNamespace(exit_code=0, logs=""))
    paths = prepare.create_run_directory(settings.data_dir, "r1")
    from exradius.worker.bundles import RuntimeMount

    runtime = RuntimeMount("claude_code", "0.1.0", "vol")

    def networks(kw):
        return {kw["network"], *kw.get("networking_config", {})}

    await manager.start_runner(image="img", run_id="r1", paths=paths, env={}, runtime=runtime,
                               service_network="exradius-run-r1")
    assert ran[-1]["network"] == "exradius-runs" and networks(ran[-1]) == {"exradius-runs", "exradius-run-r1"}
    await manager.start_runner(image="img", run_id="r1", paths=paths, env={}, runtime=runtime)
    assert networks(ran[-1]) == {"exradius-runs"} and "networking_config" not in ran[-1]
    await manager.run_setup_command(image="img", run_id="r1", paths=paths, env={}, command="true",
                                    service_network="exradius-run-r1")
    assert networks(ran[-1]) == {"exradius-runs", "exradius-run-r1"}
    await manager.run_probe(image="img", label="p", env={}, timeout=5, runtime=runtime)
    assert networks(ran[-1]) == {"exradius-runs"}
    await manager.inspect_clones(image="img", run_id="r1", paths=paths)
    assert ran[-1]["network_mode"] == "none" and "network" not in ran[-1]
    assert all("exradius" not in networks(kw) for kw in ran if "network" in kw)


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


SERVICES_CONFIG = BASE_CONFIG + """\
secret_store: billing
workstation:
  setup: [make migrate]
services:
  postgres:
    image: postgres:16
    ports: [5432]
    env: {POSTGRES_PASSWORD: "${secret:DB_PASSWORD}", POSTGRES_USER: app}
    ready: {command: [pg_isready, -U, app]}
  redis: {image: "redis:7", ports: [6379]}
env:
  DATABASE_URL: postgres://app:${secret:DB_PASSWORD}@postgres:5432/app
"""


@pytest.fixture
async def with_services(sessions, box, configured):
    commit_config(configured["remote"], {"exradius.yaml": SERVICES_CONFIG})
    async with sessions() as session:
        secret_store = SecretStore(name="billing")
        session.add(secret_store)
        await session.flush()
        session.add(SecretStoreEntry(store_id=secret_store.id, key="DB_PASSWORD", value_encrypted=box.encrypt("s3cret")))
        await session.commit()
    return configured["project"]


async def test_run_with_services(manager, sessions, store, containers, published, with_services, accept):
    seen = {}

    def check(run, env):
        seen["env"] = env

    containers.behaviours.append(runner(sessions, check=check))
    run_id = await submit_task(accept, published)
    await manager.execute(run_id)

    assert states(published) == ["queued", "preparing", "running", "completed"]
    label = str(run_id)
    # Network, then services in order, before setup; then removal at finalize.
    assert containers.service_log == [("network", label), ("start", "postgres"), ("start", "redis"), ("remove", label)]
    assert containers.setup_networks == [f"exradius-run-{label}"]
    assert containers.started[0]["service_network"] == f"exradius-run-{label}"
    assert seen["env"]["DATABASE_URL"] == "postgres://app:s3cret@postgres:5432/app"  # project env refs resolved
    assert seen["env"]["DB_PASSWORD"] == "s3cret"  # what the section's $DB_PASSWORD names
    assert containers.services == {} and containers.networks == set()


async def test_service_env_resolves_secrets(manager, with_services):
    config = parse_config(SERVICES_CONFIG).config
    specs = manager.service_specs(config, Images("ws", {"postgres": "postgres:16", "redis": "redis:7"}),
                                  StoreValues("billing", {"DB_PASSWORD": "s3cret"}))
    assert [(s.name, s.image, s.env, s.ready) for s in specs] == [
        ("postgres", "postgres:16", {"POSTGRES_PASSWORD": "s3cret", "POSTGRES_USER": "app"}, {"command": ["pg_isready", "-U", "app"]}),
        ("redis", "redis:7", {}, None),
    ]


async def test_service_failure_fails_the_run_in_preparing(manager, sessions, store, containers, published, with_services, accept):
    containers.service_failures["redis"] = "did not become ready within 120s waiting for running"
    run_id = await submit_task(accept, published)
    await manager.execute(run_id)
    assert states(published) == ["queued", "preparing", "failed"]
    [result] = results(published)
    assert "service redis (redis:7) did not become ready within 120s" in result["error"] and "log tail" in result["error"]
    assert containers.setup_commands == [] and containers.started == []
    assert containers.services == {} and containers.networks == set()  # cleaned up at finalize


async def test_services_stay_up_while_waiting_and_resume_rejoins(manager, sessions, store, containers, published, with_services, accept):
    from exradius.models import InfoRequest
    from exradius.worker.intake import accept_answer

    containers.behaviours += [runner(sessions, status="waiting_for_input", question="Which tenant?"), runner(sessions)]
    run_id = await submit_task(accept, published)
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


async def test_missing_service_secret_fails_the_run(manager, sessions, store, containers, published, with_services, configured, accept):
    run_id = await submit_task(accept, published)
    commit_config(configured["remote"], {"exradius.yaml": SERVICES_CONFIG.replace('redis: {image: "redis:7", ports: [6379]}',
                                                                                    'redis: {image: "redis:7", env: {P: "${secret:NOPE}"}}')})
    await manager.execute(run_id)
    [result] = results(published)
    assert result["state"] == "failed" and result["error"] == "PreparationError: missing secret NOPE in store billing"


async def test_sweep_removes_services_of_finished_and_unknown_runs(manager, sessions, store, containers, published, with_services, accept):
    containers.behaviours.append(runner(sessions, status="waiting_for_input", question="Q?"))
    waiting = await submit_task(accept, published)
    await manager.execute(waiting)
    finished, unknown = str(uuid.uuid4()), str(uuid.uuid4())
    containers.services[unknown] = {"postgres": object()}
    containers.networks.add(f"exradius-run-{finished}")
    containers.networks.add("exradius-run-verify-not-a-uuid")
    await manager.sweep_orphans()
    assert set(containers.services) == {str(waiting)}  # the waiting run keeps its services
    assert containers.networks == {f"exradius-run-{waiting}"}


async def test_reconcile_keeps_services_of_a_reattached_run(manager, sessions, store, containers, published, with_services, accept):
    from exradius.worker.containers import RunContainer

    containers.behaviours.append(runner(sessions, status="waiting_for_input", question="Q?"))
    run_id = await submit_task(accept, published)
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
        ("Read config", "passed"),
        ("Create directory", "passed"),
        ("Prepare attachments folder", "passed"),
        ("Start service postgres", "passed"),
        ("Start service redis", "passed"),
        ("Setup: make migrate", "passed"),
        ("List tools docs", "passed"),
        ("Stop services", "passed"),
        ("Remove directory", "passed"),
    ]
    assert verification.state == "passed"
    assert "postgres:16 is ready after 0.5s (running); host name postgres" in verification.steps[3]["log"]
    assert verification.steps[7]["log"] == "removed 2 service containers and the run network"
    assert containers.setup_networks == [f"exradius-run-verify-{vid}"]
    assert [c["service_network"] for c in containers.listed] == [f"exradius-run-verify-{vid}"]  # services are up
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


async def test_finalize_adds_warnings_to_the_result(manager, sessions, store, containers, published, configured, remote_repo, accept):
    commit_config(configured["remote"], {"exradius.yaml": BASE_CONFIG + f"repositories:\n  - {{name: app, url: {remote_repo}}}\n"})
    containers.inspect_output = "dirty\tapp\t1\nunpushed\tapp\tT-1-fix\t2\n"
    containers.behaviours.append(runner(sessions))
    run_id = await submit_task(accept, published)
    await manager.execute(run_id)
    [result] = results(published)
    assert result["state"] == "completed"
    assert result["warnings"] == [
        "app: uncommitted changes in 1 file were discarded with the task directory",
        "app: 2 commits on T-1-fix were not pushed and were discarded with the task directory",
    ]
    assert containers.inspected == [str(run_id)]
    assert not manager.paths(run_id).root.exists()


async def test_finalize_without_clones_skips_the_check(manager, sessions, store, containers, published, configured, accept):
    containers.behaviours.append(runner(sessions))
    run_id = await submit_task(accept, published)
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


def test_runtime_settings_are_passed_on_the_command_line(tmp_path):
    """--settings outranks the repository's .claude/settings.json, so a repository cannot
    turn the co-author line back on; every setting source still loads (CLAUDE.md, .claude/)."""
    runtime = ClaudeCodeRuntime()
    base = dict(run_id="r", working_dir=tmp_path, state_dir=tmp_path, prompt="p", connections=[], env={})
    options = runtime.options(RunSpec(**base))
    assert json.loads(options.settings) == {"includeCoAuthoredBy": False, "attribution": {"commit": "", "pr": ""}}
    assert options.setting_sources is None  # user, project and local all load
    assert not (tmp_path / "claude" / "settings.json").exists()