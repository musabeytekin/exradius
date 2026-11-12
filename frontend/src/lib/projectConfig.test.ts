import { describe, expect, it } from "vitest";
import {
  HAPPY, MISSING_KEYS, STORE_NOT_ALLOWED, STORE_UNREACHABLE, UNREACHABLE_REASON, UNREADABLE, WITH_ERRORS,
} from "@/test/projectFixtures";
import { agentSlugsOf, agentsOf, entries, missingKeys, projectHealth, requiredKeys, servicesOf, shortCommit, storeNameOf } from "./projectConfig";
import type { Project } from "./types";

describe("project health", () => {
  it("is ok when the file reads cleanly and every key is present", () => {
    expect(projectHealth(HAPPY)).toMatchObject({ health: "ok", tone: "ok", label: "ok" });
  });

  it("reports config errors before anything else that is readable", () => {
    const h = projectHealth(WITH_ERRORS);
    expect(h).toMatchObject({ health: "config_errors", tone: "err", label: "2 config errors" });
    expect(h.detail).toBe("services.postgres.imag: unknown key");
  });

  it("reports missing secrets from the API's report", () => {
    expect(projectHealth(MISSING_KEYS)).toMatchObject({ health: "missing_secrets", tone: "warn", label: "1 missing secret" });
    expect(projectHealth(MISSING_KEYS).detail).toBe("Missing DB_PASSWORD in store bank-platform.");
  });

  it("checks presence against the live store keys when known", () => {
    // The API still says missing, but the store now has every key.
    const all = ["GITHUB_TOKEN", "DB_PASSWORD", "DOCS_TOKEN", "TRACKER_TOKEN", "UNRELATED"];
    expect(projectHealth(MISSING_KEYS, all).health).toBe("ok");
    expect(missingKeys(HAPPY, ["GITHUB_TOKEN"])).toEqual(["DB_PASSWORD", "DOCS_TOKEN", "TRACKER_TOKEN"]);
  });

  it("treats a store that does not exist as missing secrets", () => {
    const h = projectHealth(HAPPY, null);
    expect(h).toMatchObject({ health: "missing_secrets", detail: "Secret store bank-platform does not exist." });
    expect(projectHealth({ ...HAPPY, store_exists: false }).health).toBe("missing_secrets");
  });

  it("is unreadable without a snapshot, with a read error, or without a commit", () => {
    expect(projectHealth(UNREADABLE)).toMatchObject({ health: "unreadable", tone: "err" });
    expect(projectHealth(UNREADABLE).detail).toMatch(/not found/);
    expect(projectHealth({ id: "x", slug: "x", config_url: "u" }).health).toBe("unreadable");
    expect(projectHealth({ ...HAPPY, commit: null }).health).toBe("unreadable");
    expect(projectHealth({ ...HAPPY, read_error: "auth failed" }).detail).toBe("auth failed");
  });

  it("falls back to runnable when the list carries no key report", () => {
    const bare: Project = { ...HAPPY, required_keys: undefined, missing_keys: undefined, runnable: false, reason: "secret store x does not exist" };
    expect(projectHealth(bare)).toMatchObject({ health: "missing_secrets", detail: "secret store x does not exist" });
    expect(projectHealth({ ...bare, runnable: undefined }).health).toBe("ok");
  });
});

