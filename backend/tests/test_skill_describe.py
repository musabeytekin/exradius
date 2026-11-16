"""The info skill's description of a config (skill/skills/exradius-info/scripts/
exradius_describe.py): the defaults it fills in, what it leaves to the install, and what it
must never print. The script is standard library only and not a package, so it is loaded by
path.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import types
from pathlib import Path

import pytest

SKILLS = Path(__file__).resolve().parents[2] / "skill" / "skills"
SCRIPT = SKILLS / "exradius-info" / "scripts" / "exradius_describe.py"

CONFIG = """\
version: 1
project:
  slug: shop
  name: Shop
secret_store: shop
resources:
  memory: 6g
repositories:
  - name: shop
    url: https://github.com/acme/shop.git
    credential: "${secret:GITHUB_TOKEN}"
    push: true
  - name: docs
    url: https://github.com/acme/docs.git
    branch: trunk
workstation:
  image: node:22
  setup:
    - cd shop && npm ci
  cache:
    - ~/.npm
services:
  postgres:
    image: postgres:16
    env:
      POSTGRES_PASSWORD: plain-test-value
    ports: [5432]
    ready:
      tcp: 5432
env:
  API_KEY: "${secret:STRIPE_TEST_KEY}"
  DATABASE_URL: postgres://app:plain-test-value@postgres:5432/app
network:
  allow:
    - registry.npmjs.org
connections:
  github:
    catalog: github
    config: {token: "${secret:GITHUB_TOKEN}"}
  github-read:
    catalog: github
    config: {token: "${secret:GITHUB_READ_TOKEN}"}
  docs:
    command: npx
    args: ["-y", "@acme/docs-mcp@1.4.2"]
    env: {DOCS_TOKEN: "${secret:DOCS_TOKEN}"}
  tracker:
    url: https://mcp.example.com/mcp
    headers: {Authorization: "Bearer ${secret:TRACKER_TOKEN}"}
  sentry:
    catalog: sentry
    config: {token: "${secret:SENTRY_TOKEN}"}
notes: |
  Run the tests with `npm test`.
agents:
  developer:
    role: |
      You take tickets that ask for a change to the code: bug fixes and features. You
      keep the change small.
      Before you finish, run the tests.
    model: claude-sonnet-5-5
    provider: main
    identity: {email: bot@acme.com}
    connections: [github, docs]
  reviewer:
    name: Code reviewer
    connections: [github-read, tracker]
"""


@pytest.fixture(scope="module")
def describe() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("exradius_describe", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def config(tmp_path: Path) -> Path:
    (tmp_path / "exradius.yaml").write_text(CONFIG)
    return tmp_path


def run(describe, capsys, *argv: str) -> str:
    assert describe.main(list(argv)) == 0
    return capsys.readouterr().out


def test_agents_with_what_the_file_sets_and_what_it_leaves(describe, capsys, config):
    out = run(describe, capsys, str(config))
    for line in (
        "developer  (name: Developer, the default)",
        "model      claude-sonnet-5-5",
        "provider   main",
        "git author Developer <bot@acme.com>",
        "role       3 lines; opens: You take tickets that ask for a change to the code: bug fixes and features.",
        "reviewer  (name: Code reviewer)",
        "model      not set in the file: the runtime's default model on that provider",
        "provider   not set in the file: the install's default provider",
        "git author Code reviewer <reviewer@shop.exradius.local> (the default address)",
        "role       none",
        "a task that names no agent: Exradius picks one",
    ):
        assert line in out, line
    assert "ERRORS" not in out


def test_connections_keys_hosts_and_who_uses_them(describe, capsys, config):
    out = run(describe, capsys, str(config))
    for line in (
        "+ github: catalog github; http, https://api.githubcopilot.com/mcp/; keys GITHUB_TOKEN; reached through",
        "+ docs: custom stdio, npx -y @acme/docs-mcp@1.4.2; keys DOCS_TOKEN",
        "github-read: catalog github;",
        "used by reviewer",
        "sentry: catalog sentry; stdio, started with npx in the workstation image; keys SENTRY_TOKEN",
        "no agent lists it, so no run starts it",
        "shop: https://github.com/acme/shop.git, branch the remote's default, agents may push, credential GITHUB_TOKEN",
        "docs: https://github.com/acme/docs.git, branch trunk, read-only (agents attach a patch), no credential",
        "postgres: postgres:16, ports 5432, ready by tcp 5432",
        "memory   6g",
        "plain    DATABASE_URL",
        "secret   API_KEY (STRIPE_TEST_KEY)",
        "GITHUB_TOKEN: repositories.shop.credential, connections.github.config.token",
        "TRACKER_TOKEN: connections.tracker.headers.Authorization",
        "store    shop",
    ):
        assert line in out, line
    allowed = next(line for line in out.splitlines() if line.strip().startswith("allowed"))
    assert "registry.npmjs.org" in allowed
    # An unused connection adds no host, and a proxied one is not on the list.
    assert "sentry.io" not in allowed and "mcp.example.com" not in allowed


def test_never_prints_a_value(describe, capsys, config):
    out = run(describe, capsys, str(config))
    assert "plain-test-value" not in out and "${secret:" not in out and "Bearer" not in out


def test_one_agent_with_its_role_in_full(describe, capsys, config):
    out = run(describe, capsys, "--agent", "developer", str(config))
    assert "Agent of project shop:" in out and "Before you finish, run the tests." in out
    assert "reviewer" not in out and "Repositories" not in out
    with pytest.raises(SystemExit, match="no agent nobody"):
        describe.main(["--agent", "nobody", str(config)])


def test_a_single_agent_and_a_file_with_errors(describe, capsys, tmp_path):
    (tmp_path / "exradius.yaml").write_text(
        "version: 1\nproject:\n  slug: one\nagents:\n  developer: {}\nworkstation:\n  imagee: node:22\n")
    out = run(describe, capsys, str(tmp_path))
    assert "ERRORS   1" in out and "imagee" in out
    assert "a task that names no agent: goes to the only agent" in out
    assert "Exradius's default image" in out and "store    none named" in out


def test_the_record_and_the_wrappers(config):
    (config / "check").mkdir()
    (config / "check" / "reviewed.json").write_text(json.dumps(
        {"repositories": {"shop": {"branch": "main", "commit": "abcdef1234567890"}}, "skill": "0.8.0"}))
    for skill in ("exradius-info", "exradius-agent"):
        done = subprocess.run(["bash", str(SKILLS / skill / "scripts" / "describe.sh"), str(config)],
                              capture_output=True, text=True)
        assert done.returncode == 0, done.stderr
        assert "Checked against the code: shop at abcdef12 (skill 0.8.0)" in done.stdout
    done = subprocess.run(["bash", str(SKILLS / "exradius-agent" / "scripts" / "check.sh"), "--validate", str(config)],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_every_skill_has_what_its_frontmatter_and_text_name():
    plugin = json.loads((SKILLS.parent / ".claude-plugin" / "plugin.json").read_text())
    assert plugin["version"]
    for skill in sorted(p for p in SKILLS.iterdir() if p.is_dir()):
        text = (skill / "SKILL.md").read_text()
        assert text.startswith("---\nname: %s\n" % skill.name), skill.name
        for word in text.replace("`", " ").replace(")", " ").replace(",", " ").split():
            if word.startswith("${CLAUDE_SKILL_DIR}/") and not word.endswith(("/", "*")):
                path = skill / word[len("${CLAUDE_SKILL_DIR}/"):]
                assert path.exists(), "%s names %s" % (skill.name, word)
