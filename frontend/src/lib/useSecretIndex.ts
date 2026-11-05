import { useQueries } from "@tanstack/react-query";
import { api } from "./api";
import { qk, useGlobalSecrets, useProjects } from "./queries";
import { buildSecretIndex, type SecretIndex } from "./secretRefs";
import type { Secret } from "./types";

/** Global secrets plus every project's secrets, for reference validation. */
export function useSecretIndex(): { index: SecretIndex; globals: Secret[]; loading: boolean } {
  const globals = useGlobalSecrets();
  const projects = useProjects();
  const perProject = useQueries({
    queries: (projects.data ?? []).map((p) => ({
      queryKey: qk.projectSecrets(p.id),
      queryFn: () => api.secrets.listProject(p.id),
    })),
  });
  const index = buildSecretIndex(
    globals.data ?? [],
    (projects.data ?? []).map((p, i) => ({ slug: p.slug, secrets: perProject[i]?.data ?? [] })),
  );
  return { index, globals: globals.data ?? [], loading: globals.isLoading || projects.isLoading };
}
