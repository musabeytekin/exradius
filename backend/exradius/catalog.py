"""The connection catalog (docs/config-repo.md §5).

A catalog connection in ``exradius.yaml`` names an entry and gives its config keys::

    connections:
      github:
        catalog: github
        config: {token: ${secret:GITHUB_TOKEN}}

Each entry knows its transport, its command and arguments or URL, and where each config key
goes: an environment variable, a header (with a format such as ``Bearer {}``) or an
argument. ``expand`` turns a catalog connection into the ``mcpServers``-style definition a
custom connection writes by hand; secret references stay as written until the worker
resolves them (``exradius.resolve``).

``hosts`` are what the connection reaches besides its own URL: the package registry its
launcher downloads it from and the service's API. The agent phase may reach them
(docs/kubernetes.md §11, ``exradius.network``), as it may the host of a config key marked
``host`` (GitLab's API URL, the Jira URL).

Every ``npx`` and ``uvx`` package is pinned to an exact version (``npx -y <package>@<version>``,
``uvx <command>@<version>``), so new code runs with a connection's secrets only after a
commit here (docs/agent-routing.md §3.1). The launchers resolve the package's own
dependencies afresh within its declared ranges; only the package itself is fixed. To bump
one, read the version from its registry (``npm view <package> version``,
``https://pypi.org/pypi/<package>/json``), change it in ``args`` and in the table of
docs/config-repo.md §5, and run Verify on a project that uses the entry. The new version
changes the connection's definition hash, so routing stops using that connection's stored
tools until a Verify lists them again. The hosted entries (``github``, ``linear``) have no version to pin: their tools can change
without a commit.

A pin is to a release at least two weeks old whose ``engines`` (npm) the oldest supported
Node, 22, satisfies: a release that was published yesterday has been read by nobody. A
package its registry marks deprecated is replaced or its entry removed.

There is no Postgres entry. A server here must be able to promise read-only access from its
command line or environment, and no maintained one does: ``postgres-mcp`` 0.3.0 cannot be
installed any more (it needs a ``pglast`` release that is gone from PyPI), the reference
server is archived with a known way around its read-only transaction, and DBHub reads the
setting only from a file. A project that wants one writes a custom connection, with a
database role that cannot write (docs/config-repo.md §5).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

Target = Literal["env", "header", "arg"]


@dataclass(frozen=True)
class ConfigKey:
    key: str
    label: str
    target: Target
    name: str = ""
    """The environment variable or header it sets; unused for ``arg``."""
    required: bool = True
    format: str = "{}"
    """How the value is written into the target, ``{}`` being the value."""
    default: str | None = None
    """Used when an optional key is not given."""
    host: bool = False
    """The value is a URL whose host the connection reaches."""


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    name: str
    description: str
    transport: Literal["stdio", "http"]
    command: str | None = None
    args: tuple[str, ...] = ()
    url: str | None = None
    config: tuple[ConfigKey, ...] = ()
    requires: str | None = None
    """A launcher the default workstation image lacks (``npx``, ``uvx``)."""
    docs: str | None = None
    hosts: tuple[str, ...] = ()
    """Hosts the connection reaches besides its URL (see the module's docstring)."""

    def config_key(self, key: str) -> ConfigKey | None:
        return next((k for k in self.config if k.key == key), None)


NPM = ("registry.npmjs.org",)
"""Where ``npx -y`` downloads a server when the session starts."""
PYPI = ("pypi.org", "files.pythonhosted.org")
"""Where ``uvx`` downloads one."""

CATALOG: tuple[CatalogEntry, ...] = (
    CatalogEntry(
        key="github",
        name="GitHub",
        description="Issues, pull requests, code search. GitHub's hosted MCP server.",
        transport="http",
        url="https://api.githubcopilot.com/mcp/",
        config=(ConfigKey("token", "GitHub token", "header", "Authorization", format="Bearer {}"),),
        docs="https://github.com/github/github-mcp-server",
    ),
    CatalogEntry(
        key="gitlab",
        name="GitLab",
        description="Projects, merge requests and issues on GitLab.com or self-managed.",
        transport="stdio",
        command="npx",
        # The reference server (@modelcontextprotocol/server-gitlab) is deprecated on npm.
        # This is its maintained fork: the same two variables, more tools.
        args=("-y", "@zereight/mcp-gitlab@2.3.14"),
        config=(
            ConfigKey("token", "GitLab personal access token", "env", "GITLAB_PERSONAL_ACCESS_TOKEN"),
            ConfigKey("api_url", "GitLab API URL", "env", "GITLAB_API_URL", required=False,
                      default="https://gitlab.com/api/v4", host=True),
        ),
        requires="npx",
        docs="https://github.com/zereight/gitlab-mcp",
        hosts=NPM,
    ),
    CatalogEntry(
        key="slack",
        name="Slack",
        description="Read channels and post messages with a bot token.",
        transport="stdio",
        command="npx",
        # The reference server (@modelcontextprotocol/server-slack) is deprecated on npm;
        # Zencoder took it over with the same variables and tools. It has one release.
        args=("-y", "@zencoderai/slack-mcp-server@0.0.1"),
        config=(
            ConfigKey("bot_token", "Slack bot token", "env", "SLACK_BOT_TOKEN"),
            ConfigKey("team_id", "Slack team id", "env", "SLACK_TEAM_ID"),
        ),
        requires="npx",
        docs="https://github.com/zencoderai/slack-mcp-server",
        hosts=(*NPM, "slack.com"),
    ),
    CatalogEntry(
        key="sentry",
        name="Sentry",
        description="Issues, events and stack traces from Sentry.",
        transport="stdio",
        command="npx",
        args=("-y", "@sentry/mcp-server@0.49.0"),
        config=(ConfigKey("token", "Sentry access token", "env", "SENTRY_ACCESS_TOKEN"),),
        requires="npx",
        hosts=(*NPM, "sentry.io", "*.sentry.io"),
    ),
    CatalogEntry(
        key="linear",
        name="Linear",
        description="Issues, projects and comments. Linear's hosted MCP server.",
        transport="http",
        url="https://mcp.linear.app/mcp",
        config=(ConfigKey("api_key", "Linear API key", "header", "Authorization", format="Bearer {}"),),
    ),
    CatalogEntry(
        key="jira",
        name="Jira",
        description="Jira issues and Confluence pages through mcp-atlassian.",
        transport="stdio",
        command="uvx",
        args=("mcp-atlassian@0.24.1",),
        config=(
            ConfigKey("url", "Jira URL", "env", "JIRA_URL", host=True),
            ConfigKey("username", "Jira username", "env", "JIRA_USERNAME"),
            ConfigKey("api_token", "Jira API token", "env", "JIRA_API_TOKEN"),
        ),
        requires="uvx",
        hosts=PYPI,
    ),
    CatalogEntry(
        key="fetch",
        name="Fetch",
        description="Fetch a URL and return it as markdown.",
        transport="stdio",
        command="uvx",
        args=("mcp-server-fetch@2026.11.11",),
        requires="uvx",
        hosts=PYPI,
    ),
    CatalogEntry(
        key="playwright",
        name="Playwright",
        description="Drive a headless Chromium: navigate, click, read the page, take screenshots.",
        transport="stdio",
        command="npx",
        # Without --browser the server looks for Google Chrome in /opt/google/chrome, which
        # a workstation does not have and Linux on Arm cannot. Chromium is the browser its
        # own `install-browser chromium` downloads, in a setup command: the download is
        # redirected to storage.googleapis.com, where anyone's files live, so the agent
        # phase does not get it (docs/config-repo.md §5).
        args=("-y", "@playwright/mcp@0.0.85", "--headless", "--browser", "chromium"),
        requires="npx",
        hosts=NPM,
    ),
    CatalogEntry(
        key="filesystem",
        name="Filesystem",
        description="File operations limited to one directory; /work is the project directory.",
        transport="stdio",
        command="npx",
        # 2.0.0 and later declare Node 24; 1.0.0 is the newest release for Node 22.
        args=("-y", "@modelcontextprotocol/server-filesystem@1.0.0"),
        config=(ConfigKey("path", "Directory", "arg", required=False, default="/work"),),
        requires="npx",
        hosts=NPM,
    ),
)

ENTRIES: dict[str, CatalogEntry] = {entry.key: entry for entry in CATALOG}
LAUNCHERS = ("npx", "uvx")
"""Commands the default workstation image does not have."""


def entry(key: str) -> CatalogEntry | None:
    return ENTRIES.get(key)


# -- pinned packages --------------------------------------------------------------------------

NPX_VALUE_FLAGS = frozenset({"-p", "--package", "-c", "--call", "-w", "--workspace"})
"""``npx`` options that take the next argument as their value."""
UVX_VALUE_FLAGS = frozenset({
    "--from", "-w", "--with", "--with-editable", "--with-requirements", "-p", "--python",
    "-c", "--constraints", "--overrides", "-b", "--build-constraints", "--index",
    "--default-index", "-i", "--index-url", "--extra-index-url", "-f", "--find-links",
    "--index-strategy", "--keyring-provider", "--resolution", "--prerelease", "--exclude-newer",
    "--exclude-newer-package", "-P", "--upgrade-package", "--reinstall-package",
    "--refresh-package", "--no-build-package", "--no-binary-package", "-C", "--config-setting",
    "--link-mode", "--python-platform", "--torch-backend", "--env-file", "--cache-dir",
    "--directory", "--project", "--config-file", "--color", "--allow-insecure-host",
})
"""``uvx`` options that take the next argument as their value."""
NPM_EXACT = re.compile(r"^[=v]?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
PYPI_EXACT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[^\]]*\])?\s*===?\s*[A-Za-z0-9._+!-]+$")


