// Common MCP servers (onboarding §3.3). Static: picking one fills the form.
// Values marked `secret` are credentials: the form asks for them by `label` and stores
// them as the global secret `secret`, which the value references as `${secret:NAME}`.
// The name is internal; the user never types it.

import type { ServiceReady, Transport } from "./types";

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

/**
 * Common services (docs/environment-and-services.md §2). Picking one prefills the service
 * form. Env fields with `secret` are passwords: the form asks for them by `label` and stores
 * them as a project secret, which the env value references as `${secret:NAME}`.
 */
export interface ServiceCatalogEntry {
  key: string;
  name: string;
  description: string;
  image: string;
  ports: number[];
  ready: ServiceReady;
  env: CatalogField[];
  /**
   * A project env var that points at the service, for the Environment tab. `{host}` and
   * `{port}` are the service's name and first port; `{KEY}` is the service's env value.
   */
  example?: { key: string; value: string };
}

const PASSWORD_HELP = "Any value works: the service starts empty for every task. A generated one is filled in.";

export const SERVICE_CATALOG: ServiceCatalogEntry[] = [
  {
    key: "postgres", name: "PostgreSQL", description: "Relational database.", image: "postgres:16", ports: [5432],
    ready: { command: ["pg_isready", "-h", "127.0.0.1", "-U", "app", "-d", "app"] },
    env: [
      { key: "POSTGRES_USER", value: "app" },
      { key: "POSTGRES_DB", value: "app" },
      { key: "POSTGRES_PASSWORD", secret: "DB_PASSWORD", label: "Database password", help: PASSWORD_HELP },
    ],
    example: { key: "DATABASE_URL", value: "postgres://{POSTGRES_USER}:{POSTGRES_PASSWORD}@{host}:{port}/{POSTGRES_DB}" },
  },
  {
    key: "mysql", name: "MySQL", description: "Relational database.", image: "mysql:8.4", ports: [3306],
    ready: { command: ["mysqladmin", "ping", "-h", "127.0.0.1", "--silent"] },
    env: [
      { key: "MYSQL_DATABASE", value: "app" },
      { key: "MYSQL_USER", value: "app" },
      { key: "MYSQL_PASSWORD", secret: "MYSQL_PASSWORD", label: "Database password", help: PASSWORD_HELP },
      { key: "MYSQL_RANDOM_ROOT_PASSWORD", value: "yes" },
    ],
    example: { key: "DATABASE_URL", value: "mysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{host}:{port}/{MYSQL_DATABASE}" },
  },
  {
    key: "redis", name: "Redis", description: "In-memory cache and queue.", image: "redis:7", ports: [6379],
    ready: { tcp: 6379 }, env: [],
    example: { key: "REDIS_URL", value: "redis://{host}:{port}" },
  },
  {
    key: "rabbitmq", name: "RabbitMQ", description: "Message broker, with the management UI.", image: "rabbitmq:4-management", ports: [5672, 15672],
    ready: { tcp: 5672 },
    env: [
      { key: "RABBITMQ_DEFAULT_USER", value: "app" },
      { key: "RABBITMQ_DEFAULT_PASS", secret: "RABBITMQ_PASSWORD", label: "Broker password", help: PASSWORD_HELP },
    ],
    example: { key: "AMQP_URL", value: "amqp://{RABBITMQ_DEFAULT_USER}:{RABBITMQ_DEFAULT_PASS}@{host}:{port}/" },
  },
  {
    key: "mongodb", name: "MongoDB", description: "Document database.", image: "mongo:7", ports: [27017],
    ready: { tcp: 27017 },
    env: [
      { key: "MONGO_INITDB_ROOT_USERNAME", value: "app" },
      { key: "MONGO_INITDB_ROOT_PASSWORD", secret: "MONGO_PASSWORD", label: "Database password", help: PASSWORD_HELP },
    ],
    example: { key: "MONGODB_URI", value: "mongodb://{MONGO_INITDB_ROOT_USERNAME}:{MONGO_INITDB_ROOT_PASSWORD}@{host}:{port}/?authSource=admin" },
  },
  {
    key: "elasticsearch", name: "Elasticsearch", description: "Search engine, single node, security off.", image: "elasticsearch:8.15.3", ports: [9200],
    ready: { tcp: 9200 },
    env: [
      // The image maps ES_SETTING_* to settings (dots as _, underscores as __).
      { key: "ES_SETTING_DISCOVERY_TYPE", value: "single-node" },
      { key: "ES_SETTING_XPACK_SECURITY_ENABLED", value: "false" },
      { key: "ES_JAVA_OPTS", value: "-Xms512m -Xmx512m" },
    ],
    example: { key: "ELASTICSEARCH_URL", value: "http://{host}:{port}" },
  },
  {
    key: "mailpit", name: "Mailpit", description: "Catches outgoing email over SMTP; web UI on 8025.", image: "axllent/mailpit:latest", ports: [1025, 8025],
    ready: { tcp: 1025 }, env: [],
    example: { key: "SMTP_URL", value: "smtp://{host}:{port}" },
  },
  {
    key: "minio", name: "MinIO", description: "S3-compatible object storage; console on 9001.", image: "bitnami/minio:latest", ports: [9000, 9001],
    ready: { tcp: 9000 },
    env: [
      { key: "MINIO_ROOT_USER", value: "app" },
      { key: "MINIO_ROOT_PASSWORD", secret: "MINIO_ROOT_PASSWORD", label: "Storage password", help: PASSWORD_HELP },
    ],
    example: { key: "S3_ENDPOINT", value: "http://{host}:{port}" },
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

const CREDENTIALS: Record<string, Credential> = Object.fromEntries([
  ...SERVICE_CATALOG.flatMap((e) => e.env).filter((f) => f.secret).map((f) => [f.secret!, { label: f.label ?? f.secret!, help: f.help }]),
  ...CATALOG.flatMap((e) => credentialFields(e)).map((f) => [f.secret, { label: f.label, help: f.help, createUrl: f.createUrl }]),
]);

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
