// The agent's model (docs/environment-and-services.md §3): "Runtime default", the provider's
// models, or "Other" with a free id.

import type { ProviderKindSpec, ProviderModel } from "./types";

/** Used when an API's provider catalog lists no models for the anthropic kind. */
export const ANTHROPIC_MODELS: ProviderModel[] = [
  { id: "claude-opus-5-5", label: "Claude Opus 5.5", description: "Most capable, for long and difficult tasks" },
  { id: "claude-sonnet-5-5", label: "Claude Sonnet 5.5", description: "Balanced speed and capability" },
  { id: "claude-haiku-5-5", label: "Claude Haiku 5.5", description: "Fastest and cheapest, for simple tasks" },
];

export const RUNTIME_DEFAULT = "__default__";
export const OTHER = "__other__";

/** The models a provider kind offers: the API's list when it sends one, else the static fallback. */
export function modelsFor(kind: Pick<ProviderKindSpec, "kind" | "models"> | undefined, kindName?: string | null): ProviderModel[] {
  const listed = kind?.models;
  if (Array.isArray(listed) && listed.length) {
    return listed
      .map((m) => (typeof m === "string" ? { id: m } : m))
      .filter((m): m is ProviderModel => !!m && typeof m.id === "string" && !!m.id);
  }
  return (kind?.kind ?? kindName) === "anthropic" ? ANTHROPIC_MODELS : [];
}

/** Which option the select shows for a stored model. An id the list does not know is "Other". */
export function modelSelection(model: string | null | undefined, models: ProviderModel[]): string {
  if (!model) return RUNTIME_DEFAULT;
  return models.some((m) => m.id === model) ? model : OTHER;
}

/** What is saved: null for the runtime default, the picked id, or the typed one. */
export function modelValue(selection: string, other: string): string | null {
  if (selection === RUNTIME_DEFAULT) return null;
  if (selection === OTHER) return other.trim() || null;
  return selection;
}

/** The label for a model id: its catalog label when known, else the id. */
export function modelLabel(model: string | null | undefined, models: ProviderModel[] = ANTHROPIC_MODELS): string {
  if (!model) return "Runtime default";
  return models.find((m) => m.id === model)?.label || model;
}
