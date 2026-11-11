"""exradius.yaml: parsing and every rule of docs/config-repo.md §2, the catalog (§5), the
snapshot's missing keys (§3) and build tags (§4)."""

from __future__ import annotations

import io
import os
import tarfile
import uuid
from pathlib import Path

import pytest

from exradius import catalog
from exradius.config_repo import (
    BUILD_DOCKERFILE,
    ConfigReadError,
    ProjectConfigSnapshot,
    build_context,
    git_host,
    host_auth,
    make_lookup,
    mirror_path,
    read_snapshot,
    tree_hash,
)
from exradius.project_state import Health
from exradius.resolve import (
    MissingSecret,
    StoreValues,
    agent_connection_specs,
    connection_view,
    repo_specs,
    workstation_environment,
)
from exradius.schemas.project_config import parse_config
from exradius.worker.builds import BuildJob, build_jobs, content_hash, image_tag
from tests.conftest import commit_config, git, make_config_repo

DESIGN_EXAMPLE = """\
version: 1

project:
  slug: bank
  name: Bank of Anthos
  description: Sample retail bank, Java and Python services.

secret_store: bank-platform

repositories:
  - name: bank-of-anthos
    url: https://github.com/acme/bank-of-anthos.git
    branch: main
    credential: ${secret:GITHUB_TOKEN}
    push: true

workstation:
  image: bank-of-anthos-dev:latest
  setup:
    - cp -r /opt/m2 ~/.m2

services:
  postgres:
    image: postgres:16
    env:
      POSTGRES_USER: app
      POSTGRES_DB: app
      POSTGRES_PASSWORD: ${secret:DB_PASSWORD}
    ports: [5432]
    ready: {command: [pg_isready, -U, app]}

env:
  DATABASE_URL: postgres://app:${secret:DB_PASSWORD}@postgres:5432/app

notes: |
  Python services: uv run pytest in the service directory. Java: mvn test per module.

connections:
  github:
    catalog: github
    config: {token: "${secret:GITHUB_TOKEN}"}
  internal-docs:
    command: npx
    args: ["-y", "@acme/docs-mcp@1.4.2"]
    env: {DOCS_TOKEN: "${secret:DOCS_TOKEN}"}
  tracker:
    url: https://mcp.example.com/mcp
    headers: {Authorization: "Bearer ${secret:TRACKER_TOKEN}"}

agents:
  developer:
    name: Developer
    role: |
      You are a software developer on this team.
    model: claude-sonnet-5-5
    provider: main
    runtime: claude_code
    identity: {name: Developer, email: dev-bot@acme.com}
    connections: [github]
  scout:
    role: You investigate and report. You never change code.
"""

MINIMAL = "version: 1\nproject: {slug: p}\nagents:\n  a: {}\n"


def errors(text: str, **kwargs) -> list[str]:
    result = parse_config(text, **kwargs)
    assert result.config is None
    return result.errors


def warnings(text: str, **kwargs) -> list[str]:
    result = parse_config(text, **kwargs)
    assert result.errors == [], result.errors
    return result.warnings


def test_the_design_example_parses():
    result = parse_config(DESIGN_EXAMPLE)
    assert result.errors == [] and result.warnings == []
    assert result.required_keys == ["DB_PASSWORD", "DOCS_TOKEN", "GITHUB_TOKEN", "TRACKER_TOKEN"]
    assert result.store == "bank-platform"
    config = result.config
    assert config.project.display_name == "Bank of Anthos"
    assert config.repositories[0].push is True and config.services["postgres"].ready.command == ["pg_isready", "-U", "app"]
    assert config.agents["developer"].provider == "main" and config.agent_name("scout") == "Scout"
    assert config.identity("developer") == ("Developer", "dev-bot@acme.com")
    assert config.identity("scout") == ("Scout", "scout@bank.exradius.local")
    assert config.connection_definition("internal-docs") == {
        "name": "internal-docs", "transport": "stdio", "command": "npx", "args": ["-y", "@acme/docs-mcp@1.4.2"],
        "url": None, "env": {"DOCS_TOKEN": "${secret:DOCS_TOKEN}"}, "headers": {},
    }


