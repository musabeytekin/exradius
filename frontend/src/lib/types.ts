// Shapes of the admin API (backend/exradius/schemas/api.py). Dates are ISO strings.

export type Uuid = string;

export interface Admin { id: Uuid; username: string }

// --- Projects as config repositories (docs/config-repo.md §3, §11) ---
// The panel renders what the API returns and tolerates missing fields: a project whose
// snapshot or config is absent still renders, with an explanation.

export interface ConfigRepository {
  name: string; url: string; branch?: string | null;
  /** As the API returns it: a `$NAME` placeholder, never a value. */
  credential?: string | null;
  push?: boolean | null;
}
export interface ConfigWorkstation {
  image?: string | null; dockerfile?: string | null; context?: string | null; setup?: string[] | null;
  /** Directories kept across the project's runs (`~/.cache/uv`). */
  cache?: string[] | null;
}
export type ConfigReady = null | { tcp?: number | null; command?: string[] | null };
export interface ConfigService {
  name?: string; image?: string | null; dockerfile?: string | null; context?: string | null;
  env?: Record<string, string> | null; ports?: number[] | null; ready?: ConfigReady;
  /** The container command; the image's entrypoint is kept. */
  command?: string[] | null;
  /** Declared memory as written (`512m`): reserved and enforced. */
  memory?: string | null;
}
export interface ConfigConnection {
  name?: string; catalog?: string | null; config?: Record<string, string> | null;
  command?: string | null; args?: string[] | null; env?: Record<string, string> | null;
  url?: string | null; headers?: Record<string, string> | null; type?: string | null;
}
export interface ConfigIdentity { name?: string | null; email?: string | null }
export interface ConfigAgent {
  slug?: string; name?: string | null; role?: string | null; model?: string | null;
  provider?: string | null; runtime?: string | null; identity?: ConfigIdentity | null;
  connections?: string[] | null;
}
/** The parsed `exradius.yaml`. Maps may arrive keyed by name or as lists with a `name`/`slug`. */
export interface ProjectConfig {
  version?: number;
  project?: { slug?: string; name?: string | null; description?: string | null } | null;
  secret_store?: string | null;
  repositories?: ConfigRepository[] | null;
  workstation?: ConfigWorkstation | null;
  /** `memory`: the workstation's declared memory as written (`6g`). */
  resources?: { memory?: string | null } | null;
  services?: Record<string, ConfigService> | ConfigService[] | null;
  env?: Record<string, string> | null;
  notes?: string | null;
  connections?: Record<string, ConfigConnection> | ConfigConnection[] | null;
  agents?: Record<string, ConfigAgent> | ConfigAgent[] | null;
}
export interface ProjectConfigSnapshot {
  commit?: string | null; read_at?: string | null;
  /** null when the file has errors. */
  config?: ProjectConfig | null;
  errors?: string[] | null; warnings?: string[] | null;
  /** Secret names the file references. */
  required_keys?: (string | RequiredKeyIn)[] | null;
  store?: string | null;
  /** The file as read, when the API returns it. */
  raw?: string | null;
  /** Set when the last read failed (the repository or the file could not be read). */
  read_error?: string | null;
}
export interface RequiredKey { key: string; present: boolean }
/** As the API sends it: `name` (current backend) or `key`. */
export interface RequiredKeyIn { key?: string; name?: string; present?: boolean }
/**
 * A project as `GET /api/projects[/{id}]` returns it. The current API puts the snapshot's fields
 * at the top level (`commit`, `read_at`, `config`, `raw`, `errors` ...); a nested `snapshot` is
 * read too. `commit` null means the repository or the file could not be read.
 */
