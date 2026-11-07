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
"""

from __future__ import annotations

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

    def config_key(self, key: str) -> ConfigKey | None:
        return next((k for k in self.config if k.key == key), None)


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
        args=("-y", "@modelcontextprotocol/server-gitlab"),
        config=(
            ConfigKey("token", "GitLab personal access token", "env", "GITLAB_PERSONAL_ACCESS_TOKEN"),
            ConfigKey("api_url", "GitLab API URL", "env", "GITLAB_API_URL", required=False,
                      default="https://gitlab.com/api/v4"),
        ),
        requires="npx",
    ),
    CatalogEntry(
        key="postgres",
        name="Postgres",
        description="Schema inspection and queries. Restricted mode runs read-only transactions.",
        transport="stdio",
        command="uvx",
        args=("postgres-mcp", "--access-mode=restricted"),
        config=(ConfigKey("url", "Database connection string", "env", "DATABASE_URI"),),
        requires="uvx",
    ),
    CatalogEntry(
        key="slack",
        name="Slack",
        description="Read channels and post messages with a bot token.",
        transport="stdio",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-slack"),
        config=(
            ConfigKey("bot_token", "Slack bot token", "env", "SLACK_BOT_TOKEN"),
            ConfigKey("team_id", "Slack team id", "env", "SLACK_TEAM_ID"),
        ),
        requires="npx",
    ),
    CatalogEntry(
        key="sentry",
        name="Sentry",
        description="Issues, events and stack traces from Sentry.",
        transport="stdio",
        command="npx",
        args=("-y", "@sentry/mcp-server"),
        config=(ConfigKey("token", "Sentry access token", "env", "SENTRY_ACCESS_TOKEN"),),
        requires="npx",
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
        args=("mcp-atlassian",),
        config=(
            ConfigKey("url", "Jira URL", "env", "JIRA_URL"),
            ConfigKey("username", "Jira username", "env", "JIRA_USERNAME"),
            ConfigKey("api_token", "Jira API token", "env", "JIRA_API_TOKEN"),
        ),
        requires="uvx",
    ),
    CatalogEntry(
        key="fetch",
        name="Fetch",
        description="Fetch a URL and return it as markdown.",
        transport="stdio",
        command="uvx",
        args=("mcp-server-fetch",),
        requires="uvx",
    ),
    CatalogEntry(
        key="playwright",
        name="Playwright",
        description="Drive a headless browser: navigate, click, read the page.",
        transport="stdio",
        command="npx",
        args=("-y", "@playwright/mcp@latest", "--headless"),
        requires="npx",
    ),
    CatalogEntry(
        key="filesystem",
        name="Filesystem",
        description="File operations limited to one directory; /work is the project directory.",
        transport="stdio",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem"),
        config=(ConfigKey("path", "Directory", "arg", required=False, default="/work"),),
        requires="npx",
    ),
)

ENTRIES: dict[str, CatalogEntry] = {entry.key: entry for entry in CATALOG}
LAUNCHERS = ("npx", "uvx")
"""Commands the default workstation image does not have."""


def entry(key: str) -> CatalogEntry | None:
    return ENTRIES.get(key)


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
        "config": [
            {
                "key": k.key,
                "label": k.label,
                "required": k.required,
                "target": k.target,
                "name": k.name or None,
                "format": k.format,
                "default": k.default,
            }
            for k in item.config
        ],
    }
