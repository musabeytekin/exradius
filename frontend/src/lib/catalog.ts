// Common MCP servers (onboarding §3.3). Static: picking one fills the form.
// Values marked `secret` are credentials: the form asks for them by `label` and stores
// them as the global secret `secret`, which the value references as `${secret:NAME}`.
// The name is internal; the user never types it.

import type { Transport } from "./types";

export interface CatalogField {
  key: string;
  /** Internal name of the global secret that holds the credential, e.g. GITHUB_TOKEN. */
  secret?: string;
  /** What the user is asked for, e.g. "GitHub token". Set on every field with a secret. */
  label?: string;
  /** One line: what the credential needs (scopes, permissions). */
  help?: string;
  /** Where to create one. */
  createUrl?: string;
  /** Literal value to prefill (the user edits it). */
  value?: string;
  /** Template wrapping the reference, e.g. "Bearer {}". */
  format?: string;
}

export interface CatalogEntry {
  key: string;
  name: string;
  slug: string;
  description: string;
  transport: Transport;
  command?: string;
  args?: string[];
  url?: string;
  env?: CatalogField[];
  headers?: CatalogField[];
  docs?: string;
  /** A launcher the default project image lacks; the server needs a project image that has it. */
  requires?: string;
}

/** A credential's label, help and link, shared by the catalog and the repository form. */
export interface Credential {
  label: string;
  help?: string;
  createUrl?: string;
}

const GITHUB_TOKEN: Credential = {
  label: "GitHub token",
  help: "A fine-grained token, read-only where possible. Repositories need Contents; GitHub connections also Issues and Pull requests.",
  createUrl: "https://github.com/settings/personal-access-tokens",
};

const GITLAB_TOKEN: Credential = {
  label: "GitLab personal access token",
  help: "A token with read_api and read_repository; api and write_repository only if the agent should write.",
  createUrl: "https://gitlab.com/-/user_settings/personal_access_tokens",
};

export const CATALOG: CatalogEntry[] = [
  {
    key: "github", name: "GitHub", slug: "github", transport: "http",
    description: "Issues, pull requests, code search. GitHub's hosted MCP server.",
    url: "https://api.githubcopilot.com/mcp/",
    headers: [{ key: "Authorization", secret: "GITHUB_TOKEN", format: "Bearer {}", ...GITHUB_TOKEN }],
    docs: "https://github.com/github/github-mcp-server",
  },
  {
    key: "gitlab", name: "GitLab", slug: "gitlab", transport: "stdio",
    description: "Projects, merge requests and issues on GitLab.com or self-managed.",
    command: "npx", args: ["-y", "@modelcontextprotocol/server-gitlab"],
    env: [
      { key: "GITLAB_PERSONAL_ACCESS_TOKEN", secret: "GITLAB_TOKEN", ...GITLAB_TOKEN },
      { key: "GITLAB_API_URL", value: "https://gitlab.com/api/v4" },
    ],
  },
  {
    key: "filesystem", name: "Filesystem", slug: "filesystem", transport: "stdio",
    description: "File operations limited to the listed directories. /work is the project directory.",
    command: "npx", args: ["-y", "@modelcontextprotocol/server-filesystem", "/work"],
  },
  {
    key: "postgres", name: "Postgres", slug: "postgres", transport: "stdio",
    description: "Schema inspection and queries. Restricted mode runs read-only transactions.",
    command: "uvx", args: ["postgres-mcp", "--access-mode=restricted"], requires: "uvx",
    env: [{
      key: "DATABASE_URI", secret: "DATABASE_URI", label: "Database connection string",
      help: "postgresql://user:password@host:5432/db, for a read-only database user where possible.",
    }],
  },
  {
    key: "slack", name: "Slack", slug: "slack", transport: "stdio",
    description: "Read channels and post messages with a bot token.",
    command: "npx", args: ["-y", "@modelcontextprotocol/server-slack"],
    env: [
      {
        key: "SLACK_BOT_TOKEN", secret: "SLACK_BOT_TOKEN", label: "Slack bot token",
        help: "The xoxb- token of a Slack app with channels:history, channels:read and chat:write.",
        createUrl: "https://api.slack.com/apps",
      },
      { key: "SLACK_TEAM_ID", value: "T0000000000" },
    ],
  },
  {
    key: "sentry", name: "Sentry", slug: "sentry", transport: "stdio",
    description: "Issues, events and stack traces from Sentry.",
    command: "npx", args: ["-y", "@sentry/mcp-server"],
    env: [{
      key: "SENTRY_ACCESS_TOKEN", secret: "SENTRY_ACCESS_TOKEN", label: "Sentry access token",
      help: "A user auth token with org:read, project:read and event:read.",
      createUrl: "https://sentry.io/settings/account/api/auth-tokens/",
    }],
  },
  {
    key: "linear", name: "Linear", slug: "linear", transport: "http",
    description: "Issues, projects and comments. Linear's hosted MCP server.",
    url: "https://mcp.linear.app/mcp",
    headers: [{
      key: "Authorization", secret: "LINEAR_API_KEY", format: "Bearer {}", label: "Linear API key",
      help: "A personal API key. Read-only access where possible.",
      createUrl: "https://linear.app/settings/account/security",
    }],
  },
  {
    key: "jira", name: "Jira", slug: "jira", transport: "stdio",
    description: "Jira issues and Confluence pages through mcp-atlassian.",
    command: "uvx", args: ["mcp-atlassian"], requires: "uvx",
    env: [
      { key: "JIRA_URL", value: "https://your-company.atlassian.net" },
      { key: "JIRA_USERNAME", value: "bot@your-company.com" },
      {
        key: "JIRA_API_TOKEN", secret: "JIRA_API_TOKEN", label: "Jira API token",
        help: "An Atlassian API token of the account in JIRA_USERNAME.",
        createUrl: "https://id.atlassian.com/manage-profile/security/api-tokens",
      },
    ],
  },
  {
    key: "fetch", name: "Fetch", slug: "fetch", transport: "stdio",
    description: "Fetch a URL and return it as markdown.",
    command: "uvx", args: ["mcp-server-fetch"], requires: "uvx",
  },
  {
    key: "playwright", name: "Playwright", slug: "playwright", transport: "stdio",
    description: "Drive a headless browser: navigate, click, read the page.",
    command: "npx", args: ["-y", "@playwright/mcp@latest", "--headless"],
  },
];

