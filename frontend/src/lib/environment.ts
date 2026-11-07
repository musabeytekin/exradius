/** The `environment_text` of a run or a resolved view, when the API returns one. */
export function environmentTextOf(v: unknown): string | null {
  if (!v || typeof v !== "object") return null;
  const o = v as { environment_text?: unknown; spec?: { environment_text?: unknown } | null };
  const t = o.environment_text ?? o.spec?.environment_text;
  return typeof t === "string" && t.trim() ? t : null;
}