describe("store health states", () => {
  const ALL = ["GITHUB_TOKEN", "DB_PASSWORD", "DOCS_TOKEN", "TRACKER_TOKEN"];

  it("is ready when the store allows the project and holds every key", () => {
    expect(projectHealth(HAPPY, ALL)).toMatchObject({ health: "ok", tone: "ok", detail: null });
    expect(projectHealth({ ...HAPPY, store_allowed: true })).toMatchObject({ health: "ok" });
  });

  it("reports missing keys by name against the store's live key list", () => {
    expect(projectHealth(HAPPY, ["GITHUB_TOKEN", "DOCS_TOKEN"])).toMatchObject({
      health: "missing_secrets", tone: "warn", label: "2 missing secrets",
      detail: "Missing DB_PASSWORD, TRACKER_TOKEN in store bank-platform.",
    });
  });

  it("reports a store that does not allow the project, not its keys", () => {
    const h = projectHealth(STORE_NOT_ALLOWED);
    expect(h).toMatchObject({ health: "store_not_allowed", tone: "err", label: "store not allowed" });
    expect(h.detail).toBe("Secret store bank-platform is not allowed for this project.");
    // The store's own keys say nothing to a project it does not allow.
    expect(projectHealth(STORE_NOT_ALLOWED, ALL).health).toBe("store_not_allowed");
    expect(projectHealth(STORE_NOT_ALLOWED, []).health).toBe("store_not_allowed");
  });

  it("does not call a store that does not exist not allowed", () => {
    expect(projectHealth({ ...STORE_NOT_ALLOWED, store_exists: false }).health).toBe("missing_secrets");
    expect(projectHealth(STORE_NOT_ALLOWED, null).detail).toBe("Secret store bank-platform does not exist.");
  });

  it("is ready when a store that does not allow the project is not needed", () => {
    const none: Project = { ...STORE_NOT_ALLOWED, required_keys: [], missing_keys: [], runnable: true, reason: null, health: "ok" };
    expect(projectHealth(none).health).toBe("ok");
  });

  it("reports an unreachable store as unreachable with its reason, never as missing keys", () => {
    const h = projectHealth(STORE_UNREACHABLE);
    expect(h).toMatchObject({ health: "store_unreachable", tone: "warn", label: "store unreachable", detail: UNREACHABLE_REASON });
    expect(h.label).not.toMatch(/missing/);
    expect(h.detail).not.toMatch(/missing/i);
    // Without store_error, the reason the API gave.
    expect(projectHealth({ ...STORE_UNREACHABLE, store_error: null }).detail).toBe(UNREACHABLE_REASON);
  });

  it("uses the store's key list once it answers again", () => {
    expect(projectHealth(STORE_UNREACHABLE, ALL).health).toBe("ok");
    expect(projectHealth(STORE_UNREACHABLE, ["GITHUB_TOKEN", "DOCS_TOKEN", "TRACKER_TOKEN"])).toMatchObject({
      health: "missing_secrets", detail: "Missing DB_PASSWORD in store bank-platform.",
    });
  });

  it("orders the states: unreadable, config errors, no store, not allowed, unreachable, missing", () => {
    expect(projectHealth({ ...STORE_NOT_ALLOWED, commit: null }).health).toBe("unreadable");
    expect(projectHealth({ ...STORE_UNREACHABLE, errors: ["agents: required"] }).health).toBe("config_errors");
    expect(projectHealth({ ...STORE_UNREACHABLE, store_allowed: false }).health).toBe("store_not_allowed");
    expect(projectHealth({ ...STORE_UNREACHABLE, store_exists: false }).health).toBe("missing_secrets");
  });
});

describe("snapshot reading", () => {
  it("accepts required keys as names, {name, present} or {key, present}", () => {
    expect(requiredKeys({ ...HAPPY, required_keys: [{ key: "A", present: false }] })).toEqual([{ key: "A", present: false }]);
    const names: Project = { ...HAPPY, required_keys: undefined, missing_keys: ["DOCS_TOKEN"] };
    expect(requiredKeys(names).filter((k) => !k.present).map((k) => k.key)).toEqual(["DOCS_TOKEN"]);
    expect(requiredKeys(MISSING_KEYS).find((k) => k.key === "DB_PASSWORD")?.present).toBe(false);
  });

  it("reads maps keyed by name and lists alike", () => {
    expect(agentsOf(HAPPY.config!).map(([s]) => s)).toEqual(["developer", "scout"]);
    expect(agentSlugsOf({ ...HAPPY, config: null })).toEqual(["developer", "scout"]);
    expect(entries([{ name: "redis" }, { slug: "a" }]).map(([n]) => n)).toEqual(["redis", "a"]);
    expect(servicesOf(null)).toEqual([]);
  });

  it("reads the nested snapshot of older APIs", () => {
    expect(projectHealth(WITH_ERRORS).health).toBe("config_errors");
    expect(storeNameOf({ id: "x", slug: "x", config_url: "u", snapshot: { commit: "c", store: "s" } })).toBe("s");
  });

  it("finds the store and shortens commits", () => {
    expect(storeNameOf(HAPPY)).toBe("bank-platform");
    expect(storeNameOf(WITH_ERRORS)).toBeNull();
    expect(shortCommit("3f9c2a1b7e4d")).toBe("3f9c2a1");
    expect(shortCommit(null)).toBe("");
  });
});
