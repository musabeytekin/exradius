// `${secret:NAME}` references in connection env and header values (architecture §12,
// "Configuration resolution"). The connection form resolves them against existing
// secrets: global ones resolve everywhere, project ones only where the agent is attached.

import { ENV_KEY_RE } from "./slug";
import type { Secret } from "./types";

/** Anything that looks like a reference, including malformed ones, so typos surface. */
const LOOSE_RE = /\$\{\s*secrets?\s*:([^}]*)\}/g;
/** An unterminated reference: `${secret:NAME` with no closing brace. */
const UNTERMINATED_RE = /\$\{\s*secrets?\s*:[^}]*$/;

export type RefStatus = "global" | "project" | "unknown" | "invalid";

export interface SecretRef {
  /** The text as written, e.g. `${secret:GITHUB_TOKEN}`. */
  raw: string;
  /** The referenced name, trimmed. */
  name: string;
  status: RefStatus;
  /** For `project`: slugs of the projects that define a secret with this name. */
  projects: string[];
  /** Human explanation for the field hint. */
  message: string;
}

export interface SecretIndex {
  global: Set<string>;
  /** Secret name to the slugs of projects that define it. */
  project: Map<string, string[]>;
}

export function buildSecretIndex(
  globals: Secret[],
  perProject: { slug: string; secrets: Secret[] }[] = [],
): SecretIndex {
  const project = new Map<string, string[]>();
  for (const { slug, secrets } of perProject) {
    for (const s of secrets) project.set(s.key, [...(project.get(s.key) ?? []), slug]);
  }
  return { global: new Set(globals.map((s) => s.key)), project };
}

export function formatRef(name: string): string {
  return `\${secret:${name}}`;
}

/** Every reference in one value, in order of appearance. */
export function findRefs(value: string, index: SecretIndex): SecretRef[] {
  const refs: SecretRef[] = [];
  for (const m of value.matchAll(LOOSE_RE)) {
    const raw = m[0];
    const name = m[1].trim();
    const wellFormed = raw.startsWith("${secret:") && m[1] === name && ENV_KEY_RE.test(name);
    if (!wellFormed) {
      refs.push({
        raw, name, status: "invalid", projects: [],
        message: name
          ? `${raw} is malformed; write it as ${formatRef(name.replace(/\W/g, "_") || "NAME")}`
          : `${raw} names no secret`,
      });
      continue;
    }
    if (index.global.has(name)) {
      refs.push({ raw, name, status: "global", projects: [], message: `${name} resolves to the global secret` });
    } else if (index.project.has(name)) {
      const projects = index.project.get(name)!;
      refs.push({
        raw, name, status: "project", projects,
        message: `${name} is not global; it resolves only in ${projects.join(", ")}`,
      });
    } else {
      refs.push({ raw, name, status: "unknown", projects: [], message: `No secret named ${name} exists` });
    }
  }
  if (UNTERMINATED_RE.test(value)) {
    refs.push({ raw: value.slice(value.lastIndexOf("${")), name: "", status: "invalid", projects: [], message: "Unterminated reference: missing }" });
  }
  return refs;
}

export interface FieldValidation {
  key: string;
  refs: SecretRef[];
  /** Worst status of the field: invalid > unknown > project > global, or null when there are no references. */
  worst: RefStatus | null;
}

const RANK: Record<RefStatus, number> = { global: 0, project: 1, unknown: 2, invalid: 3 };

export function worstOf(refs: SecretRef[]): RefStatus | null {
  return refs.reduce<RefStatus | null>((w, r) => (w === null || RANK[r.status] > RANK[w] ? r.status : w), null);
}

/** Validate every value of an env or headers map. */
export function validateFields(fields: Record<string, string>, index: SecretIndex): FieldValidation[] {
  return Object.entries(fields).map(([key, value]) => {
    const refs = findRefs(value, index);
    return { key, refs, worst: worstOf(refs) };
  });
}

/** Summary for the form footer: blocking problems (invalid) and warnings (unknown, project-only). */
export function summarize(validations: FieldValidation[]) {
  const all = validations.flatMap((v) => v.refs);
  return {
    invalid: all.filter((r) => r.status === "invalid"),
    unknown: [...new Set(all.filter((r) => r.status === "unknown").map((r) => r.name))],
    projectOnly: [...new Set(all.filter((r) => r.status === "project").map((r) => r.name))],
    names: [...new Set(all.filter((r) => r.status !== "invalid").map((r) => r.name))],
  };
}

/** Insert a reference at the caret position of a value. */
export function insertRef(value: string, name: string, caret: number = value.length): { value: string; caret: number } {
  const ref = formatRef(name);
  const at = Math.max(0, Math.min(caret, value.length));
  return { value: value.slice(0, at) + ref + value.slice(at), caret: at + ref.length };
}
