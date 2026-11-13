// The only client of Desk's API. Same origin, the session cookie, JSON or multipart.
import type { Me, Project, PersonRef, Task, TaskItem } from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

let onUnauthorized: () => void = () => {};
export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method, credentials: "same-origin", headers: {} };
  if (body instanceof FormData) {
    init.body = body;
  } else if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["Content-Type"] = "application/json";
  }
  const response = await fetch(path, init);
  if (response.status === 401) {
    onUnauthorized();
    throw new ApiError(401, "sign in");
  }
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const data = await response.json();
      if (typeof data?.detail === "string") detail = data.detail;
      else if (Array.isArray(data?.detail)) detail = data.detail.map((d: { msg: string }) => d.msg).join("; ");
    } catch {
      // not JSON
    }
    throw new ApiError(response.status, detail);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

function form(fields: Record<string, string | undefined>, files: File[] = []): FormData {
  const data = new FormData();
  for (const [k, v] of Object.entries(fields)) if (v !== undefined) data.append(k, v);
  for (const f of files) data.append("files", f, f.name);
  return data;
}

export interface TaskFilters {
  project?: string;
  assignee?: string;
  status?: string;
}

function query(filters: TaskFilters): string {
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(filters)) if (v) params.set(k, v);
  const text = params.toString();
  return text ? `?${text}` : "";
}

export const api = {
  me: () => request<Me>("GET", "/api/me"),
  logout: () => request<void>("POST", "/auth/logout"),
  projects: () => request<Project[]>("GET", "/api/projects"),
  people: (project: string) => request<PersonRef[]>("GET", `/api/projects/${encodeURIComponent(project)}/people`),
  tasks: (filters: TaskFilters) => request<TaskItem[]>("GET", `/api/tasks${query(filters)}`),
  task: (key: string) => request<Task>("GET", `/api/tasks/${encodeURIComponent(key)}`),
  create: (fields: { project: string; title: string; description: string; assignee: string }, files: File[]) =>
    request<{ key: string }>("POST", "/api/tasks", form(fields, files)),
  edit: (key: string, changes: { title?: string; description?: string; status?: string }) =>
    request<void>("PATCH", `/api/tasks/${encodeURIComponent(key)}`, changes),
  assign: (key: string, assignee: string) =>
    request<void>("POST", `/api/tasks/${encodeURIComponent(key)}/assignee`, { assignee }),
  addFiles: (key: string, files: File[]) =>
    request<void>("POST", `/api/tasks/${encodeURIComponent(key)}/files`, form({}, files)),
  comment: (key: string, body: string, files: File[]) =>
    request<{ id: number; effect: "answered" | "started" | null }>(
      "POST", `/api/tasks/${encodeURIComponent(key)}/comments`, form({ body }, files)),
  cancel: (key: string) => request<void>("POST", `/api/tasks/${encodeURIComponent(key)}/cancel`),
};

export const attachmentUrl = (id: number) => `/api/attachments/${id}`;
export const artifactUrl = (id: number) => `/api/artifacts/${id}`;
