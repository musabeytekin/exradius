import { describe, expect, it } from "vitest";
import { runtimeAccepts } from "./providers";
import type { Runtime, Worker } from "./types";
import { workerHealthy, workerRuntimes } from "./workers";

const worker = (hostname: string, extra: Partial<Worker>): Worker => ({
  id: hostname, hostname, started_at: "2026-11-05T10:00:00Z", last_seen_at: "2026-11-05T10:00:00Z",
  concurrency: 2, runtimes: [], version: "0.2.0", ...extra,
});
const RUNTIMES: Runtime[] = [{
  name: "claude_code", label: "Claude Code",
  providers: [{ kind: "anthropic", label: "Anthropic", auth_methods: [{ name: "api_key", label: "API key", credential_label: "API key", settings: [] }] }],
}];

describe("workerRuntimes", () => {
  it("merges runtimes across workers and is ready only when every worker has it ready", () => {
    const s = workerRuntimes([
      worker("a", { runtimes: [{ name: "claude_code", version: "2.1.3", ready: true }] }),
      worker("b", { runtimes: [{ name: "claude_code", version: "2.1.4", ready: false, image: "exradius-runtime-claude-code:0.1.0", error: "image not found" }] }),
    ], RUNTIMES);
    expect(s).toEqual([{ name: "claude_code", label: "Claude Code", versions: ["2.1.3", "2.1.4"], ready: false, notReadyOn: ["b"], errors: ["image not found"] }]);
  });

  it("judges worker health by its runtimes", () => {
    expect(workerHealthy(worker("a", { runtimes: [{ name: "claude_code", version: null, ready: true }] }))).toBe(true);
    expect(workerHealthy(worker("a", { runtimes: [] }))).toBe(false);
    expect(workerHealthy(worker("a", { runtimes: [{ name: "claude_code", version: null, ready: false }] }))).toBe(false);
  });
});

describe("runtimeAccepts", () => {
  it("filters providers by the kinds and auth methods a runtime declares", () => {
    expect(runtimeAccepts(RUNTIMES[0], { kind: "anthropic", auth_method: "api_key" })).toBe(true);
    expect(runtimeAccepts(RUNTIMES[0], { kind: "anthropic", auth_method: "oauth_token" })).toBe(false);
    expect(runtimeAccepts(RUNTIMES[0], { kind: "openai", auth_method: "api_key" })).toBe(false);
    // A runtime that declares nothing (an older API) accepts every provider.
    expect(runtimeAccepts({ name: "x", label: "X" }, { kind: "openai", auth_method: "api_key" })).toBe(true);
  });
});
