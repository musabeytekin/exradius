// Shapes of the admin API (backend/exradius/schemas/api.py). Dates are ISO strings.

export type Uuid = string;

export interface Admin { id: Uuid; username: string }

export interface Project {
  id: Uuid; slug: string; name: string; image: string | null;
  setup_commands: string[]; created_at: string;
}
export interface ProjectIn { slug: string; name: string; image?: string | null; setup_commands?: string[] }

export interface RepositoryCheck {
  ok: boolean; branches: string[]; default_branch: string | null;
  error: string | null; checked_at: string | null;
}
export interface Repository {
  id: Uuid; project_id: Uuid; name: string; url: string; branch: string;
  auth_secret_id: Uuid | null; last_check: RepositoryCheck | null;
  /** Absent on older APIs. */
  push_allowed?: boolean;
}
export interface RepositoryIn { name: string; url: string; branch: string; auth_secret_id: Uuid | null; push_allowed?: boolean }

export interface EnvVar { id: Uuid; project_id: Uuid; key: string; value: string }

/** Secrets are write-only: the API never returns a value. */
export interface Secret { id: Uuid; project_id: Uuid | null; key: string }

export interface ToolInfo { name: string; description: string }
export interface ConnectionLastTest { ok: boolean; tested_at: string; tools: ToolInfo[]; error: string | null }
export type Transport = "stdio" | "http";
export interface Connection {
  id: Uuid; slug: string; name: string; transport: Transport;
  command: string | null; args: string[]; url: string | null;
  env: Record<string, string>; headers: Record<string, string>;
  last_test: ConnectionLastTest | null; notice: string;
}
export interface ConnectionIn {
  slug: string; name: string; transport: Transport; command: string | null; args: string[];
  url: string | null; env: Record<string, string>; headers: Record<string, string>;
}
export interface ConnectionTestResult { ok: boolean; tools: { name: string; description?: string }[]; error: string | null }

export interface Agent {
  id: Uuid; slug: string; name: string; prompt: string; runtime: string;
  created_at: string; connection_ids: Uuid[];
  /** null = the default provider. Absent on backends without providers. */
  provider_id?: Uuid | null;
  /** Git identity (docs/agent-identity.md). Absent on older APIs. */
  identity_name?: string | null; identity_email?: string | null;
}
export interface AgentIn {
  slug: string; name: string; prompt: string; runtime: string; connection_ids: Uuid[]; provider_id?: Uuid | null;
  identity_name?: string | null; identity_email?: string;
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
export interface ProviderKindSpec { kind: string; label: string; auth_methods: AuthMethodSpec[] }

export interface ProviderLastTest { ok: boolean; tested_at: string; detail: unknown; error: string | null }
export interface Provider {
  id: Uuid; slug: string; name: string; kind: string; auth_method: string;
  settings: Record<string, string>; is_default: boolean;
  last_test: ProviderLastTest | null; agent_count: number;
  created_at?: string; updated_at?: string;
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

export type ConnectionOverride = Partial<Pick<ConnectionIn, "command" | "args" | "url" | "env" | "headers">>;
export interface ProjectAgent {
  project_id: Uuid; agent_id: Uuid;
  connection_overrides: Record<string, ConnectionOverride>;
  secret_overrides: Record<string, string>;
}

export interface SecretReference { name: string; resolves_to: string; scope: "project" | "global" | null }
export interface ResolvedConnection {
  slug: string; name: string; transport: string; command: string | null; args: string[];
  url: string | null; env: Record<string, string>; headers: Record<string, string>;
  overridden: boolean; references: SecretReference[]; unresolved: string[];
}
export interface ResolvedAgent {
  project_id: Uuid; agent_id: Uuid; secret_overrides: Record<string, string>;
  connections: ResolvedConnection[];
}

export type StepState = "pending" | "running" | "passed" | "failed" | "skipped";
export interface VerificationStep { name: string; state: StepState; started_at: string | null; ended_at: string | null; log: string }
export interface Verification {
  id: Uuid; project_id: Uuid; state: "pending" | "running" | "passed" | "failed";
  created_at: string; started_at: string | null; ended_at: string | null;
  error: string | null; steps: VerificationStep[];
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
}
export interface Run {
  id: Uuid; state: TaskState; container_id: string | null; runtime_session_id: string | null;
  started_at: string | null; ended_at: string | null; error: string | null;
  summary: string | null; output: unknown;
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
  version: number; task_id: string; run_id: string | null; project: string; agent: string;
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
  version?: 1; task_id: string; project: string; agent: string; instructions: string;
  context: unknown; attachments: AttachmentIn[]; correlation?: unknown;
}
export interface Published { queue: string; message: Record<string, unknown> }

export interface Check { ok: boolean; detail: string | null }
export interface Worker {
  id: string; hostname: string; started_at: string; last_seen_at: string; concurrency: number;
  docker_ok: boolean; docker_detail: string | null;
  /** Runtime bundles extracted on the worker (docs/runtime-bundles.md §3). Absent on older workers. */
  runtimes?: WorkerRuntime[];
  /** Older workers only; replaced by `runtimes`. */
  runner_image?: string; runner_image_ok?: boolean;
  version: string | null;
}
export interface WorkerRuntime { name: string; version: string | null; ready: boolean; image?: string | null; error?: string | null }
export interface SystemStatus {
  database: Check; broker: Check; workers: Worker[];
  provider?: ProviderStatus;
  public_api_url: string; version: string;
  /** Not in the contract yet; shown when an API reports it. */
  data_dir?: string | null;
}
export interface ProviderStatus {
  configured: boolean; count: number;
  default: { id: Uuid; name: string; kind: string; auth_method: string; last_test: ProviderLastTest | null } | null;
}
export interface RunnerImage { tag: string; created_at: string | null; size: number | null }
export interface Runtime { name: string; label: string; providers?: ProviderKindSpec[] }
export interface Integration {
  broker: { host: string; port: number; vhost: string; username: string | null; password_set: boolean };
  tasks_queue: string; answers_queue: string; events_exchange: string; events_exchange_type: string;
  routing_keys: string[]; public_api_url: string;
}