/** Fields of an entry that hold a credential, with where they go. */
export function credentialFields(e: CatalogEntry): (CatalogField & { secret: string; label: string; target: "env" | "headers" })[] {
  return [
    ...(e.env ?? []).map((f) => ({ ...f, target: "env" as const })),
    ...(e.headers ?? []).map((f) => ({ ...f, target: "headers" as const })),
  ]
    .filter((f): f is typeof f & { secret: string } => !!f.secret)
    .map((f) => ({ ...f, label: f.label ?? f.secret }));
}

/** The default secret name for a repository token on a host the catalog does not know. */
export const REPO_TOKEN_SECRET = "GIT_TOKEN";

const CREDENTIALS: Record<string, Credential> = Object.fromEntries(
  CATALOG.flatMap((e) => credentialFields(e)).map((f) => [f.secret, { label: f.label, help: f.help, createUrl: f.createUrl }]),
);

/** Label, help and link for a secret name the catalog (or the repository form) uses; undefined for others. */
export function credentialFor(name: string): Credential | undefined {
  if (CREDENTIALS[name]) return CREDENTIALS[name];
  const m = /^GIT_TOKEN(?:_(\d+))?$/.exec(name);
  if (m) return { label: m[1] ? `Repository token ${m[1]}` : "Repository token" };
  return undefined;
}

/** The human label of a secret name, when the panel knows it. */
export function secretLabel(name: string): string | undefined {
  return credentialFor(name)?.label;
}

/** The catalog credential that authenticates git on a repository URL's host, for github.com and gitlab.com. */
export function repoCredential(url: string): (Credential & { secret: string }) | null {
  let host: string;
  try {
    host = new URL(url.trim()).hostname.toLowerCase();
  } catch {
    return null;
  }
  const secret = host === "github.com" || host.endsWith(".github.com") ? "GITHUB_TOKEN" : host === "gitlab.com" ? "GITLAB_TOKEN" : null;
  return secret ? { secret, ...CREDENTIALS[secret] } : null;
}

export const COMMAND_SUGGESTIONS = ["npx", "uvx", "docker", "node", "python"];

export function fieldValue(f: CatalogField): string {
  if (f.secret) {
    const ref = `\${secret:${f.secret}}`;
    return f.format ? f.format.replace("{}", ref) : ref;
  }
  return f.value ?? "";
}

/** A standard `mcpServers` block, as Claude Code, Cursor and server READMEs write it. */
export interface ImportedServer {
  slug: string;
  name: string;
  transport: Transport;
  command: string | null;
  args: string[];
  url: string | null;
  env: Record<string, string>;
  headers: Record<string, string>;
}

export function parseMcpServers(text: string): { servers: ImportedServer[]; error: string | null } {
  let data: unknown;
  try {
    data = JSON.parse(text);
  } catch (e) {
    return { servers: [], error: `Not valid JSON: ${(e as Error).message}` };
  }
  if (!data || typeof data !== "object") return { servers: [], error: "Expected an object" };
  const obj = data as Record<string, unknown>;
  const block = (obj.mcpServers ?? obj.servers ?? obj) as Record<string, unknown>;
  if (!block || typeof block !== "object" || Array.isArray(block)) return { servers: [], error: "Expected an mcpServers object" };
  const servers: ImportedServer[] = [];
  for (const [name, raw] of Object.entries(block)) {
    if (!raw || typeof raw !== "object") continue;
    const s = raw as Record<string, unknown>;
    const url = typeof s.url === "string" ? s.url : null;
    const type = typeof s.type === "string" ? s.type : url ? "http" : "stdio";
    const transport: Transport = type === "stdio" ? "stdio" : "http";
    servers.push({
      slug: name.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^[-._]+/, "") || "server",
      name,
      transport,
      command: typeof s.command === "string" ? s.command : null,
      args: Array.isArray(s.args) ? s.args.map(String) : [],
      url,
      env: stringMap(s.env),
      headers: stringMap(s.headers),
    });
  }
  if (servers.length === 0) return { servers, error: "No servers found in the block" };
  return { servers, error: null };
}

function stringMap(v: unknown): Record<string, string> {
  if (!v || typeof v !== "object") return {};
  return Object.fromEntries(Object.entries(v as Record<string, unknown>).map(([k, x]) => [k, String(x)]));
}
