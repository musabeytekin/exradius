import type * as T from "./types";

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  constructor(status: number, detail: unknown) {
    super(describeDetail(status, detail));
    this.status = status;
    this.detail = detail;
  }
}

/** FastAPI returns `detail` as a string or as a list of validation errors. */
function describeDetail(status: number, detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d: { loc?: unknown[]; msg?: string }) => {
        const loc = (d.loc ?? []).filter((p) => p !== "body").join(".");
        const msg = (d.msg ?? "invalid").replace(/^Value error, /, "");
        return loc ? `${loc}: ${msg}` : msg;
      })
      .join("; ");
  }
  if (status === 503) return "The broker cannot be reached.";
  return `Request failed (${status})`;
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

async function request<R>(method: string, path: string, body?: unknown, query?: Record<string, unknown>): Promise<R> {
  let url = `/api${path}`;
  if (query) {
    const params = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) {
      if (v !== undefined && v !== null && v !== "") params.set(k, String(v));
    }
    const qs = params.toString();
    if (qs) url += `?${qs}`;
  }
  const res = await fetch(url, {
    method,
    credentials: "same-origin",
    headers: body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 204) return undefined as R;
  const text = await res.text();
  const data = text ? safeJson(text) : undefined;
  if (!res.ok) {
    if (res.status === 401 && path !== "/auth/login" && path !== "/auth/me") onUnauthorized();
    const detail = data && typeof data === "object" && "detail" in data ? (data as { detail: unknown }).detail : text;
    throw new ApiError(res.status, detail);
  }
  return data as R;
}

function safeJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

const get = <R>(p: string, q?: Record<string, unknown>) => request<R>("GET", p, undefined, q);
const post = <R>(p: string, b?: unknown, q?: Record<string, unknown>) => request<R>("POST", p, b ?? {}, q);
const patch = <R>(p: string, b: unknown) => request<R>("PATCH", p, b);
const put = <R>(p: string, b: unknown) => request<R>("PUT", p, b);
const del = (p: string) => request<void>("DELETE", p);