export interface Project {
  id: Uuid; slug: string;
  config_url: string; config_branch?: string | null; config_path?: string | null;
  created_at?: string;
  /** The file's `project.name` (the slug when unreadable) and description. */
  name?: string | null; description?: string | null;
  commit?: string | null; read_at?: string | null;
  /** The branch and file actually read. */
  branch?: string | null; path?: string | null;
  config?: ProjectConfig | null;
  raw?: string | null;
  health?: string | null;
  /** Why tasks are refused, as the rejection says it. */
  reason?: string | null;
  runnable?: boolean | null;
  errors?: string[] | null; warnings?: string[] | null;
  store?: string | null; store_id?: Uuid | null;
  /** Whether the store the file names exists. */
  store_exists?: boolean | null;
  required_keys?: (string | RequiredKeyIn)[] | null;
  missing_keys?: string[] | null;
  /** Agent slugs of the config. */
  agents?: string[] | null;
  snapshot?: ProjectConfigSnapshot | null;
  snapshot_commit?: string | null; snapshot_read_at?: string | null;
  read_error?: string | null;
  /** The latest verification and what it says about the commit read (setup-and-verify.md §1.2). */
  verification?: VerificationSummary | null;
  freshness?: Freshness | null;
  /** The config hook URL and its last accepted call (integration-api.md §2). */
  hook_url?: string | null;
  hook_last_at?: string | null;
  /** The config's `bridges/<name>.yaml` files (integration-api.md §6). */
  bridges?: BridgeFile[] | null;
  /** What bridges last reported about this project (integration-api.md §7). */
  bridge_problems?: BridgeProjectProblem[] | null;
}
/** A bridge file: its content parsed from YAML, or why it could not be read. */
export interface BridgeFile { name: string; config: Record<string, unknown> | null; error: string | null }
export interface BridgeProjectProblem { bridge: string; message: string; reported_at: string }
export type Freshness = "current" | "stale" | "failed" | "running" | "never";
export interface VerificationSummary {
  id: Uuid; state: Verification["state"]; commit: string | null; ended_at: string | null; diagnosis: string | null;
}
export interface ProjectIn { config_url: string; config_branch?: string | null; config_path?: string | null }

export interface CatalogConfigKey {
  key?: string; name?: string | null; label?: string | null; required?: boolean;
  target?: string | null; format?: string | null; default?: string | null;
}
export interface CatalogEntry {
  key: string; name: string; description?: string | null; transport?: string | null;
  command?: string | null; args?: string[] | null; url?: string | null;
  requires?: string | null;
  /** The config keys it accepts. */
  config?: CatalogConfigKey[] | null;
}

/** Secret stores (§7). Values are write-only. */
export interface SecretStore {
  id: Uuid; name: string; kind?: string; created_at?: string;
  key_count?: number | null; keys?: (string | SecretStoreKey)[] | null;
  /** Slugs of the projects whose config names it. */
  used_by?: string[] | null;
}
export interface SecretStoreKey { key: string; updated_at?: string | null }

/** Config repository access per git host (§6). */
export type GitCredentialKind = "token" | "basic";
export interface GitCredential { id: Uuid; host: string; kind: GitCredentialKind; created_at?: string }
/** A bearer token for /api/integration/v1 (integration-api.md §5); the value is only in the create answer. */
export interface IntegrationToken {
  id: Uuid; name: string;
  /** The bridge the token is bound to. Absent on older APIs. */
  bridge?: string;
  created_at: string; last_used_at: string | null; revoked_at: string | null;
}
export interface IntegrationTokenCreated extends IntegrationToken { token: string }
export interface IntegrationTokenIn { bridge: string; name?: string | null }
/** A bridge as `GET /api/bridges` returns it: from its tokens, its files and its last report (§7). */
export interface Bridge {
  bridge: string;
  /** Tokens bound to it that are not revoked. */
  tokens: number;
  /** Slugs of the projects whose config has its file. */
  projects: string[];
  reported_at: string | null;
  /** A report in the last 15 minutes. */
  reporting: boolean;
  software: string | null; started_at: string | null;
  problems: BridgeProblem[];
}
/** `project` null: about the bridge as a whole. */
export interface BridgeProblem { project: string | null; message: string }

// --- Agent routing (docs/agent-routing.md) ---

export interface RoutingTest { ok: boolean; at: string; ms: number | null; detail: string }
export interface RoutingSettings {
  key_set: boolean; key_set_at: string | null; last_test: RoutingTest | null;
  model: string; guess_below: number;
}
/** What routing sees of an agent (§3). */
export interface AgentProfile { slug: string; name: string; role: string | null; connections: ProfileConnection[] }
export interface ProfileConnection {
  name: string;
  /** `catalog <key>`, `custom stdio` or `custom http`. */
  kind: string;
  /** null when not listed for the connection as the config writes it now; `tools_note` says why. */
  tools: ProfileTool[] | null;
  tools_listed_at?: string | null;
  tools_note?: string | null;
}
export interface ToolAnnotations {
  title?: string | null; readOnlyHint?: boolean | null; destructiveHint?: boolean | null;
  idempotentHint?: boolean | null; openWorldHint?: boolean | null;
  [k: string]: unknown;
}
export interface ProfileTool { name: string; description?: string | null; annotations?: ToolAnnotations | null }
/** `runs.routing` (§8): the decision's fields only for `decided`. */
export interface RunRouting {
  method: "named" | "only_agent" | "decided";
  agent: string;
  decided_at?: string | null;
  confidence?: number | null;
  guess?: boolean;
  probabilities?: Record<string, number> | null;
  model?: string | null; ms?: number | null;
  input_tokens?: number | null; cached_tokens?: number | null;
}
export interface GitCredentialIn { host: string; kind: GitCredentialKind; value: string }

