import type { Project, ProjectConfig } from "@/lib/types";

// Snapshots shaped as docs/config-repo.md §3 and §11 describe them.

export const BANK_CONFIG: ProjectConfig = {
  version: 1,
  project: { slug: "bank", name: "Bank of Anthos", description: "Sample retail bank, Java and Python services." },
  secret_store: "bank-platform",
  repositories: [
    { name: "bank-of-anthos", url: "https://github.com/acme/bank-of-anthos.git", branch: "main", credential: "$GITHUB_TOKEN", push: true },
    { name: "docs", url: "https://github.com/acme/bank-docs.git", branch: null, credential: null, push: false },
  ],
  workstation: { dockerfile: "docker/workstation.Dockerfile", context: "docker/", setup: ["cp -r /opt/m2 ~/.m2"] },
  services: {
    postgres: {
      image: "postgres:16",
      env: { POSTGRES_USER: "app", POSTGRES_DB: "app", POSTGRES_PASSWORD: "$DB_PASSWORD" },
      ports: [5432],
      ready: { command: ["pg_isready", "-U", "app"] },
    },
  },
  env: { DATABASE_URL: "postgres://app:$DB_PASSWORD@postgres:5432/app" },
  notes: "Python services: uv run pytest in the service directory.",
  connections: {
    github: { catalog: "github", config: { token: "$GITHUB_TOKEN" } },
    "internal-docs": { command: "npx", args: ["-y", "@acme/docs-mcp"], env: { DOCS_TOKEN: "$DOCS_TOKEN" } },
    tracker: { url: "https://mcp.example.com/mcp", headers: { Authorization: "Bearer $TRACKER_TOKEN" } },
  },
  agents: {
    developer: {
      name: "Developer", role: "You are a software developer on this team.", model: "claude-sonnet-5-5", provider: "main",
      identity: { name: "Developer", email: "dev-bot@acme.com" }, connections: ["github"],
    },
    scout: { role: "You investigate and report. You never change code." },
  },
};

const REQUIRED = ["GITHUB_TOKEN", "DB_PASSWORD", "DOCS_TOKEN", "TRACKER_TOKEN"];

/** The current API's shape: snapshot fields at the top level, required keys as {name, present}. */
export const HAPPY: Project = {
  id: "p1", slug: "bank", name: "Bank of Anthos", description: "Sample retail bank, Java and Python services.",
  config_url: "https://github.com/acme/bank-config.git", config_branch: "main", config_path: null,
  commit: "3f9c2a1b7e4d5c6f8a9b0c1d2e3f4a5b6c7d8e9f", read_at: "2026-11-07T10:00:00Z",
  branch: "main", path: "exradius.yaml",
  health: "ok", runnable: true, reason: null, errors: [], warnings: [],
  store: "bank-platform", store_id: "s1", store_exists: true,
  required_keys: REQUIRED.map((name) => ({ name, present: true })),
  missing_keys: [], agents: ["developer", "scout"],
  config: BANK_CONFIG,
  raw: "version: 1\nproject:\n  slug: bank\n",
  created_at: "2026-11-07T10:00:00Z",
};

export const MISSING_KEYS: Project = {
  ...HAPPY,
  id: "p2",
  health: "missing_secrets", runnable: false, reason: "missing secret DB_PASSWORD in store bank-platform",
  required_keys: REQUIRED.map((name) => ({ name, present: name !== "DB_PASSWORD" })),
  missing_keys: ["DB_PASSWORD"],
};

/** The nested snapshot of docs/config-repo.md §3, as an older API may send it. */
export const WITH_ERRORS: Project = {
  id: "p3", slug: "billing",
  config_url: "https://github.com/acme/billing-config.git", config_branch: null, config_path: ".exradius/exradius.yaml",
  snapshot: {
    commit: "a1b2c3d4e5f6", read_at: "2026-11-07T09:00:00Z", config: null,
    errors: ["services.postgres.imag: unknown key", "agents: at least one agent is required"],
    warnings: ["connections.github: requires npx, which the default workstation image lacks"],
    required_keys: [], store: null,
  },
  runnable: false,
};

/** The current API when the repository cannot be read: no commit, the read error first. */
export const UNREADABLE: Project = {
  id: "p4", slug: "ghost", name: "ghost", config_url: "https://github.com/acme/missing.git",
  config_branch: null, config_path: null, commit: null, read_at: "2026-11-07T09:00:00Z",
  health: "config_errors", runnable: false, reason: "config error: repository not found",
  errors: ["fatal: repository 'https://github.com/acme/missing.git/' not found"], warnings: [],
  store: null, store_exists: false, required_keys: [], missing_keys: [], agents: [], config: null, raw: null,
};
