import { describe, expect, it } from "vitest";
import { HAPPY, MISSING_KEYS, UNREADABLE, WITH_ERRORS } from "@/test/projectFixtures";
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
