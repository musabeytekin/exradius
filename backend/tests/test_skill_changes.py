"""The update skill's change report (skill/skills/exradius-update/scripts/exradius_changes.py)
on local git repositories: which commit it compares from, what it lists, and what it must
never print. The script is standard library only and not a package, so it is loaded by path.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[2] / "skill" / "skills"
SCRIPT = SKILLS / "exradius-update" / "scripts" / "exradius_changes.py"

CONFIG = """\
version: 1
project:
  slug: app
repositories:
  - name: app
    url: https://github.com/acme/app.git
    branch: main
workstation:
  image: node:20
  setup:
    - cd app && pnpm install --frozen-lockfile
    - cd app && ./scripts/prepare-db.sh && cp config/database.yml.sample config/database.yml
services:
  db:
    image: postgres:16
    ports: [5432]
env:
  DATABASE_URL: postgres://db/app
notes: |
  From /work/app: `pnpm test src/a.test.js`. The seed is in db/seeds.rb.
agents:
  developer:
    name: Developer
    role: You take tickets that ask for a change to the code.
"""


@pytest.fixture(scope="module")
def changes() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("exradius_changes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(cwd: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull)
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()


def write(root: Path, files: dict[str, str | None]) -> None:
    for name, text in files.items():
        path = root / name
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


def commit(root: Path, files: dict[str, str | None], date: str) -> str:
    write(root, files)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "change", date=date)
    return git(root, "rev-parse", "HEAD")


FIRST = {
    ".nvmrc": "20\n",
    "package.json": json.dumps({"packageManager": "pnpm@9.1.0", "scripts": {"test": "vitest run"},
                                "dependencies": {"express": "4"}}),
    "pnpm-lock.yaml": 'lockfileVersion: "9.0"\n',
    "docker-compose.yml": "services:\n  db:\n    image: postgres:16\n",
    ".env.example": "DATABASE_URL=postgres://localhost/app\nOLD_KEY=1\n",
    ".github/workflows/ci.yml": "jobs:\n  test:\n    steps:\n      - with:\n          node-version: 20\n",
    "scripts/prepare-db.sh": "echo prepare\n",
    "config/database.yml.sample": "test: {}\n",
    "db/seeds.rb": "seed\n",
    "src/a.js": "a\n",
}
SECOND = {
    ".nvmrc": "22\n",
    "package.json": json.dumps({"packageManager": "pnpm@10.2.0",
                                "scripts": {"test": "vitest run --pool=forks", "e2e": "playwright test"},
                                "dependencies": {"express": "5", "sharp": "1"}}),
    "pnpm-lock.yaml": 'lockfileVersion: "9.0"\nimporters: {}\n',
    "docker-compose.yml": "services:\n  db:\n    image: postgres:17\n  search:\n    image: getmeili/meilisearch:v1.8\n",
    ".env.example": "DATABASE_URL=postgres://localhost/app\nMEILI_MASTER_KEY=not-to-be-printed\n# SENTRY_DSN=\n",
    ".github/workflows/ci.yml": "jobs:\n  test:\n    steps:\n      - with:\n          node-version: 22\n",
    ".npmrc": "//registry.npmjs.org/:_authToken=npm_not-to-be-printed\n",
    "scripts/prepare-db.sh": None,
    "config/database.yml.sample": "test: {}\ndevelopment: {}\n",
    "worker/go.mod": "module worker\n\ngo 1.23\n",
    "AGENTS.md": "# Rules\n",
    "src/a.js": "b\n",
}


@pytest.fixture
def project(tmp_path: Path):
    """A code repository with two commits and a config repository written between them."""
    code, config = tmp_path / "app", tmp_path / "exradius-app"
    code.mkdir()
    config.mkdir()
    git(code, "init", "-q", "-b", "main")
    git(code, "remote", "add", "origin", "git@github.com:acme/app.git")
    first = commit(code, FIRST, "2026-10-01T10:00:00+00:00")
    git(config, "init", "-q", "-b", "main")
    commit(config, {"exradius.yaml": CONFIG}, "2026-10-02T10:00:00+00:00")
    second = commit(code, SECOND, "2026-10-20T10:00:00+00:00")
    return types.SimpleNamespace(code=code, config=config, first=first, second=second)


def report(changes, capsys, *argv: str) -> tuple[int, str]:
    code = changes.main(list(argv))
    return code, capsys.readouterr().out


def test_estimates_the_baseline_from_the_configs_last_commit(changes, capsys, project):
    code, out = report(changes, capsys, str(project.config))
    assert code == 0
    assert "no check/reviewed.json" in out
    assert "since    %s" % project.first[:7] in out and "estimated" in out
    assert "until    %s" % project.second[:7] in out


def test_lists_what_the_config_depends_on(changes, capsys, project):
    _code, out = report(changes, capsys, str(project.config))
    for line in (
        "scripts/prepare-db.sh is gone (setup[1])",
        "config/database.yml.sample changed, +1 -0 (setup[1])",
        ".nvmrc: 20 -> 22",
        'package.json: packageManager: "pnpm@9.1.0" -> "pnpm@10.2.0"',
        "worker/go.mod added: a module or workspace is new",
        "worker/go.mod: go 1.23",
        ".npmrc (added)",
        "service added: search (getmeili/meilisearch:v1.8; the config has no service of that name)",
        "db: postgres:16 -> postgres:17; the config's db is postgres:16",
        ".env.example: added MEILI_MASTER_KEY, SENTRY_DSN",
        ".env.example: removed OLD_KEY",
        "no longer node-version: 20; now node-version: 22",
        "package.json: scripts added: e2e",
        "package.json: script test: vitest run -> vitest run --pool=forks",
        "AGENTS.md (added)",
        "worker/",
        "package.json: added: sharp",
        "1 lockfile changed without a format change",
    ):
        assert line in out, line
    # Application code is not a finding, and an unchanged file the notes name is not one either.
    assert "src/a.js" not in out and "db/seeds.rb" not in out


def test_never_prints_a_value(changes, capsys, project):
    _code, out = report(changes, capsys, str(project.config))
    assert "not-to-be-printed" not in out and "postgres://localhost" not in out


def test_record_then_nothing_to_compare(changes, capsys, project):
    code, out = report(changes, capsys, "--record", str(project.config))
    assert code == 0 and project.second[:7] in out
    record = json.loads((project.config / "check" / "reviewed.json").read_text())
    assert record["repositories"] == {"app": {"branch": "main", "commit": project.second}}
    assert record["skill"] == json.loads((SKILLS.parent / ".claude-plugin" / "plugin.json").read_text())["version"]

    code, out = report(changes, capsys, "--exit-code", str(project.config))
    assert code == 0
    assert "recorded in check/reviewed.json" in out and "no commits since" in out

    commit(project.code, {"Makefile": "test:\n\tpnpm test\n"}, "2026-11-01T10:00:00+00:00")
    code, out = report(changes, capsys, "--exit-code", str(project.config))
    assert code == 1
    assert "Makefile (added)" in out and ".nvmrc" not in out


def test_record_stops_at_what_the_checkout_and_the_branch_share(changes, capsys, project):
    git(project.code, "checkout", "-q", "-b", "feature")
    commit(project.code, {"src/b.js": "b\n"}, "2026-11-02T10:00:00+00:00")
    report(changes, capsys, "--record", str(project.config))
    record = json.loads((project.config / "check" / "reviewed.json").read_text())
    assert record["repositories"]["app"]["commit"] == project.second


def test_since_overrides_and_a_lost_record_falls_back(changes, capsys, project):
    write(project.config, {"check/reviewed.json": json.dumps(
        {"repositories": {"app": {"branch": "main", "commit": "0" * 40}}, "skill": "0.6.0"})})
    _code, out = report(changes, capsys, str(project.config))
    assert "the recorded commit 000000000000 is not in this checkout" in out and "estimated" in out

    _code, out = report(changes, capsys, "--since", "app=%s" % project.second, str(project.config))
    assert "given with --since" in out and "no commits since" in out

    _code, out = report(changes, capsys, "--since", "app=2026-10-05", str(project.config))
    assert "since    %s (given with --since)" % git(project.code, "log", "-1", "--format=%h %cs", project.first) in out


def test_compares_up_to_the_remote_branch_and_names_a_checkout_that_is_elsewhere(changes, capsys, project):
    git(project.code, "update-ref", "refs/remotes/origin/main", project.second)
    git(project.code, "checkout", "-q", "--detach", project.first)
    _code, out = report(changes, capsys, str(project.config))
    assert "until    %s" % project.second[:7] in out and "(origin/main)" in out
    assert "not at origin/main: the check clones the checkout as it is" in out


def test_a_repository_without_a_checkout_is_named(changes, capsys, project, tmp_path):
    elsewhere = tmp_path / "elsewhere" / "exradius-app"
    elsewhere.parent.mkdir()
    project.config.rename(elsewhere)
    code, out = report(changes, capsys, "--exit-code", str(elsewhere))
    assert code == 1 and "no local checkout: pass --repo app=PATH" in out

    _code, out = report(changes, capsys, "--repo", "app=%s" % project.code, str(elsewhere))
    assert ".nvmrc: 20 -> 22" in out


def test_a_config_inside_the_code_repository(changes, capsys, tmp_path):
    code = tmp_path / "app"
    code.mkdir()
    git(code, "init", "-q", "-b", "main")
    git(code, "remote", "add", "origin", "https://github.com/acme/app")
    commit(code, {".nvmrc": "20\n"}, "2026-10-01T10:00:00+00:00")
    first = git(code, "rev-parse", "HEAD")
    config_commit = commit(code, {".exradius/exradius.yaml": CONFIG, ".exradius/docker/workstation.Dockerfile":
                                  "FROM node:20\n"}, "2026-10-02T10:00:00+00:00")
    _code, out = report(changes, capsys, str(code))
    assert "no commits since" in out and "its only commit" in out

    commit(code, {".nvmrc": "22\n"}, "2026-10-20T10:00:00+00:00")
    _code, out = report(changes, capsys, str(code))
    assert "since    %s" % config_commit[:7] in out and "the config's own last commit" in out
    assert ".nvmrc: 20 -> 22" in out and "1 file changed" in out

    # The config's own files are not findings about the code.
    _code, out = report(changes, capsys, "--since", "app=%s" % first, str(code))
    assert "1 file changed" in out and "workstation.Dockerfile" not in out


def test_the_wrappers_run(project):
    done = subprocess.run(["bash", str(SCRIPT.parent / "changes.sh"), str(project.config)],
                          capture_output=True, text=True)
    assert done.returncode == 0 and ".nvmrc: 20 -> 22" in done.stdout, done.stderr
    done = subprocess.run(["bash", str(SKILLS / "exradius-project" / "scripts" / "record.sh"), str(project.config)],
                          capture_output=True, text=True)
    assert done.returncode == 0 and (project.config / "check" / "reviewed.json").is_file(), done.stderr
    done = subprocess.run(["bash", str(SCRIPT.parent / "check.sh"), "--validate", str(project.config)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_the_script_runs_on_the_oldest_python_it_names():
    assert sys.version_info >= (3, 8)
    source = SCRIPT.read_text()
    for newer in ("removeprefix(", "removesuffix(", "match ", " | None"):
        assert newer not in source, newer
