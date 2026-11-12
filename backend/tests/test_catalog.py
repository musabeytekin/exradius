"""The connection catalog's pins (docs/agent-routing.md §3.1, docs/config-repo.md §5): every
``npx`` and ``uvx`` entry names an exact version, what counts as one for each launcher, and
the warning a custom connection gets without one, from the backend and from the setup skill's
check, which carries its own copy of the rule.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

from exradius import catalog
from exradius.schemas.project_config import parse_config

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "skill" / "skills" / "exradius-project" / "scripts" / "exradius_check.py"
MINIMAL = "version: 1\nproject: {slug: p}\nagents:\n  a: {}\n"

PINS = {
    "gitlab": "@zereight/mcp-gitlab@2.3.14",
    "slack": "@zencoderai/slack-mcp-server@0.0.1",
    "sentry": "@sentry/mcp-server@0.42.0",
    "jira": "mcp-atlassian@0.23.1",
    "fetch": "mcp-server-fetch@2026.10.28",
    "playwright": "@playwright/mcp@0.0.83",
    "filesystem": "@modelcontextprotocol/server-filesystem@1.0.0",
}
"""The package each launcher entry runs. A new version is a change here too, on purpose: it
is code that runs with a connection's secrets."""


@pytest.fixture(scope="module")
def check() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("exradius_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["exradius_check"] = module
    spec.loader.exec_module(module)
    return module


# -- the entries -------------------------------------------------------------------------------


def test_every_launcher_entry_runs_an_exact_version():
    launched = [item for item in catalog.CATALOG if item.command is not None]
    assert launched
    for item in launched:
        assert item.command in catalog.LAUNCHERS, item.key
        assert item.requires == item.command, item.key
        args = catalog.expand(item, {k.key: "value" for k in item.config})["args"]
        assert catalog.unpinned_packages(item.command, args) == [], item.key


def test_the_pins_are_the_reviewed_ones():
    packages = {}
    for item in catalog.CATALOG:
        if item.command == "npx":
            assert item.args[0] == "-y", item.key
            packages[item.key] = item.args[1]
        elif item.command == "uvx":
            packages[item.key] = item.args[0]
    assert packages == PINS


def test_hosted_entries_have_nothing_to_pin():
    hosted = [item for item in catalog.CATALOG if item.command is None]
    assert {item.key for item in hosted} == {"github", "linear"}
    for item in hosted:
        assert item.transport == "http" and item.url and not item.args and item.requires is None


def test_a_launcher_entry_may_reach_its_registry():
    for item in catalog.CATALOG:
        if item.command == "npx":
            assert set(catalog.NPM) <= set(item.hosts), item.key
        elif item.command == "uvx":
            assert set(catalog.PYPI) <= set(item.hosts), item.key


def test_the_design_notes_table_shows_the_catalogs_commands():
    text = (ROOT / "docs" / "config-repo.md").read_text()
    rows = dict(re.findall(r"^\| `([a-z]+)`\s*\| `((?:npx|uvx) [^`]+)`\s*\|$", text, flags=re.MULTILINE))
    commands = {
        item.key: " ".join([item.command, *item.args, *("<path>" for k in item.config if k.target == "arg")])
        for item in catalog.CATALOG if item.command is not None
    }
    assert rows == commands


def test_the_skills_playwright_recipe_installs_the_pinned_servers_browser():
    # The browser comes from the server's own package in a project's setup, so a recipe at
    # another version downloads a build the connection does not look for.
    text = "".join(path.read_text() for path in (SCRIPT.parents[1] / "references").glob("*.md"))
    text += (ROOT / "docs" / "config-repo.md").read_text()
    versions = set(re.findall(r"@playwright/mcp@[^\s`]+", text))
    assert versions == {PINS["playwright"]}


# -- what an exact version is ------------------------------------------------------------------

NPX = [
    (["-y", "pkg@1.2.3"], []),
    (["-y", "@scope/pkg@1.2.3", "--flag", "value"], []),
    (["-y", "pkg@1.2.3-beta.1"], []),
    (["--package=pkg@1.2.3", "command"], []),
    (["-p", "pkg@1.2.3", "-p", "other@2.0.0", "command"], []),
    (["./local-server"], []),
    (["-y", "github:owner/repo"], []),
    (["-y", "https://example.com/server.tgz"], []),
    (["-c", "echo hello"], []),
    ([], []),
    (["-y", "pkg"], ["pkg"]),
    (["pkg"], ["pkg"]),
    (["-y", "pkg@latest"], ["pkg@latest"]),
    (["-y", "@scope/pkg"], ["@scope/pkg"]),
    (["-y", "@scope/pkg@next"], ["@scope/pkg@next"]),
    (["-y", "pkg@^1.2.3"], ["pkg@^1.2.3"]),
    (["-y", "pkg@1"], ["pkg@1"]),
    (["-y", "pkg@1.2"], ["pkg@1.2"]),
    (["-y", "pkg@1.x"], ["pkg@1.x"]),
    (["-p", "first", "-p", "second@1.0.0", "command"], ["first"]),
    (["--package", "first", "command@1.0.0"], ["first"]),
    (["-y", "pkg", "other@1.0.0"], ["pkg"]),
]
UVX = [
    (["tool@1.2.3"], []),
    (["tool@1.2.3", "--flag"], []),
    (["--from", "package==1.2.3", "tool"], []),
    (["--from=package==1.2.3", "tool"], []),
    (["--from", "package[extra]==1.2.3", "tool"], []),
    (["--from", "git+https://github.com/owner/repo@0123abc", "tool"], []),
    (["--from", "./checkout", "tool"], []),
    (["--python", "3.12", "tool@1.2.3"], []),
    (["--with", "dependency==1.0", "tool@1.0.0"], []),
    ([], []),
    (["tool"], ["tool"]),
    (["tool", "--flag"], ["tool"]),
    (["tool@latest"], ["tool@latest"]),
    (["--from", "package", "tool"], ["package"]),
    (["--from", "package>=1.0", "tool"], ["package>=1.0"]),
    (["--from", "package~=1.2", "tool@1.0.0"], ["package~=1.2"]),
    (["--python", "3.12", "tool"], ["tool"]),
    (["--with", "dependency", "tool@1.0.0"], ["dependency"]),
    (["--with", "first==1.0,second", "tool@1.0.0"], ["second"]),
    (["-w", "dependency", "tool"], ["tool", "dependency"]),
]


@pytest.mark.parametrize(("args", "unpinned"), NPX, ids=[" ".join(args) or "nothing" for args, _ in NPX])
def test_npx_packages_without_an_exact_version(check, args, unpinned):
    assert catalog.unpinned_packages("npx", args) == unpinned
    assert check.unpinned_packages("npx", args) == unpinned


@pytest.mark.parametrize(("args", "unpinned"), UVX, ids=[" ".join(args) or "nothing" for args, _ in UVX])
def test_uvx_packages_without_an_exact_version(check, args, unpinned):
    assert catalog.unpinned_packages("uvx", args) == unpinned
    assert check.unpinned_packages("uvx", args) == unpinned


@pytest.mark.parametrize("command", ["node", "python", "docker", "/usr/local/bin/server", None])
def test_other_commands_have_no_registry_package(check, command):
    assert catalog.unpinned_packages(command, ["server", "tool"]) == []
    assert check.unpinned_packages(command, ["server", "tool"]) == []


def test_arguments_that_are_not_strings_are_read_as_written(check):
    assert catalog.unpinned_packages("npx", ["-y", "pkg", 8080]) == ["pkg"]
    assert check.unpinned_packages("npx", ["-y", "pkg", "8080"]) == ["pkg"]


# -- the warning a custom connection gets ------------------------------------------------------

UNPINNED = MINIMAL + """\
connections:
  docs: {command: npx, args: [-y, docs-mcp]}
  search: {command: uvx, args: [--from, search-tools, search]}
  both: {command: uvx, args: [--with, helper, lookup]}
"""
PINNED = MINIMAL + """\
connections:
  docs: {command: npx, args: [-y, docs-mcp@1.4.0]}
  search: {command: uvx, args: [--from, search-tools==2.0.1, search]}
  local: {command: node, args: [server.js]}
  web: {url: 'https://mcp.example.com/mcp'}
  files: {catalog: filesystem}
  tickets: {catalog: gitlab, config: {token: t}}
"""
WARNINGS = [
    "connections.docs: runs docs-mcp without an exact version, so each session gets its latest release",
    "connections.search: runs search-tools without an exact version, so each session gets its latest release",
    "connections.both: runs lookup, helper without an exact version, so each session gets its latest release",
]


def version_warnings(warnings: list[str]) -> list[str]:
    return [w for w in warnings if "without an exact version" in w]


def test_a_custom_connection_without_an_exact_version_is_a_warning():
    result = parse_config(UNPINNED)
    assert result.errors == []
    assert version_warnings(result.warnings) == WARNINGS


def test_a_pinned_or_catalog_connection_is_not_warned_about():
    result = parse_config(PINNED)
    assert result.errors == []
    assert version_warnings(result.warnings) == []


def test_the_skills_check_gives_the_same_warning(check):
    _config, errors, warnings, _required = check.validate(check.load_yaml(UNPINNED))
    assert errors == []
    assert version_warnings(warnings) == WARNINGS
    _config, errors, warnings, _required = check.validate(check.load_yaml(PINNED))
    assert errors == []
    assert version_warnings(warnings) == []
