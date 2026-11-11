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

/**
 * The API did not answer: the network failed, or the proxy in front of it returned a gateway
 * error page (502/503/504 without the API's JSON body), typically while the api container
 * restarts. A 503 the API itself sends, such as a broker outage, stays an ordinary ApiError.
 */
export class ApiUnreachableError extends ApiError {
  constructor(status: number) {
    super(status, UNREACHABLE_MESSAGE);
  }
}
export const UNREACHABLE_MESSAGE = "Exradius API is not reachable.";
export const isUnreachable = (e: unknown): e is ApiUnreachableError => e instanceof ApiUnreachableError;
/** The API's own message for a refused request, or null when the API did not answer. */
export const refusal = (e: unknown): string | null => (e instanceof ApiError && !(e instanceof ApiUnreachableError) ? e.message : null);

// Whether the last request reached the API, for the shell's banner.
let reachable = true;
const reachListeners = new Set<() => void>();
function setReachable(v: boolean) {
  if (v === reachable) return;
  reachable = v;
  reachListeners.forEach((l) => l());
}
export const reachability = {
  get: () => reachable,
  subscribe: (l: () => void) => {
    reachListeners.add(l);
    return () => reachListeners.delete(l);
  },
};

const GATEWAY = new Set([502, 503, 504]);

