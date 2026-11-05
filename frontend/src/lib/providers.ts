// The provider form is built from what runtimes declare (docs/providers.md §2.1); nothing
// about kinds or auth methods is known here.

import type { AuthMethodSpec, Provider, ProviderKindSpec, ProviderLastTest, ProviderSettingSpec, Runtime } from "./types";

/** Every kind any runtime accepts, merged by kind; auth methods merged by name, first wins. */
export function providerKinds(runtimes: Runtime[] | undefined): ProviderKindSpec[] {
  const byKind = new Map<string, ProviderKindSpec>();
  for (const r of runtimes ?? []) {
    for (const k of r.providers ?? []) {
      const have = byKind.get(k.kind);
      if (!have) {
        byKind.set(k.kind, { ...k, auth_methods: [...k.auth_methods] });
        continue;
      }
      for (const m of k.auth_methods) {
        if (!have.auth_methods.some((x) => x.name === m.name)) have.auth_methods.push(m);
      }
    }
  }
  return [...byKind.values()];
}

export interface ProviderFormModel {
  kinds: { value: string; label: string }[];
  kind: ProviderKindSpec | null;
  authMethods: { value: string; label: string }[];
  method: AuthMethodSpec | null;
  credentialLabel: string;
  settings: ProviderSettingSpec[];
  notice: string | null;
}

/**
 * What the form shows for a chosen kind and auth method. An unknown or missing choice falls
 * back to the first one offered, so the form always has a valid selection when any exists.
 */
export function providerFormModel(kinds: ProviderKindSpec[], kindName: string | null, methodName: string | null): ProviderFormModel {
  const kind = kinds.find((k) => k.kind === kindName) ?? kinds[0] ?? null;
  const method = kind?.auth_methods.find((m) => m.name === methodName) ?? kind?.auth_methods[0] ?? null;
  return {
    kinds: kinds.map((k) => ({ value: k.kind, label: k.label })),
    kind,
    authMethods: (kind?.auth_methods ?? []).map((m) => ({ value: m.name, label: m.label })),
    method,
    credentialLabel: method?.credential_label || "Credential",
    settings: method?.settings ?? [],
    notice: method?.notice || null,
  };
}

/** Settings values for a method: keep what was typed for fields it still has, fill defaults. */
export function settingsFor(method: AuthMethodSpec | null, current: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const f of method?.settings ?? []) out[f.name] = current[f.name] ?? f.default ?? "";
  return out;
}

export function missingSettings(method: AuthMethodSpec | null, values: Record<string, string>): string[] {
  return (method?.settings ?? []).filter((f) => f.required && !values[f.name]?.trim()).map((f) => f.name);
}

export function settingLabel(f: ProviderSettingSpec): string {
  return f.label || f.name.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
}

/** Labels for a stored provider; falls back to the raw names when no runtime declares them. */
export function describeProvider(kinds: ProviderKindSpec[], p: Pick<Provider, "kind" | "auth_method">) {
  const kind = kinds.find((k) => k.kind === p.kind);
  const method = kind?.auth_methods.find((m) => m.name === p.auth_method);
  return { kindLabel: kind?.label ?? p.kind, methodLabel: method?.label ?? p.auth_method, shortMethod: method?.credential_label ?? p.auth_method };
}

/** `detail` of a test: a message, a list of model ids, or anything else the backend stores. */
export function testDetail(detail: unknown): string | null {
  if (detail === null || detail === undefined || detail === "") return null;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(String).join(", ");
  if (typeof detail === "object") {
    const o = detail as Record<string, unknown>;
    if (Array.isArray(o.models)) return o.models.map(String).join(", ");
    if (typeof o.message === "string") return o.message;
  }
  return JSON.stringify(detail);
}

export type TestTone = "ok" | "err" | "warn";
export function testTone(t: ProviderLastTest | null | undefined): TestTone {
  return !t ? "warn" : t.ok ? "ok" : "err";
}

/** Does a runtime accept this provider's kind and auth method? Runtimes that declare nothing accept all. */
export function runtimeAccepts(runtime: Runtime | undefined, p: Pick<Provider, "kind" | "auth_method">): boolean {
  if (!runtime?.providers) return true;
  return runtime.providers.some((k) => k.kind === p.kind && k.auth_methods.some((m) => m.name === p.auth_method));
}