export const api = {
  auth: {
    me: () => get<T.Admin>("/auth/me"),
    login: (username: string, password: string) => post<T.Admin>("/auth/login", { username, password }),
    logout: () => post<void>("/auth/logout"),
    changePassword: (current_password: string, new_password: string) =>
      post<void>("/auth/password", { current_password, new_password }),
  },
  system: {
    status: () => get<T.SystemStatus>("/system/status"),
    images: () => get<T.RunnerImage[]>("/system/images"),
    runtimes: () => get<T.Runtime[]>("/system/runtimes"),
    integration: () => get<T.Integration>("/system/integration"),
  },
  projects: {
    list: () => get<T.Project[]>("/projects"),
    get: (id: string) => get<T.Project>(`/projects/${id}`),
    create: (b: T.ProjectIn) => post<T.Project>("/projects", b),
    update: (id: string, b: Partial<T.ProjectIn>) => patch<T.Project>(`/projects/${id}`, b),
    remove: (id: string) => del(`/projects/${id}`),
    verify: (id: string) => post<{ verification_id: string }>(`/projects/${id}/verify`),
    verifications: (id: string, limit = 5) => get<T.Verification[]>(`/projects/${id}/verifications`, { limit }),
    verification: (id: string, vid: string) => get<T.Verification>(`/projects/${id}/verifications/${vid}`),
  },
  repositories: {
    list: (pid: string) => get<T.Repository[]>(`/projects/${pid}/repositories`),
    create: (pid: string, b: T.RepositoryIn) => post<T.Repository>(`/projects/${pid}/repositories`, b),
    update: (pid: string, id: string, b: Partial<T.RepositoryIn>) => patch<T.Repository>(`/projects/${pid}/repositories/${id}`, b),
    remove: (pid: string, id: string) => del(`/projects/${pid}/repositories/${id}`),
    check: (pid: string, id: string) => post<T.RepositoryCheck>(`/projects/${pid}/repositories/${id}/check`),
  },
  envVars: {
    list: (pid: string) => get<T.EnvVar[]>(`/projects/${pid}/env-vars`),
    create: (pid: string, b: { key: string; value: string }) => post<T.EnvVar>(`/projects/${pid}/env-vars`, b),
    update: (pid: string, id: string, b: { key?: string; value?: string }) => patch<T.EnvVar>(`/projects/${pid}/env-vars/${id}`, b),
    remove: (pid: string, id: string) => del(`/projects/${pid}/env-vars/${id}`),
  },
  secrets: {
    listGlobal: () => get<T.Secret[]>("/secrets"),
    listProject: (pid: string) => get<T.Secret[]>(`/projects/${pid}/secrets`),
    create: (pid: string | null, b: { key: string; value: string }) =>
      post<T.Secret>(pid ? `/projects/${pid}/secrets` : "/secrets", b),
    replace: (pid: string | null, id: string, value: string) =>
      put<T.Secret>(pid ? `/projects/${pid}/secrets/${id}` : `/secrets/${id}`, { value }),
    remove: (pid: string | null, id: string) => del(pid ? `/projects/${pid}/secrets/${id}` : `/secrets/${id}`),
  },
  connections: {
    list: () => get<T.Connection[]>("/connections"),
    create: (b: T.ConnectionIn) => post<T.Connection>("/connections", b),
    update: (id: string, b: Partial<T.ConnectionIn>) => patch<T.Connection>(`/connections/${id}`, b),
    remove: (id: string) => del(`/connections/${id}`),
    test: (id: string, opts?: { project_id?: string; agent_id?: string }) =>
      post<T.ConnectionTestResult>(`/connections/${id}/test`, undefined, opts),
  },
  providers: {
    list: () => get<T.Provider[]>("/providers"),
    create: (b: T.ProviderIn) => post<T.Provider>("/providers", b),
    update: (id: string, b: Partial<T.ProviderIn>) => patch<T.Provider>(`/providers/${id}`, b),
    remove: (id: string) => del(`/providers/${id}`),
    makeDefault: (id: string) => post<void>(`/providers/${id}/default`),
    test: (id: string) => post<T.ProviderTestStart>(`/providers/${id}/test`),
    testResult: (id: string, tid: string) => get<T.ProviderTest>(`/providers/${id}/tests/${tid}`),
  },
  agents: {
    list: () => get<T.Agent[]>("/agents"),
    create: (b: T.AgentIn) => post<T.Agent>("/agents", b),
    update: (id: string, b: Partial<T.AgentIn>) => patch<T.Agent>(`/agents/${id}`, b),
    remove: (id: string) => del(`/agents/${id}`),
  },
  projectAgents: {
    list: (pid: string) => get<T.ProjectAgent[]>(`/projects/${pid}/agents`),
    attach: (pid: string, b: { agent_id: string; connection_overrides: T.ProjectAgent["connection_overrides"]; secret_overrides: Record<string, string> }) =>
      post<T.ProjectAgent>(`/projects/${pid}/agents`, b),
    update: (pid: string, aid: string, b: Partial<Pick<T.ProjectAgent, "connection_overrides" | "secret_overrides">>) =>
      patch<T.ProjectAgent>(`/projects/${pid}/agents/${aid}`, b),
    detach: (pid: string, aid: string) => del(`/projects/${pid}/agents/${aid}`),
    resolved: (pid: string, aid: string) => get<T.ResolvedAgent>(`/projects/${pid}/agents/${aid}/resolved`),
  },
  tasks: {
    list: (q: { state?: string; project?: string; agent?: string; limit?: number; offset?: number }) =>
      get<T.TaskSummary[]>("/tasks", q),
    get: (id: string) => get<T.TaskDetail>(`/tasks/${encodeURIComponent(id)}`),
    transcript: (id: string, after_seq = 0, limit = 5000) =>
      get<T.TranscriptChunk[]>(`/tasks/${encodeURIComponent(id)}/transcript`, { after_seq, limit }),
    events: (id: string) => get<T.StoredEvent[]>(`/tasks/${encodeURIComponent(id)}/events`),
    cancel: (id: string) => post<{ run_id: string; status: string }>(`/tasks/${encodeURIComponent(id)}/cancel`),
    send: (b: T.TaskMessage) => post<T.Published>("/tasks", b),
    answer: (id: string, b: { request_id: string; answer: string; attachments: T.AttachmentIn[] }) =>
      post<T.Published>(`/tasks/${encodeURIComponent(id)}/answer`, b),
    streamUrl: (id: string) => `/api/tasks/${encodeURIComponent(id)}/stream`,
  },
  events: {
    recent: (q: { since?: string; limit?: number }) => get<T.StoredEvent[]>("/events", q),
  },
  artifactUrl: (id: string) => `/api/artifacts/${id}/content`,
  runTranscriptUrl: (runId: string) => `/api/runs/${runId}/transcript`,
};
