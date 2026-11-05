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
