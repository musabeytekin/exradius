import { api } from "./api";
import type { Secret } from "./types";

/**
 * Store a labelled credential under an internal name: create it, or replace the value of
 * the existing secret with that name in the same scope.
 */
export async function storeCredential(pid: string | null, name: string, value: string, existing: Secret[]): Promise<Secret> {
  const found = existing.find((s) => s.key === name);
  return found ? api.secrets.replace(pid, found.id, value) : api.secrets.create(pid, { key: name, value });
}