def test_minimal_file_and_defaults():
    config = parse_config(MINIMAL).config
    assert config.project.display_name == "p" and config.secret_store is None
    assert config.repositories == [] and config.services == {} and config.workstation.setup == []
    assert config.agents["a"].runtime == "claude_code" and config.agents["a"].role == ""


def test_flow_mappings_need_quoted_secrets():
    """``{token: ${secret:X}}`` is not YAML: ``{`` starts a flow mapping."""
    [message] = errors(MINIMAL + "env: {A: ${secret:X}}\n")
    assert message.startswith("(file): not valid YAML: line 5")


@pytest.mark.parametrize(("text", "message"), [
    (MINIMAL.replace("version: 1", "version: 2"), "version: must be 1"),
    (MINIMAL.replace("version: 1\n", ""), "version: required"),
    (MINIMAL + "agnets: {}\n", "agnets: unknown key"),
    (MINIMAL.replace("{slug: p}", "{slug: p, owner: me}"), "project.owner: unknown key"),
    (MINIMAL + "services:\n  db: {image: x, imgae: y}\n", "services.db.imgae: unknown key"),
    (MINIMAL + "services:\n  db: {image: x, ready: {cmd: [a]}}\n", "services.db.ready.cmd: unknown key"),
    (MINIMAL + "repositories:\n  - {name: a, url: u, pushh: true}\n", "repositories[0].pushh: unknown key"),
    (MINIMAL.replace("a: {}", "a: {identity: {mail: x@y}}"), "agents.a.identity.mail: unknown key"),
    (MINIMAL + "workstation: {image: x, steps: []}\n", "workstation.steps: unknown key"),
    (MINIMAL.replace("{slug: p}", "{slug: Bad_Slug}"), "project.slug: must match ^[a-z][a-z0-9-]*$"),
    (MINIMAL.replace("agents:\n  a: {}\n", "agents: {}\n"), "agents: at least one agent is required"),
    (MINIMAL.replace("agents:\n  a: {}\n", ""), "agents: required"),
    (MINIMAL + "secret_store: Bad\n", "secret_store: must match ^[a-z][a-z0-9-]*$"),
    (MINIMAL + "repositories:\n  - {name: a, url: u, push: 'yes'}\n", "repositories[0].push: must be true or false"),
    (MINIMAL + "env: {A: true}\n", "env.A: write true, false, yes or no in quotes: the value must be a string"),
    (MINIMAL + "env: {1A: x}\n", "env.1A (key): must match ^[A-Za-z_][A-Za-z0-9_]*$"),
    (MINIMAL + "services:\n  db: {image: x, ports: [70000]}\n", "services.db.ports[0]: Input should be less than or equal to 65535"),
    (MINIMAL.replace("a: {}", "a: {runtime: codex}"), "agents.a.runtime: Input should be 'claude_code'"),
    (MINIMAL.replace("a: {}", "a: {model: 'bad model'}"), "agents.a.model: must match ^[A-Za-z0-9][A-Za-z0-9._:@/\\[\\]-]*$"),
    (MINIMAL + "notes: [a]\n", "notes: must be a string"),
    ("- a\n", "(file): must be a mapping with version, project and agents"),
])
def test_structure_errors_carry_the_yaml_path(text, message):
    assert message in errors(text)


def test_numbers_become_strings():
    config = parse_config(MINIMAL + "env: {PORT: 5432, RATIO: 1.5}\nrepositories:\n  - {name: a, url: u, branch: 2.0}\n").config
    assert config.env == {"PORT": "5432", "RATIO": "1.5"} and config.repositories[0].branch == "2.0"


def test_duplicate_keys_are_errors():
    [message] = errors(MINIMAL + "  a: {}\n")
    assert message == "(file): not valid YAML: line 5, column 3: duplicate key 'a'"
    assert errors(MINIMAL + "env:\n  X: 1\n  X: 2\n")[0].endswith("duplicate key 'X'")


