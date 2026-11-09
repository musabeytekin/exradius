import type { Tone } from "@/components/ui/primitives";

export const STATE_TONE: Record<string, Tone> = {
  queued: "neutral",
  preparing: "info",
  running: "info",
  resuming: "info",
  waiting_for_input: "warn",
  completed: "ok",
  failed: "err",
  cancelled: "neutral",
  rejected: "err",
  pending: "neutral",
  passed: "ok",
  skipped: "neutral",
};

/** What a run in `preparing` waits for (`runs.waiting_for`), as the task row says it. */
export const WAITING_FOR: Record<string, string> = {
  capacity: "waiting for cluster capacity",
  build: "building images",
};

export function waitingLabel(state: string | undefined, waitingFor: string | null | undefined): string | null {
  return state === "preparing" && waitingFor ? WAITING_FOR[waitingFor] ?? null : null;
}
