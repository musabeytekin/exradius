import { api } from "./api";
import type { ProviderTest } from "./types";

const POLL_MS = 2000;
const POLL_LIMIT = 120;

/** A worker-run test reports `state`; the synchronous one reports `ok`. */
export function testOk(t: ProviderTest): boolean | null {
  if (typeof t.ok === "boolean") return t.ok;
  if (t.state === "passed") return true;
  if (t.state === "failed") return false;
  return null;
}

/**
 * Run a provider test, optionally for one model, and wait for a worker-run test to end.
 * `onQueued` gets the test id of a worker-run test. Null when it is still running after the
 * polling window, or when `alive` turned false.
 */
export async function runProviderTest(
  id: string, opts: { model?: string | null; alive?: () => boolean; onQueued?: (testId: string) => void } = {},
): Promise<ProviderTest | null> {
  const alive = opts.alive ?? (() => true);
  const r = await api.providers.test(id, opts.model);
  if (!("test_id" in r)) return r;
  opts.onQueued?.(r.test_id);
  for (let i = 0; i < POLL_LIMIT && alive(); i++) {
    await new Promise((res) => setTimeout(res, POLL_MS));
    const t = await api.providers.testResult(id, r.test_id);
    if (testOk(t) !== null) return t;
  }
  return null;
}