def test_duplicate_names():
    text = MINIMAL + "repositories:\n  - {name: app, url: u}\n  - {name: app, url: v}\n"
    assert errors(text) == ["repositories[1].name: duplicate repository name 'app'"]
    text = MINIMAL.replace("a: {}", "a: {connections: [x, x]}") + "connections:\n  x: {url: 'http://x'}\n"
    assert errors(text) == ["agents.a.connections[1]: 'x' is listed twice"]


@pytest.mark.parametrize(("extra", "path"), [
    ("notes: ${secret:A}\n", "notes"),
    ("workstation:\n  setup: ['echo ${secret:A}']\n", "workstation.setup[0]"),
    ("workstation:\n  image: ${secret:A}\n", "workstation.image"),
    ("services:\n  db: {image: '${secret:A}'}\n", "services.db.image"),
    ("services:\n  db: {image: x, ready: {command: ['${secret:A}']}}\n", "services.db.ready.command[0]"),
    ("repositories:\n  - {name: a, url: 'https://${secret:A}@h/x'}\n", "repositories[0].url"),
    ("connections:\n  c: {command: x, args: ['${secret:A}']}\n", "connections.c.args[0]"),
    ("connections:\n  c: {url: 'http://h/${secret:A}'}\n", "connections.c.url"),
])
def test_secrets_only_in_their_places(extra, path):
    text = MINIMAL.replace("a: {}", "a: {role: '${secret:R}'}") + "secret_store: s\n" + extra
    found = errors(text)
    assert f"{path}: secrets are not allowed here" in found
    assert "agents.a.role: secrets are not allowed here" in found


def test_secrets_in_allowed_places_are_required_keys():
    text = MINIMAL + (
        "secret_store: s\n"
        "repositories:\n  - {name: a, url: u, credential: '${secret:GIT}'}\n"
        "services:\n  db: {image: x, env: {P: '${secret:DB}'}}\n"
        "env: {URL: 'x://${secret:DB}:${secret:EXTRA.v2}@h'}\n"
        "connections:\n"
        "  c1: {catalog: github, config: {token: '${secret:GH}'}}\n"
        "  c2: {command: x, env: {T: '${secret:C2}'}}\n"
        "  c3: {url: 'http://h', headers: {A: 'Bearer ${secret:C3}'}}\n"
    )
    result = parse_config(text)
    assert result.errors == [] and result.required_keys == ["C2", "C3", "DB", "EXTRA.v2", "GH", "GIT"]


def test_malformed_and_storeless_references():
    assert errors(MINIMAL + "secret_store: s\nenv: {A: '${secret:has space}'}\n") == [
        "env.A: malformed secret reference; write ${secret:NAME} with a NAME of letters, digits, '_', '.' and '-'"
    ]
    assert errors(MINIMAL + "env: {A: '${secret:X}', B: '${secret:Y}'}\n") == [
        "secret_store: required, because the file references secrets (X, Y)"
    ]


def test_repository_rules():
    assert errors(MINIMAL + "repositories:\n  - {name: a, url: u, push: true}\n") == ["repositories[0].push: needs a credential"]
    assert warnings(MINIMAL + "repositories:\n  - {name: a, url: u, credential: ghp_literal}\n") == [
        "repositories[0].credential: written in the file; use ${secret:NAME}"
    ]
    assert "repositories[0].name: must match ^[A-Za-z0-9][A-Za-z0-9._-]*$" in errors(MINIMAL + "repositories:\n  - {name: ../x, url: u}\n")


