import type { RunState, Status } from "./types";

export const STATUS_LABEL: Record<Status, string> = { todo: "To do", in_progress: "In progress", done: "Done" };
export const STATUSES: Status[] = ["todo", "in_progress", "done"];

export const RUN_LABEL: Record<RunState, string> = {
  sent: "Sent",
  queued: "Queued",
  preparing: "Preparing",
  running: "Running",
  waiting_for_input: "Asks a question",
  resuming: "Resuming",
  completed: "Finished",
  failed: "Could not finish",
  cancelled: "Stopped",
  rejected: "Could not start",
};

export function runTone(state: RunState): "info" | "warn" | "ok" | "err" | "muted" {
  if (state === "waiting_for_input") return "warn";
  if (state === "completed") return "ok";
  if (state === "failed" || state === "rejected") return "err";
  if (state === "cancelled") return "muted";
  return "info";
}

export function ago(iso: string, now: number = Date.now()): string {
  const seconds = Math.max(0, Math.round((now - new Date(iso).getTime()) / 1000));
  if (seconds < 45) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days < 30) return `${days} d ago`;
  return new Date(iso).toLocaleDateString();
}

export function exact(iso: string): string {
  return new Date(iso).toLocaleString();
}

export function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
