// Reading a project's config snapshot (docs/config-repo.md §3, §11) defensively: every field
// may be missing on an older or partial API, and maps may arrive keyed by name or as lists.
import type { Tone } from "@/components/ui/primitives";
import type {
  CatalogConfigKey, CatalogEntry, ConfigAgent, ConfigConnection, ConfigService, Project, ProjectConfig,
  ProjectConfigSnapshot, RequiredKey, RequiredKeyIn, SecretStoreKey,
} from "./types";

export const STORE_NAME_RE = /^[a-z][a-z0-9-]*$/;
export const SECRET_KEY_RE = /^[A-Za-z0-9_.-]+$/;

export const snapshotOf = (p: Project | null | undefined): ProjectConfigSnapshot => p?.snapshot ?? {};
export const configOf = (p: Project | null | undefined): ProjectConfig | null => p?.config ?? p?.snapshot?.config ?? null;

/** `[name, item]` pairs from a map keyed by name, or from a list whose items carry `name`/`slug`. */
export function entries<T extends { name?: string | null; slug?: string | null }>(
  m: Record<string, T> | T[] | null | undefined,
): [string, T][] {
  if (!m) return [];
  if (Array.isArray(m)) return m.map((x, i) => [x.slug ?? x.name ?? `#${i + 1}`, x]);
  return Object.entries(m);
}

export const servicesOf = (c: ProjectConfig | null) => entries<ConfigService>(c?.services);
export const connectionsOf = (c: ProjectConfig | null) => entries<ConfigConnection>(c?.connections);
export const agentsOf = (c: ProjectConfig | null): [string, ConfigAgent][] => entries<ConfigAgent>(c?.agents);

export function projectName(p: Project): string {
  return p.name || configOf(p)?.project?.name || p.slug;
}

export function shortCommit(c: string | null | undefined): string {
  return c ? c.slice(0, 7) : "";
}

export const commitOf = (p: Project) => p.commit ?? snapshotOf(p).commit ?? p.snapshot_commit ?? null;
export const readAtOf = (p: Project) => p.read_at ?? snapshotOf(p).read_at ?? p.snapshot_read_at ?? null;
export const errorsOf = (p: Project): string[] => (p.errors?.length ? p.errors : snapshotOf(p).errors ?? []);
export const warningsOf = (p: Project): string[] => (p.warnings?.length ? p.warnings : snapshotOf(p).warnings ?? []);
export const readErrorOf = (p: Project): string | null => p.read_error ?? snapshotOf(p).read_error ?? null;
export const rawOf = (p: Project): string | null => {
  const r = p.raw ?? snapshotOf(p).raw;
  return typeof r === "string" && r.length ? r : null;
};

/** The store the file names. */
export function storeNameOf(p: Project): string | null {
  return p.store ?? snapshotOf(p).store ?? configOf(p)?.secret_store ?? null;
}

export function keyName(k: string | SecretStoreKey | RequiredKeyIn): string {
  return typeof k === "string" ? k : k.key ?? (k as RequiredKeyIn).name ?? "";
}

/**
 * Required keys with presence. `storeKeys` is the store's key list when known, `null` when the
 * store does not exist, `undefined` to rely on what the API reported.
 */
export function requiredKeys(p: Project, storeKeys?: string[] | null): RequiredKey[] {
  const raw = p.required_keys ?? snapshotOf(p).required_keys ?? [];
  const missing = new Set(p.missing_keys ?? []);
  const seen = new Set<string>();
  const out: RequiredKey[] = [];
  for (const k of raw) {
    const key = keyName(k);
    if (!key || seen.has(key)) continue;
    seen.add(key);
    const present =
      storeKeys === null ? false
        : storeKeys ? storeKeys.includes(key)
          : typeof k === "object" ? !!k.present
            : p.missing_keys ? !missing.has(key)
              : p.store_exists === false ? false : true;
    out.push({ key, present });
  }
  // Keys the API calls missing that the required list left out.
  for (const key of missing) {
    if (!seen.has(key) && storeKeys === undefined) out.push({ key, present: false });
  }
  return out;
}

export function missingKeys(p: Project, storeKeys?: string[] | null): string[] {
  return requiredKeys(p, storeKeys).filter((k) => !k.present).map((k) => k.key);
}

export type Health = "ok" | "config_errors" | "missing_secrets" | "unreadable";
export interface HealthInfo { health: Health; label: string; tone: Tone; detail: string | null }

export function projectHealth(p: Project, storeKeys?: string[] | null): HealthInfo {
  const readError = readErrorOf(p);
  if (readError || !commitOf(p)) {
    const detail = readError ?? errorsOf(p)[0] ?? "The config repository has not been read.";
    return { health: "unreadable", label: "unreadable", tone: "err", detail };
  }
  const errors = errorsOf(p);
  if (errors.length) {
    return { health: "config_errors", label: errors.length === 1 ? "1 config error" : `${errors.length} config errors`, tone: "err", detail: errors[0] };
  }
  const store = storeNameOf(p);
  const missing = missingKeys(p, storeKeys);
  const storeMissing = storeKeys === null || (storeKeys === undefined && p.store_exists === false);
  if (storeMissing && store && requiredKeys(p, storeKeys).length) {
    return { health: "missing_secrets", label: "missing secrets", tone: "warn", detail: `Secret store ${store} does not exist.` };
  }
  if (missing.length) {
    return {
      health: "missing_secrets",
      label: missing.length === 1 ? "1 missing secret" : `${missing.length} missing secrets`,
      tone: "warn",
      detail: `Missing ${missing.join(", ")}${store ? ` in store ${store}` : ""}.`,
    };
  }
  // Only when nothing fresher is known: the flag goes stale as soon as a key is set.
  if (p.runnable === false && storeKeys === undefined) {
    return { health: "missing_secrets", label: "missing secrets", tone: "warn", detail: p.reason ?? "The API reports the project is not runnable." };
  }
  return { health: "ok", label: "ok", tone: "ok", detail: null };
}

/** The config keys a catalog entry declares, by key, split into required and optional. */
export function catalogKeyNames(e: CatalogEntry | undefined): { required: string[]; optional: string[] } {
  const keys = (e?.config ?? []).filter((k) => k.key || k.name);
  const name = (k: CatalogConfigKey) => (k.key ?? k.name)!;
  return { required: keys.filter((k) => k.required).map(name), optional: keys.filter((k) => !k.required).map(name) };
}

/** Agent slugs: from the parsed config, else the API's list. */
export function agentSlugsOf(p: Project | null | undefined): string[] {
  const fromConfig = agentsOf(configOf(p)).map(([slug]) => slug);
  return fromConfig.length ? fromConfig : p?.agents ?? [];
}

export function readyLabel(r: ConfigService["ready"]): string {
  if (!r) return "image healthcheck or running";
  if (r.tcp) return `tcp ${r.tcp}`;
  if (r.command?.length) return r.command.join(" ");
  return "image healthcheck or running";
}