def _options(args: list[str], value_flags: frozenset[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """``(flag, value)`` pairs before the first positional argument, and the arguments from
    it on (both launchers stop reading options there)."""
    options: list[tuple[str, str]] = []
    index = 0
    while index < len(args) and args[index].startswith("-"):
        flag, eq, value = args[index].partition("=")
        if not eq and flag in value_flags and index + 1 < len(args):
            index += 1
            value = args[index]
        options.append((flag, value))
        index += 1
    return options, args[index:]


def _npm_unpinned(spec: str) -> bool:
    if spec.startswith((".", "/", "~")) or ":" in spec or ("/" in spec and not spec.startswith("@")):
        return False  # a path, URL, git or alias spec: not a registry version
    name, _, version = spec[1:].partition("@") if spec.startswith("@") else spec.partition("@")
    return bool(name) and not NPM_EXACT.match(version)


def _pypi_unpinned(requirement: str) -> bool:
    if requirement.startswith((".", "/", "~")) or "://" in requirement or " @ " in requirement:
        return False  # a path or a direct URL: not a registry version
    return not PYPI_EXACT.match(requirement.strip())


def unpinned_packages(command: str | None, args: list[str]) -> list[str]:
    """The registry packages an ``npx`` or ``uvx`` command runs without an exact version,
    as written; the launcher fetches their latest release when the session starts."""
    args = [str(a) for a in args]
    if command == "npx":
        options, rest = _options(args, NPX_VALUE_FLAGS)
        specs = [value for flag, value in options if flag in ("-p", "--package")]
        if not specs and rest and not any(flag in ("-c", "--call") for flag, _ in options):
            specs = [rest[0]]
        return [spec for spec in specs if _npm_unpinned(spec)]
    if command == "uvx":
        options, rest = _options(args, UVX_VALUE_FLAGS)
        unpinned = []
        source = next((value for flag, value in options if flag == "--from"), None)
        if source is not None:
            if _pypi_unpinned(source):
                unpinned.append(source)
        elif rest:
            name, at, version = rest[0].partition("@")
            if not at and _pypi_unpinned(name) or at and version in ("", "latest"):
                unpinned.append(rest[0])
        for flag, value in options:
            if flag in ("-w", "--with"):
                unpinned += [r for r in re.split(r",\s*(?=[A-Za-z0-9])", value) if _pypi_unpinned(r)]
        return unpinned
    return []


def config_errors(item: CatalogEntry, config: dict[str, str]) -> list[tuple[str, str]]:
    """``(config key, message)`` for unknown keys and missing required ones."""
    errors = [(key, f"unknown config key for catalog {item.key} (known: "
               f"{', '.join(k.key for k in item.config) or 'none'})")
              for key in config if item.config_key(key) is None]
    errors += [(k.key, f"required by catalog {item.key} ({k.label})")
               for k in item.config if k.required and k.key not in config]
    return errors


def expand(item: CatalogEntry, config: dict[str, str]) -> dict[str, Any]:
    """The connection as command/args/env or url/headers; values as written."""
    env: dict[str, str] = {}
    headers: dict[str, str] = {}
    args = list(item.args)
    for key in item.config:
        value = config.get(key.key, key.default)
        if value is None:
            continue
        written = key.format.replace("{}", value)
        if key.target == "env":
            env[key.name] = written
        elif key.target == "header":
            headers[key.name] = written
        else:
            args.append(written)
    return {
        "transport": item.transport,
        "command": item.command,
        "args": args,
        "url": item.url,
        "env": env,
        "headers": headers,
    }


def describe(item: CatalogEntry) -> dict[str, Any]:
    """``GET /api/catalog`` shape of one entry."""
    return {
        "key": item.key,
        "name": item.name,
        "description": item.description,
        "transport": item.transport,
        "command": item.command,
        "args": list(item.args),
        "url": item.url,
        "requires": item.requires,
        "docs": item.docs,
        "hosts": list(item.hosts),
        "config": [
            {
                "key": k.key,
                "label": k.label,
                "required": k.required,
                "target": k.target,
                "name": k.name or None,
                "format": k.format,
                "default": k.default,
                "host": k.host,
            }
            for k in item.config
        ],
    }