/** What a run recorded of its agent (runs.agent_snapshot, §8). */
export interface AgentSnapshot {
  slug?: string | null; name?: string | null; model?: string | null; provider?: string | null;
  identity?: ConfigIdentity | null; connections?: string[] | null;
}

// --- Providers (docs/providers.md) ---

/** A settings field an auth method needs (region, project, base_url ...). */
export interface ProviderSettingSpec {
  name: string; label?: string; required?: boolean; default?: string | null;
  placeholder?: string | null; hint?: string | null; options?: string[] | null;
}
export interface AuthMethodSpec {
  name: string; label: string; credential_label: string;
  settings: ProviderSettingSpec[]; notice?: string | null;
}
/** A model a provider offers (docs/environment-and-services.md §3). */
export interface ProviderModel { id: string; label?: string | null; description?: string | null }
export interface ProviderKindSpec {
  kind: string; label: string; auth_methods: AuthMethodSpec[];
  /** Absent on older APIs; entries may be bare ids. */
  models?: (ProviderModel | string)[] | null;
}

export interface ProviderLastTest { ok: boolean; tested_at: string; detail: unknown; error: string | null }
export interface Provider {
  id: Uuid; slug: string; name: string; kind: string; auth_method: string;
  settings: Record<string, string>; is_default: boolean;
  last_test: ProviderLastTest | null;
  /** Agents (across project configs) that name it. Absent on some APIs. */
  agent_count?: number;
  created_at?: string; updated_at?: string;
  /** Models the provider offers. Absent on older APIs. */
  models?: (ProviderModel | string)[] | null;
}
export interface ProviderIn {
  slug: string; name: string; kind: string; auth_method: string;
  /** Write-only. On update, omit to keep the stored credential. */
  credential?: string; settings: Record<string, string>;
}
/** 200 for a synchronous test, 202 `{test_id}` for one the worker runs. */
export type ProviderTestStart = { ok: boolean; detail: unknown; error: string | null } | { test_id: string };
export interface ProviderTest {
  id?: string; state?: string; ok?: boolean | null; detail?: unknown; error?: string | null; tested_at?: string | null;
}

export type StepState = "pending" | "running" | "passed" | "failed" | "skipped";
export interface VerificationStep { name: string; state: StepState; started_at: string | null; ended_at: string | null; log: string }
export interface Verification {
  id: Uuid; project_id: Uuid; state: "pending" | "running" | "passed" | "failed";
  created_at: string; started_at: string | null; ended_at: string | null;
  error: string | null; steps: VerificationStep[];
  /** The config commit "Read config" read; the diagnosis of a failure, when one was recognised. */
  commit?: string | null; diagnosis?: string | null;
}

export type TaskState =
  | "queued" | "preparing" | "running" | "waiting_for_input" | "resuming"
  | "completed" | "failed" | "cancelled" | "rejected";

export const TERMINAL_STATES: TaskState[] = ["completed", "failed", "cancelled", "rejected"];
export const ALL_STATES: TaskState[] = [
  "queued", "preparing", "running", "waiting_for_input", "resuming",
  "completed", "failed", "cancelled", "rejected",
];

export interface TaskSummary {
  task_id: string; project: string; agent: string; state: TaskState;
  received_at: string; run_id: Uuid | null;
  /** While the run stays preparing: `capacity` (its pod waits for room in the cluster) or
   * `build` (its images are being built). */
  waiting_for?: string | null;
}
export interface Run {
  id: Uuid; state: TaskState;
  /** The run's Kubernetes Run object (`run-<id>`), deleted when the run ends. */
  run_object?: string | null;
  runtime_session_id: string | null;
  started_at: string | null; ended_at: string | null; error: string | null;
  summary: string | null; output: unknown;
  /** The environment section the run was given, when the API exposes it. */
  environment_text?: string | null;
  spec?: { environment_text?: string | null } | null;
  /** The config repository commit the run used. */
  config_commit?: string | null;
  agent_snapshot?: AgentSnapshot | null;
  waiting_for?: string | null;
  /** How the agent was picked; null for runs from before routing. */
  routing?: RunRouting | null;
}
export interface TaskAttachment { id: Uuid; name: string; content_type: string; object_key: string | null; source_url: string | null }
export interface InfoRequest {
  id: Uuid; question: string; context: string | null; created_at: string;
  answered_at: string | null; answer: string | null;
  answer_attachments: unknown[] | null; late_answers: unknown[];
}
export interface Artifact { id: Uuid; name: string; content_type: string; object_key: string }