/** FastAPI returns `detail` as a string or as a list of validation errors. */
function describeDetail(status: number, detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((d: string | { loc?: unknown[]; msg?: string }) => {
        if (typeof d === "string") return d;
        const loc = (d.loc ?? []).filter((p) => p !== "body").join(".");
        const msg = (d.msg ?? "invalid").replace(/^Value error, /, "");
        return loc ? `${loc}: ${msg}` : msg;
      })
      .join("; ");
  }
  if (detail && typeof detail === "object") {
    const o = detail as { message?: unknown; errors?: unknown };
    const parts = [typeof o.message === "string" ? o.message : null, ...(Array.isArray(o.errors) ? o.errors.map(String) : [])].filter(Boolean);
    if (parts.length) return parts.join("\n");
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
  let res: Response;
  try {
    res = await fetch(url, {
      method,
      credentials: "same-origin",
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    setReachable(false);
    throw new ApiUnreachableError(0);
  }
  if (res.status === 204) {
    setReachable(true);
    return undefined as R;
  }
  const text = await res.text();
  const data = text ? safeJson(text) : undefined;
  const fromApi = typeof data === "object" && data !== null;
  if (GATEWAY.has(res.status) && !fromApi) {
    setReachable(false);
    throw new ApiUnreachableError(res.status);
  }
  setReachable(true);
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
    runtimes: () => get<T.Runtime[]>("/system/runtimes"),
    integration: () => get<T.Integration>("/system/integration"),
  },
  projects: {
    list: () => get<T.Project[]>("/projects"),
    get: (id: string) => get<T.Project>(`/projects/${id}`),
    create: (b: T.ProjectIn) => post<T.Project>("/projects", b),
    update: (id: string, b: Partial<Pick<T.ProjectIn, "config_branch" | "config_path">>) => patch<T.Project>(`/projects/${id}`, b),
    remove: (id: string) => del(`/projects/${id}`),
    reread: (id: string) => post<T.Project>(`/projects/${id}/reread`),
    /** A new config hook URL; the old one stops working. */
    replaceHookToken: (id: string) => post<T.Project>(`/projects/${id}/hook-token`),
    verify: (id: string) => post<{ verification_id: string }>(`/projects/${id}/verify`),
    verifications: (id: string, limit = 5) => get<T.Verification[]>(`/projects/${id}/verifications`, { limit }),
    verification: (id: string, vid: string) => get<T.Verification>(`/projects/${id}/verifications/${vid}`),
    /** Each agent as routing sees it; empty when the config has errors. */
    agents: (id: string) => get<T.AgentProfile[]>(`/projects/${id}/agents`),
  },
  catalog: {
    list: () => get<T.CatalogEntry[]>("/catalog"),
  },
  stores: {
    list: () => get<T.SecretStore[]>("/stores"),
    create: (name: string, allowed_projects: string[] = []) => post<T.SecretStore>("/stores", { name, allowed_projects }),
    /** Replaces the projects allowed to use the store. */
    update: (id: string, b: { allowed_projects?: string[] }) => patch<T.SecretStore>(`/stores/${id}`, b),
    remove: (id: string) => del(`/stores/${id}`),
    /** The store's change log, newest first. */
    changes: (id: string) => get<T.StoreChange[]>(`/stores/${id}/changes`),
    keys: (id: string) => get<(string | T.SecretStoreKey)[]>(`/stores/${id}/keys`),
    /** Write-only: the value is never returned. */
    setKey: (id: string, key: string, value: string) => put<unknown>(`/stores/${id}/keys/${encodeURIComponent(key)}`, { value }),
    /** Set or replace several values in one transaction; returns the store's key list. */
    setKeys: (id: string, values: Record<string, string>) => put<T.SecretStoreKey[]>(`/stores/${id}/keys`, { values }),
    removeKey: (id: string, key: string) => del(`/stores/${id}/keys/${encodeURIComponent(key)}`),
  },
  gitCredentials: {
    list: () => get<T.GitCredential[]>("/git-credentials"),
    create: (b: T.GitCredentialIn) => post<T.GitCredential>("/git-credentials", b),
    remove: (id: string) => del(`/git-credentials/${id}`),
  },
  integrationTokens: {
    list: () => get<T.IntegrationToken[]>("/integration-tokens"),
    /** The answer is the only time the token is shown. */
    create: (b: T.IntegrationTokenIn) => post<T.IntegrationTokenCreated>("/integration-tokens", b),
    revoke: (id: string) => del(`/integration-tokens/${id}`),
  },
  bridges: {
    list: () => get<T.Bridge[]>("/bridges"),
  },
  routing: {
    get: () => get<T.RoutingSettings>("/routing"),
    /** Write-only: the key is never returned. */
    setKey: (key: string) => put<T.RoutingSettings>("/routing/key", { key }),
    removeKey: () => del("/routing/key"),
    test: () => post<T.RoutingTest>("/routing/test"),
  },
  providers: {
    list: () => get<T.Provider[]>("/providers"),
    create: (b: T.ProviderIn) => post<T.Provider>("/providers", b),
    update: (id: string, b: Partial<T.ProviderIn>) => patch<T.Provider>(`/providers/${id}`, b),
    remove: (id: string) => del(`/providers/${id}`),
    makeDefault: (id: string) => post<void>(`/providers/${id}/default`),
    /** `model` checks that id with the credential; older APIs ignore it. */
    test: (id: string, model?: string | null) => post<T.ProviderTestStart>(`/providers/${id}/test`, model ? { model } : {}),
    testResult: (id: string, tid: string) => get<T.ProviderTest>(`/providers/${id}/tests/${tid}`),
  },
  tasks: {
    list: (q: { state?: string; project?: string; agent?: string; limit?: number; offset?: number }) =>
      get<T.TaskSummary[]>("/tasks", q),
    get: (id: string) => get<T.TaskDetail>(`/tasks/${encodeURIComponent(id)}`),
    transcript: (id: string, after_seq = 0, limit = 5000) =>
      get<T.TranscriptChunk[]>(`/tasks/${encodeURIComponent(id)}/transcript`, { after_seq, limit }),
    events: (id: string) => get<T.StoredEvent[]>(`/tasks/${encodeURIComponent(id)}/events`),
    /** Hosts the run reached and was refused, from the egress proxy's records. */
    network: (id: string) => get<T.TaskNetwork>(`/tasks/${encodeURIComponent(id)}/network`),
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
