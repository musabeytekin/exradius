import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError, isUnreachable, reachability, UNREACHABLE_MESSAGE } from "./api";

const respond = (status: number, body: string, type = "application/json") =>
  vi.fn().mockResolvedValue(new Response(body, { status, headers: { "content-type": type } }));

afterEach(() => vi.unstubAllGlobals());

describe("request errors", () => {
  it("treats a network failure as an unreachable API", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    const e = await api.system.status().catch((x) => x);
    expect(isUnreachable(e)).toBe(true);
    expect(e.message).toBe(UNREACHABLE_MESSAGE);
    expect(reachability.get()).toBe(false);
  });

  it("treats the proxy's HTML gateway page as unreachable, never showing it", async () => {
    vi.stubGlobal("fetch", respond(502, "<html><body><h1>502 Bad Gateway</h1></body></html>", "text/html"));
    const e = await api.system.status().catch((x) => x);
    expect(isUnreachable(e)).toBe(true);
    expect(e.message).not.toContain("<html>");
  });

  it("keeps a 503 the API itself sends as an ordinary error with its detail", async () => {
    vi.stubGlobal("fetch", respond(503, JSON.stringify({ detail: "could not reach the broker" })));
    const e = await api.tasks.cancel("t1").catch((x) => x);
    expect(e).toBeInstanceOf(ApiError);
    expect(isUnreachable(e)).toBe(false);
    expect(e.message).toBe("could not reach the broker");
    expect(reachability.get()).toBe(true);
  });

  it("marks the API reachable again on the next answer", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await api.system.status().catch(() => {});
    expect(reachability.get()).toBe(false);
    vi.stubGlobal("fetch", respond(200, "[]"));
    await api.projects.list();
    expect(reachability.get()).toBe(true);
  });
});