/** The outbound envelope (architecture §4.3); stored as `EventOut.payload`. */
export interface Envelope<P = Record<string, unknown>> {
  version: number; task_id: string; run_id: string | null; project: string;
  /** null on a rejection of a task that named no agent. */
  agent: string | null;
  correlation: unknown; timestamp: string; type: "status" | "info_request" | "result"; payload: P;
}
export interface StatusPayload { state: TaskState; previous: TaskState | null; reason: string | null }
export interface InfoRequestPayload { request_id: string; question: string; context: string | null }
export interface ResultPayload {
  state: TaskState; summary: string | null; output: unknown;
  artifacts: { name: string; content_type: string; url: string }[];
  transcript_url: string | null; error: string | null;
}

export interface StoredEvent {
  id: Uuid; task_id: string; run_id: Uuid | null; type: Envelope["type"];
  payload: Envelope; created_at: string; published_at: string | null;
}

/** One host a run's pod asked the egress proxy for (`GET /api/tasks/{id}/network`). */
export interface NetworkHost {
  host: string; ports: number[];
  /** `setup` (clones, services, setup commands: any host) and/or `agent` (the allowlist). */
  phases: ("setup" | "agent")[];
  /** `connect` (a tunnel), `http`, or a route of the proxy: `model`, `git`, `mcp`. */
  routes: string[];
  requests: number; bytes: number; last_at: string | null;
  /** Why the latest request was refused (refused hosts only). */
  reason: string | null;
}
export interface TaskNetwork { reached: NetworkHost[]; refused: NetworkHost[]; requests: number }

export interface TaskDetail extends TaskSummary {
  instructions: string; context: unknown; correlation: unknown;
  attachments: TaskAttachment[]; run: Run | null; info_requests: InfoRequest[];
  artifacts: Artifact[]; events: StoredEvent[];
}

export interface TranscriptChunk { seq: number; payload: SdkMessage }
/** One Claude Agent SDK message, serialised by the runner: dataclass fields plus `type`. */
export type SdkMessage = { type: string } & Record<string, unknown>;

export interface AttachmentIn { name: string; content_type: string; url?: string; content_base64?: string }
export interface TaskMessage {
  version?: 1; task_id: string; project: string;
  /** Omitted: Exradius picks one of the project's agents. */
  agent?: string | null;
  instructions: string;
  context: unknown; attachments: AttachmentIn[]; correlation?: unknown;
}
export interface Published { queue: string; message: Record<string, unknown> }

export interface Check { ok: boolean; detail: string | null }
export interface Worker {
  id: string; hostname: string; started_at: string; last_seen_at: string; concurrency: number;
  /** The runtime bundles the worker is configured with (docs/kubernetes.md §15). */
  runtimes: WorkerRuntime[];
  version: string | null;
}
/** The operator as the workers see its leader Lease (docs/kubernetes.md §15). */
export interface OperatorStatus { alive: boolean; holder?: string | null; renewed_at?: string | null; detail?: string | null }
export interface WorkerRuntime { name: string; version: string | null; ready: boolean; image?: string | null; error?: string | null }
export interface SystemStatus {
  database: Check; broker: Check; workers: Worker[];
  /** `null` while no live worker reported it. */
  operator?: OperatorStatus | null;
  provider?: ProviderStatus;
  public_api_url: string; version: string;
}
export interface ProviderStatus {
  configured: boolean; count: number;
  default: { id: Uuid; name: string; kind: string; auth_method: string; last_test: ProviderLastTest | null } | null;
}
export interface Runtime { name: string; label: string; providers?: ProviderKindSpec[] }
export interface Integration {
  broker: { host: string; port: number; vhost: string; username: string | null; password_set: boolean };
  tasks_queue: string; answers_queue: string; events_exchange: string; events_exchange_type: string;
  routing_keys: string[]; public_api_url: string;
}
