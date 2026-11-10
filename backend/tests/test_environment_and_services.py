"""docs/environment-and-services.md: the environment section, the agent's model and the
delivery warning. Services as pod sidecars come with phase 3 (docs/kubernetes-plan.md); their
config and environment text are covered here already."""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from exradius.api import provider_routes
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
from exradius.models import Run, Task
from exradius.resolve import StoreValues, exported_secrets
from exradius.runner import delivery
from exradius.runner.delivery import delivery_warnings
from exradius.runner.prompt import OPERATING_RULES, build_system_prompt
from exradius.runtimes import RunSpec
from exradius.runtimes.claude_code import ClaudeCodeRuntime
from exradius.schemas.internal import RunSpecResponse
from exradius.schemas.project_config import parse_config
from tests.conftest import git
from tests.test_api import API_CONFIG, admin, client, control, make_run, setup_configuration, sync_db  # noqa: F401

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

Not available: a Docker daemon, Kubernetes, sudo, installing packages. Outbound network access is limited to the repositories above.

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
        f"{NOT_AVAILABLE} There is no outbound network access.\n\n"
        "The working directory is deleted when the task ends. Only what you push, attach to the result,\n"
        "or write in the result survives."
    )
    assert "Services running" not in text and "Notes from" not in text


def test_the_network_line_names_what_the_proxy_allows():
    """The agent phase's hosts (exradius.network), the same list the egress proxy enforces."""
    hosts = ["registry.npmjs.org", "*.pythonhosted.org"]
    text = build_environment_text(image="the image i", repositories=REPOS[:1], services=[], notes="", hosts=hosts)
    assert (f"{NOT_AVAILABLE} Outbound network access is limited to the repositories above and to: "
            "registry.npmjs.org, *.pythonhosted.org.") in text
    text = build_environment_text(image="the image i", repositories=[], services=[], notes="", hosts=hosts)
    assert "Outbound network access is limited to: registry.npmjs.org, *.pythonhosted.org." in text


def test_config_network_line_from_allow_catalog_and_connections():
    config = parse_config("""\
version: 1
project: {slug: shop}
secret_store: shop
network:
  allow: [registry.npmjs.org, '*.pythonhosted.org']
connections:
  gitlab: {catalog: gitlab, config: {token: '${secret:GL}', api_url: 'https://gitlab.acme.dev/api/v4'}}
  status: {url: 'https://status.example/mcp'}
  tracker: {url: 'https://mcp.tracker.example/mcp', headers: {Authorization: 'Bearer ${secret:T}'}}
  docs: {command: npx, args: [-y, docs-mcp]}
agents:
  dev: {connections: [gitlab, status, tracker]}
  scout: {connections: [docs]}
""").config
    text = config_environment_text(config, "default", agent="dev", install_allow=["artifacts.acme.dev"])
    # Install first, then the file, then the agent's connections: gitlab's launcher and API
    # URL, the plain HTTP connection's host. The tracker goes through the proxy's route.
    assert ("Outbound network access is limited to: artifacts.acme.dev, registry.npmjs.org, *.pythonhosted.org, "
            "gitlab.acme.dev, status.example.") in text
    assert "mcp.tracker.example" not in text
    scout = config_environment_text(config, "default", agent="scout")
    assert "limited to: registry.npmjs.org, *.pythonhosted.org." in scout  # registry.npmjs.org listed once


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


# The delivery warning: the runner's check after the session (docs/kubernetes.md §8.5) ---------------


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


async def test_the_check_with_real_git(tmp_path, remote_repo, monkeypatch):
    """Clean, pushed, dirty with unpushed commits, and a detached HEAD with a commit."""
    work = tmp_path / "work"
    work.mkdir()
    (work / ".exradius").mkdir()
    for name in ("clean", "pushed", "dirty", "detached"):
        git("clone", "-q", str(remote_repo), str(work / name))
    pushed, dirty, detached = work / "pushed", work / "dirty", work / "detached"
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
    git("checkout", "-q", "--detach", cwd=detached)
    (detached / "d.txt").write_text("d\n")
    git("add", ".", cwd=detached)
    git("commit", "-q", "-m", "d", cwd=detached)
    # The run's own environment does not reach the check.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "nowhere"))
    assert sorted(await delivery.check(work)) == sorted([
        "dirty: uncommitted changes in 2 files were discarded with the task directory",
        "dirty: 2 commits on T-1-wip were not pushed and were discarded with the task directory",
        "detached: 1 commit on a detached HEAD was not pushed and was discarded with the task directory",
    ])


async def test_the_check_without_clones_gives_nothing(tmp_path):
    assert await delivery.check(tmp_path / "missing") == []
    (tmp_path / "plain").mkdir()
    assert await delivery.check(tmp_path) == []


async def test_a_check_that_hangs_gives_nothing(tmp_path, remote_repo, monkeypatch):
    git("clone", "-q", str(remote_repo), str(tmp_path / "app"))
    monkeypatch.setattr(delivery, "INSPECT_CLONES_SCRIPT", "sleep 5")
    assert await delivery.check(tmp_path, timeout=0.2) == []


def test_the_check_runs_with_an_empty_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env = delivery._environment("/tmp/home")
    assert env["PATH"] == "/usr/bin:/bin" and env["HOME"] == "/tmp/home" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "ANTHROPIC_API_KEY" not in env


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