def test_service_rules():
    def service_errors(block: str, repos: str = "") -> list[str]:
        return errors(MINIMAL + repos + "services:\n" + block)

    assert service_errors("  api: {image: x}\n") == ["services.api: the name 'api' is reserved"]
    assert service_errors("  exradius-db: {image: x}\n") == ["services.exradius-db: the name 'exradius-db' is reserved"]
    assert service_errors("  workstation: {image: x}\n") == ["services.workstation: the name 'workstation' is reserved"]
    assert service_errors("  Db: {image: x}\n") == ["services.Db: service names must match ^[a-z][a-z0-9-]*$"]
    assert service_errors("  app: {image: x}\n", "repositories:\n  - {name: app, url: u}\n") == [
        "services.app: the name 'app' is also a repository folder"
    ]
    assert service_errors("  db: {ports: [1]}\n") == ["services.db: needs image or dockerfile"]
    assert service_errors("  db: {image: x, dockerfile: D}\n") == ["services.db: give image or dockerfile, not both"]
    assert service_errors("  db: {image: x, context: c}\n") == ["services.db.context: only with dockerfile"]
    assert service_errors("  db: {image: x, ports: [1, 1]}\n") == ["services.db.ports: ports must be unique"]
    assert service_errors("  db: {image: x, ready: {}}\n") == ["services.db.ready: give exactly one of command or tcp"]
    assert service_errors("  db: {image: x, ready: {tcp: 1, command: [a]}}\n") == ["services.db.ready: give exactly one of command or tcp"]
    assert parse_config(MINIMAL + "services:\n  db: {image: x, ready: {tcp: 5432}}\n").config.services["db"].ready.tcp == 5432


def tree(**entries: str):
    """A ``lookup`` over a fixed set of repository paths."""
    return lambda path: "tree" if path == "" else entries.get(path)


def test_dockerfile_paths_stay_inside_the_repository():
    station = MINIMAL + "workstation: {dockerfile: %s}\n"
    assert errors(station % "/etc/Dockerfile") == ["workstation.dockerfile: must be a relative path inside the repository (no '..')"]
    assert errors(station % "../Dockerfile", config_dir=".exradius") == [
        "workstation.dockerfile: must be a relative path inside the repository (no '..')"
    ]
    assert errors(MINIMAL + "workstation: {dockerfile: D, context: ../x}\n") == [
        "workstation.context: must be a relative path inside the repository (no '..')"
    ]
    lookup = tree(**{"docker": "tree", "docker/ws.Dockerfile": "blob", "link": "link", "file": "blob"})
    assert errors(station % "docker/missing", lookup=lookup) == ["workstation.dockerfile: docker/missing does not exist in the repository"]
    assert errors(station % "link/Dockerfile", lookup=tree(**{"link/Dockerfile": "link", "link": "link"})) == [
        "workstation.dockerfile: link/Dockerfile is or passes through a symlink",
        "workstation.context: link is or passes through a symlink",
    ]
    assert errors(station % "docker", lookup=lookup) == ["workstation.dockerfile: docker is not a file"]
    assert errors(MINIMAL + "workstation: {dockerfile: docker/ws.Dockerfile, context: file}\n", lookup=lookup) == [
        "workstation.context: file is not a directory"
    ]


def test_build_paths_are_stored_from_the_repository_root():
    lookup = tree(**{".exradius": "tree", ".exradius/ws.Dockerfile": "blob", ".exradius/db": "tree",
                     ".exradius/db/Dockerfile": "blob", "": "tree"})
    text = MINIMAL + "workstation: {dockerfile: ./ws.Dockerfile}\nservices:\n  db: {dockerfile: db/Dockerfile, context: .}\n"
    config = parse_config(text, config_dir=".exradius", lookup=lookup).config
    assert (config.workstation.dockerfile, config.workstation.context) == (".exradius/ws.Dockerfile", ".exradius")
    assert (config.services["db"].dockerfile, config.services["db"].context) == (".exradius/db/Dockerfile", ".exradius")
    assert build_jobs(config) == [BuildJob("workstation", ".exradius/ws.Dockerfile", ".exradius"),
                                  BuildJob("db", ".exradius/db/Dockerfile", ".exradius")]
    assert [job.step for job in build_jobs(config)] == ["Build workstation", "Build service db"]


