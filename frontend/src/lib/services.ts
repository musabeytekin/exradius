// Project services (docs/environment-and-services.md §2): pure helpers for the Services tab.

import { SERVICE_CATALOG, fieldValue, type CatalogField, type ServiceCatalogEntry } from "./catalog";
import type { ProjectService, ProjectServiceIn, ServiceReady } from "./types";

/** Host name on the run network. */
export const SERVICE_NAME_RE = /^[a-z][a-z0-9-]*$/;
/** The API's EnvKey, as for project variables. */
export { ENV_KEY_RE as SERVICE_ENV_KEY_RE } from "./slug";

/** A catalog password field: asked for by label, stored as the project secret `secret`. */
export type ServiceCredential = CatalogField & { secret: string; label: string };

export interface ServiceDraft {
  name: string;
  image: string;
  /** Plain env entries; credential fields are kept apart. */
  env: { key: string; value: string }[];
  ports: number[];
  ready: ServiceReady;
  credentials: ServiceCredential[];
}

export function serviceCredentials(e: ServiceCatalogEntry): ServiceCredential[] {
  return e.env.filter((f): f is CatalogField & { secret: string } => !!f.secret).map((f) => ({ ...f, label: f.label ?? f.secret }));
}

/** `base`, or `base-2`, `base-3` … when a service of the project already uses it. */
export function uniqueServiceName(base: string, taken: string[]): string {
  if (!taken.includes(base)) return base;
  for (let i = 2; ; i++) if (!taken.includes(`${base}-${i}`)) return `${base}-${i}`;
}

/** The form's starting point for a catalog entry, or an empty one for a custom image. */
export function draftFromCatalog(entry: ServiceCatalogEntry | null, taken: string[] = []): ServiceDraft {
  if (!entry) return { name: "", image: "", env: [], ports: [], ready: null, credentials: [] };
  return {
    name: uniqueServiceName(entry.key, taken),
    image: entry.image,
    env: entry.env.filter((f) => !f.secret).map((f) => ({ key: f.key, value: f.value ?? "" })),
    ports: [...entry.ports],
    ready: entry.ready && ("command" in entry.ready ? { command: [...entry.ready.command] } : { ...entry.ready }),
    credentials: serviceCredentials(entry),
  };
}

/** The catalog entry an image comes from, by repository (the part before the tag). */
export function catalogEntryForImage(image: string): ServiceCatalogEntry | undefined {
  const repo = (s: string) => s.trim().replace(/@.*$/, "").replace(/:[^/:]*$/, "").replace(/^docker\.io\/(library\/)?/, "");
  const r = repo(image);
  return SERVICE_CATALOG.find((e) => repo(e.image) === r);
}

/** An existing service as a draft: env values that reference a catalog password become credential fields again. */
export function draftFromService(s: ProjectService): ServiceDraft {
  const entry = catalogEntryForImage(s.image);
  const credentials = (entry ? serviceCredentials(entry) : []).filter((f) => s.env[f.key] === fieldValue(f));
  const credKeys = new Set(credentials.map((f) => f.key));
  return {
    name: s.name,
    image: s.image,
    env: Object.entries(s.env ?? {}).filter(([k]) => !credKeys.has(k)).map(([key, value]) => ({ key, value })),
    ports: [...(s.ports ?? [])],
    ready: s.ready ?? null,
    credentials,
  };
}

/** The request body: plain env plus `${secret:NAME}` for each credential field. */
export function draftToBody(d: ServiceDraft): ProjectServiceIn {
  const env: Record<string, string> = {};
  for (const r of d.env) if (r.key.trim()) env[r.key.trim()] = r.value;
  for (const f of d.credentials) env[f.key] = fieldValue(f);
  return { name: d.name.trim(), image: d.image.trim(), env, ports: d.ports, ready: d.ready };
}

/** "5432, 15672" → [5432, 15672]; null when something is not a port. */
export function parsePorts(text: string): number[] | null {
  const parts = text.split(/[\s,]+/).filter(Boolean);
  const ports = parts.map(Number);
  if (ports.some((p) => !Number.isInteger(p) || p < 1 || p > 65535)) return null;
  return [...new Set(ports)];
}

/** Shell-like split for a readiness command: whitespace separates, quotes group. */
export function splitCommand(text: string): string[] {
  const out: string[] = [];
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g;
  for (let m = re.exec(text); m; m = re.exec(text)) out.push(m[1] ?? m[2] ?? m[3]);
  return out;
}

export function joinCommand(args: string[]): string {
  return args.map((a) => (/[\s"']/.test(a) || a === "" ? `"${a}"` : a)).join(" ");
}

export type ReadyMode = "image" | "tcp" | "command";
export const readyMode = (r: ServiceReady | undefined): ReadyMode => (!r ? "image" : "tcp" in r ? "tcp" : "command");

/** One line for the row: what the worker waits for. */
export function readyLabel(r: ServiceReady | undefined): string {
  if (!r) return "image healthcheck";
  if ("tcp" in r) return `port ${r.tcp} open`;
  return joinCommand(r.command);
}

/** A project env var that points at the service, from its catalog entry; null for custom images. */
export function serviceExample(s: Pick<ProjectService, "name" | "image" | "env" | "ports">): { key: string; value: string } | null {
  const entry = catalogEntryForImage(s.image);
  if (!entry?.example) return null;
  const defaults = Object.fromEntries(entry.env.map((f) => [f.key, fieldValue(f)]));
  const port = s.ports?.[0] ?? entry.ports[0];
  const value = entry.example.value.replace(/\{([^}]+)\}/g, (_, k: string) =>
    k === "host" ? s.name : k === "port" ? String(port) : s.env?.[k] ?? defaults[k] ?? "",
  );
  return { key: entry.example.key, value };
}

/** A password for a throwaway service: letters and digits only, so it is safe inside URLs. */
export function generatePassword(n = 24): string {
  const alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789";
  return Array.from(crypto.getRandomValues(new Uint8Array(n)), (b) => alphabet[b % alphabet.length]).join("");
}

/** The verify step for a service, by its name ("Start service <name>"). */
export const startStepName = (name: string) => `Start service ${name}`;
