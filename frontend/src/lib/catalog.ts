// Common MCP servers (onboarding §3.3). Static: picking one fills the form.
// Values marked `secret` become `${secret:NAME}`; the form offers to create the secret.

import type { Transport } from "./types";

export interface CatalogField {
  key: string;
  /** Secret name to reference, e.g. GITHUB_TOKEN. */
  secret?: string;
  /** Literal value to prefill (the user edits it). */
  value?: string;
  /** Template wrapping the reference, e.g. "Bearer {}". */
  format?: string;
  hint?: string;
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

export const CATALOG: CatalogEntry[] = [
  {
    key: "github", name: "GitHub", slug: "github", transport: "http",
    description: "Issues, pull requests, code search. GitHub's hosted MCP server.",
    url: "https://api.githubcopilot.com/mcp/",
    headers: [{ key: "Authorization", secret: "GITHUB_TOKEN", format: "Bearer {}", hint: "A fine-grained token, read-only where possible" }],
    docs: "https://github.com/github/github-mcp-server",
  },
  {
    key: "gitlab", name: "GitLab", slug: "gitlab", transport: "stdio",
    description: "Projects, merge requests and issues on GitLab.com or self-managed.",
    command: "npx", args: ["-y", "@modelcontextprotocol/server-gitlab"],
    env: [
      { key: "GITLAB_PERSONAL_ACCESS_TOKEN", secret: "GITLAB_TOKEN" },
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
    env: [{ key: "DATABASE_URI", secret: "DATABASE_URI", hint: "postgresql://user:password@host:5432/db" }],
  },
  {
    key: "slack", name: "Slack", slug: "slack", transport: "stdio",
    description: "Read channels and post messages with a bot token.",
    command: "npx", args: ["-y", "@modelcontextprotocol/server-slack"],
    env: [
      { key: "SLACK_BOT_TOKEN", secret: "SLACK_BOT_TOKEN" },
      { key: "SLACK_TEAM_ID", value: "T0000000000" },
    ],
  },
  {
    key: "sentry", name: "Sentry", slug: "sentry", transport: "stdio",
    description: "Issues, events and stack traces from Sentry.",
    command: "npx", args: ["-y", "@sentry/mcp-server"],
    env: [{ key: "SENTRY_ACCESS_TOKEN", secret: "SENTRY_ACCESS_TOKEN" }],
  },
  {
    key: "linear", name: "Linear", slug: "linear", transport: "http",
    description: "Issues, projects and comments. Linear's hosted MCP server.",
    url: "https://mcp.linear.app/mcp",
    headers: [{ key: "Authorization", secret: "LINEAR_API_KEY", format: "Bearer {}" }],
  },
  {
    key: "jira", name: "Jira", slug: "jira", transport: "stdio",
    description: "Jira issues and Confluence pages through mcp-atlassian.",
    command: "uvx", args: ["mcp-atlassian"], requires: "uvx",
    env: [
      { key: "JIRA_URL", value: "https://your-company.atlassian.net" },
      { key: "JIRA_USERNAME", value: "bot@your-company.com" },
      { key: "JIRA_API_TOKEN", secret: "JIRA_API_TOKEN" },
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