def test_connection_rules():
    def conn_errors(block: str) -> list[str]:
        return errors(MINIMAL + "secret_store: s\nconnections:\n" + block)

    assert conn_errors("  x: {command: a, url: b}\n") == ["connections.x: give exactly one of catalog, command or url"]
    assert conn_errors("  x: {args: [a]}\n") == ["connections.x: give exactly one of catalog, command or url"]
    assert conn_errors("  x: {command: a, headers: {A: b}}\n") == ["connections.x.headers: not used with command"]
    assert conn_errors("  x: {url: a, config: {k: v}}\n") == ["connections.x.config: not used with url"]
    assert conn_errors("  exradius: {url: a}\n") == ["connections.exradius: the name 'exradius' is reserved for the built-in connection"]
    assert conn_errors("  X: {url: a}\n") == ["connections.X: connection names must match ^[a-z][a-z0-9-]*$"]
    assert conn_errors("  x: {catalog: gitbub}\n") == [
        "connections.x.catalog: unknown catalog entry 'gitbub' (known: github, gitlab, postgres, slack, sentry, "
        "linear, jira, fetch, playwright, filesystem)"
    ]
    assert conn_errors("  x: {catalog: github}\n") == ["connections.x.config.token: required by catalog github (GitHub token)"]
    assert conn_errors("  x: {catalog: github, config: {token: t, tokn: u}}\n") == [
        "connections.x.config.tokn: unknown config key for catalog github (known: token)"
    ]
    assert conn_errors("  x: {catalog: jira, config: {url: u}}\n") == [
        "connections.x.config.username: required by catalog jira (Jira username)",
        "connections.x.config.api_token: required by catalog jira (Jira API token)",
    ]
    assert conn_errors("  x: {catalog: fetch, config: {a: b}}\n") == [
        "connections.x.config.a: unknown config key for catalog fetch (known: none)"
    ]
    assert errors(MINIMAL.replace("a: {}", "a: {connections: [nope]}")) == ["agents.a.connections[0]: no connection named 'nope'"]
    assert errors(MINIMAL + "  Bad: {}\n") == ["agents.Bad: agent names must match ^[a-z][a-z0-9-]*$"]
    # ``type`` from mcpServers blocks is accepted and ignored.
    config = parse_config(MINIMAL + "connections:\n  x: {type: http, url: 'http://h'}\n").config
    assert config.connection_definition("x")["transport"] == "http"


def test_launchers_on_the_default_workstation_are_warnings():
    text = MINIMAL + "connections:\n  fs: {catalog: filesystem}\n  docs: {command: uvx, args: [docs-mcp@1.0.0]}\n  web: {url: 'http://h'}\n"
    assert warnings(text) == [
        "connections.fs: needs npx, which the default workstation image does not have",
        "connections.docs: needs uvx, which the default workstation image does not have",
    ]
    assert warnings(text + "workstation: {image: node:22}\n") == []


# The catalog and resolution -----------------------------------------------------------------------


def test_catalog_expansion():
    github = catalog.expand(catalog.entry("github"), {"token": "${secret:GH}"})
    assert github == {"transport": "http", "command": None, "args": [], "url": "https://api.githubcopilot.com/mcp/",
                      "env": {}, "headers": {"Authorization": "Bearer ${secret:GH}"}}
    gitlab = catalog.expand(catalog.entry("gitlab"), {"token": "t"})
    assert gitlab["env"] == {"GITLAB_PERSONAL_ACCESS_TOKEN": "t", "GITLAB_API_URL": "https://gitlab.com/api/v4"}
    assert catalog.expand(catalog.entry("filesystem"), {})["args"][-1] == "/work"
    assert catalog.expand(catalog.entry("filesystem"), {"path": "/work/app"})["args"] == [
        "-y", "@modelcontextprotocol/server-filesystem@2.3.0", "/work/app"]
    assert {e.key for e in catalog.CATALOG if e.requires == "uvx"} == {"postgres", "jira", "fetch"}


