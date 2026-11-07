import { describe, expect, it } from "vitest";
import { SERVICE_CATALOG, secretLabel } from "./catalog";
import {
  SERVICE_ENV_KEY_RE, catalogEntryForImage, draftFromCatalog, draftFromService, draftToBody, generatePassword, parsePorts, readyLabel,
  serviceExample, splitCommand, uniqueServiceName,
} from "./services";
import type { ProjectService } from "./types";

const entry = (key: string) => SERVICE_CATALOG.find((e) => e.key === key)!;

describe("services catalog prefill", () => {
  it("covers the agreed services with valid names and readiness", () => {
    expect(SERVICE_CATALOG.map((e) => e.key)).toEqual(["postgres", "mysql", "redis", "rabbitmq", "mongodb", "elasticsearch", "mailpit", "minio"]);
    for (const e of SERVICE_CATALOG) {
      expect(e.key).toMatch(/^[a-z][a-z0-9-]*$/);
      expect(e.ports.length).toBeGreaterThan(0);
      if (e.key === "postgres" || e.key === "mysql") expect(e.ready).toHaveProperty("command");
      else expect(e.ready).toEqual({ tcp: e.ports[0] });
      for (const f of e.env.filter((x) => x.secret)) expect(f.label, `${e.key}.${f.key}`).toBeTruthy();
      // The API takes service env keys as EnvKey.
      for (const f of e.env) expect(f.key, `${e.key}.${f.key}`).toMatch(SERVICE_ENV_KEY_RE);
    }
  });

  it("prefills Postgres with defaults and a labelled password stored as a project secret", () => {
    const d = draftFromCatalog(entry("postgres"));
    expect(d).toMatchObject({ name: "postgres", image: "postgres:16", ports: [5432] });
    expect(d.ready).toEqual({ command: ["pg_isready", "-h", "127.0.0.1", "-U", "app", "-d", "app"] });
    expect(d.env).toEqual([{ key: "POSTGRES_USER", value: "app" }, { key: "POSTGRES_DB", value: "app" }]);
    expect(d.credentials).toMatchObject([{ key: "POSTGRES_PASSWORD", secret: "DB_PASSWORD", label: "Database password" }]);
    expect(draftToBody(d).env).toEqual({ POSTGRES_USER: "app", POSTGRES_DB: "app", POSTGRES_PASSWORD: "${secret:DB_PASSWORD}" });
    expect(secretLabel("DB_PASSWORD")).toBe("Database password");
  });

  it("prefills Redis with a port check and no env; custom image starts empty", () => {
    expect(draftFromCatalog(entry("redis"))).toMatchObject({ image: "redis:7", env: [], ports: [6379], ready: { tcp: 6379 }, credentials: [] });
    expect(draftFromCatalog(null)).toEqual({ name: "", image: "", env: [], ports: [], ready: null, credentials: [] });
  });

  it("does not share the catalog's arrays with a draft", () => {
    const d = draftFromCatalog(entry("postgres"));
    d.ports.push(1);
    (d.ready as { command: string[] }).command.push("x");
    expect(entry("postgres").ports).toEqual([5432]);
    expect((entry("postgres").ready as { command: string[] }).command).not.toContain("x");
  });

  it("picks a free name when the project has one already", () => {
    expect(uniqueServiceName("postgres", ["postgres", "postgres-2"])).toBe("postgres-3");
    expect(draftFromCatalog(entry("redis"), ["redis"]).name).toBe("redis-2");
  });

  it("turns a saved catalog service back into a draft with its credential field", () => {
    const s: ProjectService = {
      id: "s1", project_id: "p1", name: "db", image: "docker.io/library/postgres:15", position: 0, ports: [5432], ready: { tcp: 5432 },
      env: { POSTGRES_USER: "bank", POSTGRES_PASSWORD: "${secret:DB_PASSWORD}", EXTRA: "1" },
    };
    expect(catalogEntryForImage(s.image)?.key).toBe("postgres");
    const d = draftFromService(s);
    expect(d.credentials.map((f) => f.key)).toEqual(["POSTGRES_PASSWORD"]);
    expect(d.env).toEqual([{ key: "POSTGRES_USER", value: "bank" }, { key: "EXTRA", value: "1" }]);
    expect(draftToBody(d).env).toEqual(s.env);
  });

  it("builds the example env var from the service's own values", () => {
    const s = { name: "db", image: "postgres:16", ports: [5432], env: { POSTGRES_USER: "app", POSTGRES_DB: "app", POSTGRES_PASSWORD: "${secret:DB_PASSWORD}" } };
    expect(serviceExample(s)).toEqual({ key: "DATABASE_URL", value: "postgres://app:${secret:DB_PASSWORD}@db:5432/app" });
    expect(serviceExample({ name: "cache", image: "redis:7", ports: [], env: {} })).toEqual({ key: "REDIS_URL", value: "redis://cache:6379" });
    expect(serviceExample({ name: "x", image: "acme/thing:1", ports: [80], env: {} })).toBeNull();
  });

  it("parses ports, commands and readiness labels", () => {
    expect(parsePorts("5432, 15672 5432")).toEqual([5432, 15672]);
    expect(parsePorts("")).toEqual([]);
    expect(parsePorts("80, http")).toBeNull();
    expect(parsePorts("70000")).toBeNull();
    expect(splitCommand(`sh -c "redis-cli ping"`)).toEqual(["sh", "-c", "redis-cli ping"]);
    expect(readyLabel(null)).toBe("image healthcheck");
    expect(readyLabel({ tcp: 6379 })).toBe("port 6379 open");
    expect(readyLabel({ command: ["sh", "-c", "redis-cli ping"] })).toBe(`sh -c "redis-cli ping"`);
  });

  it("generates URL-safe passwords", () => {
    const p = generatePassword();
    expect(p).toMatch(/^[A-Za-z0-9]{24}$/);
    expect(generatePassword()).not.toBe(p);
  });
});
