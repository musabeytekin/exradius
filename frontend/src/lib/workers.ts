import type { Runtime, Worker } from "./types";

export interface RuntimeSummary { name: string; label: string; versions: string[]; ready: boolean; notReadyOn: string[]; errors: string[] }

/**
 * Runtimes across live workers: one entry per runtime name, ready only when every worker
 * that reports it has it ready. `null` when no worker reports runtimes (an older worker).
 */
export function workerRuntimes(workers: Worker[], declared: Runtime[] | undefined): RuntimeSummary[] | null {
  if (!workers.some((w) => Array.isArray(w.runtimes))) return null;
  const by = new Map<string, RuntimeSummary>();
  for (const w of workers) {
    for (const r of w.runtimes ?? []) {
      const e = by.get(r.name) ?? { name: r.name, label: declared?.find((d) => d.name === r.name)?.label ?? r.name, versions: [], ready: true, notReadyOn: [], errors: [] };
      if (r.version && !e.versions.includes(r.version)) e.versions.push(r.version);
      if (!r.ready) {
        e.ready = false;
        e.notReadyOn.push(w.hostname);
        if (r.error && !e.errors.includes(r.error)) e.errors.push(r.error);
      }
      by.set(r.name, e);
    }
  }
  return [...by.values()];
}

/** A worker needs attention when Docker or a runtime (or, on older workers, the runner image) is not ready. */
export function workerHealthy(w: Worker): boolean {
  if (!w.docker_ok) return false;
  if (Array.isArray(w.runtimes)) return w.runtimes.length > 0 && w.runtimes.every((r) => r.ready);
  return w.runner_image_ok !== false;
}