def test_connection_specs_resolve_from_the_store():
    config = parse_config(DESIGN_EXAMPLE).config
    secrets = StoreValues("bank-platform", {"GITHUB_TOKEN": "ghp_1", "DB_PASSWORD": "pw"})
    [github] = agent_connection_specs(config, "developer", secrets)
    assert github["headers"] == {"Authorization": "Bearer ghp_1"}
    assert agent_connection_specs(config, "scout", secrets) == []
    assert connection_view(config.connection_definition("tracker"))["headers"] == {"Authorization": "Bearer $TRACKER_TOKEN"}
    with pytest.raises(MissingSecret, match="missing secret TRACKER_TOKEN in store bank-platform"):
        config.agents["scout"].connections.append("tracker")
        agent_connection_specs(config, "scout", secrets)
    env = workstation_environment(config, secrets)
    assert env == {"DB_PASSWORD": "pw", "DATABASE_URL": "postgres://app:pw@postgres:5432/app"}
    [repo] = repo_specs(config, secrets)
    assert (repo.auth, repo.push_allowed, repo.branch) == ("ghp_1", True, "main")
    assert repo_specs(config, None)[0].auth is None


# Missing keys and the reasons tasks are refused -------------------------------------------------------


def test_health_reasons():
    def health(**fields) -> Health:
        return Health(**{"errors": [], "store": "s", "store_exists": True, "required_keys": ["A", "B"], "missing_keys": [], **fields})

    assert health().reason is None and health().status == "ok"
    assert health(errors=["x: bad", "y: worse"]).reason == "config error: x: bad"
    assert health(errors=["x"]).status == "config_errors"
    assert health(store_exists=False, missing_keys=["A", "B"]).reason == "secret store s does not exist"
    assert health(missing_keys=["B"]).reason == "missing secret B in store s"
    assert health(missing_keys=["A", "B"]).reason == "missing secrets A, B in store s"
    assert health(missing_keys=["A"]).status == "missing_secrets"
    # A store the file names but no secret needs is not required to exist.
    assert health(store_exists=False, required_keys=[]).runnable


# Reading the repository ---------------------------------------------------------------------------------


def test_git_hosts_and_auth():
    assert git_host("https://GitHub.com/acme/x.git") == "github.com"
    assert git_host("https://user:pw@gitlab.example:8443/x.git") == "gitlab.example"
    assert git_host("git@github.com:acme/x.git") == "github.com"
    assert git_host("ssh://git@github.com/acme/x.git") == "github.com"
    assert git_host("/var/lib/exradius/e2e/config.git") is None and git_host("file:///srv/x.git") is None
    assert host_auth("token", "ghp_1") == "x-access-token:ghp_1" and host_auth("basic", "u:p") == "u:p"


def test_read_snapshot_finds_the_file_and_round_trips(tmp_path):
    remote = make_config_repo(tmp_path, {".exradius/exradius.yaml": MINIMAL + "env: {A: '${secret:X}'}\nsecret_store: s\n"})
    data = tmp_path / "data"
    project_id = uuid.uuid4()
    snapshot = read_snapshot(data, project_id, str(remote))
    assert (snapshot.path, snapshot.branch, snapshot.config_dir) == (".exradius/exradius.yaml", "main", ".exradius")
    assert snapshot.required_keys == ["X"] and snapshot.store == "s"
    assert (mirror_path(data, project_id) / "HEAD").exists()
    again = ProjectConfigSnapshot.from_json(snapshot.to_json())
    assert again.to_json() == snapshot.to_json() and again.config == snapshot.config

    commit_config(remote, {"exradius.yaml": MINIMAL})  # the root wins
    assert read_snapshot(data, project_id, str(remote)).path == "exradius.yaml"
    assert read_snapshot(data, project_id, str(remote), path="/.exradius/exradius.yaml").path == ".exradius/exradius.yaml"
    with pytest.raises(ConfigReadError, match="no other.yaml on branch main"):
        read_snapshot(data, project_id, str(remote), path="other.yaml")


def test_the_mirror_follows_the_remotes_default_branch(tmp_path):
    remote = make_config_repo(tmp_path, {"exradius.yaml": MINIMAL})
    work = tmp_path / "config-work"
    git("checkout", "-q", "-b", "next", cwd=work)
    (work / "exradius.yaml").write_text(MINIMAL.replace("{slug: p}", "{slug: q}"))
    git("commit", "-q", "-am", "next", cwd=work)
    git("push", "-q", "origin", "next", cwd=work)
    data, pid = tmp_path / "data", uuid.uuid4()
    first = read_snapshot(data, pid, str(remote))
    assert (first.branch, first.config.project.slug) == ("main", "p")
    git("symbolic-ref", "HEAD", "refs/heads/next", cwd=remote)  # the remote's default changes
    second = read_snapshot(data, pid, str(remote))
    assert (second.branch, second.config.project.slug) == ("next", "q")
    assert read_snapshot(data, pid, str(remote), branch="main").config.project.slug == "p"


def test_lookup_reports_symlinks(tmp_path):
    remote = make_config_repo(tmp_path, {"exradius.yaml": MINIMAL, "real/Dockerfile": "FROM x\n"})
    work = tmp_path / "config-work"
    os.symlink("real", work / "alias")
    os.symlink("/etc/passwd", work / "outside")
    commit_config(remote, {})
    commit = git("rev-parse", "HEAD", cwd=work).strip()
    lookup = make_lookup(remote, commit)
    assert lookup("real") == "tree" and lookup("real/Dockerfile") == "blob"
    assert lookup("alias") == "link" and lookup("alias/Dockerfile") == "link" and lookup("outside") == "link"
    assert lookup("nope") is None and lookup("real/Dockerfile/x") is None and lookup("") == "tree"


# Build tags --------------------------------------------------------------------------------------------


def head(tmp_path: Path) -> str:
    return git("rev-parse", "HEAD", cwd=tmp_path / "config-work").strip()


def test_build_tags_follow_the_context_tree_hash(tmp_path):
    remote = make_config_repo(tmp_path, {"exradius.yaml": MINIMAL, "ws/Dockerfile": "FROM ubuntu\n", "ws/a.sh": "a\n"})
    job = BuildJob("workstation", "ws/Dockerfile", "ws")
    first = head(tmp_path)
    digest = content_hash(remote, first, job)
    assert digest == git("rev-parse", f"{first}:ws", cwd=remote).strip() == tree_hash(remote, first, "ws")
    assert image_tag("bank", "workstation", digest) == f"exradius/bank/workstation:{digest[:12]}"

    commit_config(remote, {"README": "elsewhere\n"})
    assert content_hash(remote, head(tmp_path), job) == digest  # unchanged context: same tag
    commit_config(remote, {"ws/a.sh": "b\n"})
    assert content_hash(remote, head(tmp_path), job) != digest


def test_a_dockerfile_outside_the_context_is_part_of_the_hash_and_the_tar(tmp_path):
    remote = make_config_repo(tmp_path, {"exradius.yaml": MINIMAL, "db.Dockerfile": "FROM postgres\n", "db/init.sql": "x\n"})
    job = BuildJob("db", "db.Dockerfile", "db")
    before = content_hash(remote, head(tmp_path), job)
    data, name = build_context(remote, head(tmp_path), "db", "db.Dockerfile")
    assert name == BUILD_DOCKERFILE
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        assert {m.name for m in archive.getmembers()} == {"init.sql", BUILD_DOCKERFILE}
        assert archive.extractfile(BUILD_DOCKERFILE).read() == b"FROM postgres\n"
    commit_config(remote, {"db.Dockerfile": "FROM postgres:16\n"})
    assert content_hash(remote, head(tmp_path), job) != before

    data, name = build_context(remote, head(tmp_path), "", "db.Dockerfile")  # the root as context
    assert name == "db.Dockerfile"
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        assert {"exradius.yaml", "db.Dockerfile", "db/init.sql"} <= {m.name for m in archive.getmembers()}


def test_ordered_config_columns_are_plain_json():
    """JSONB reorders object keys; a config's order (services start in file order) must survive."""
    from sqlalchemy.dialects import postgresql

    from exradius.models import Project, Run

    for column in (Project.__table__.c.snapshot, Run.__table__.c.config):
        assert column.type.compile(dialect=postgresql.dialect()) == "JSON"
