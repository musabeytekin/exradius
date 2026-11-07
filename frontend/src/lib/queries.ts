import { QueryClient, useQuery } from "@tanstack/react-query";
import { api } from "./api";

export const qk = {
  me: ["me"] as const,
  status: ["system", "status"] as const,
  runtimes: ["system", "runtimes"] as const,
  integration: ["system", "integration"] as const,
  projects: ["projects"] as const,
  project: (id: string) => ["projects", id] as const,
  verifications: (pid: string) => ["projects", pid, "verifications"] as const,
  catalog: ["catalog"] as const,
  stores: ["stores"] as const,
  storeKeys: (id: string) => ["stores", id, "keys"] as const,
  gitCredentials: ["git-credentials"] as const,
  providers: ["providers"] as const,
  tasks: (filters: Record<string, unknown>) => ["tasks", filters] as const,
  allTasks: ["tasks"] as const,
  task: (id: string) => ["task", id] as const,
  transcript: (id: string) => ["task", id, "transcript"] as const,
  events: ["events"] as const,
};

export function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        retry: (count, err) => {
          const status = (err as { status?: number }).status;
          if (status && status >= 400 && status < 500) return false;
          return count < 2;
        },
        refetchOnWindowFocus: true,
      },
    },
  });
}

export const useStatus = () => useQuery({ queryKey: qk.status, queryFn: api.system.status, refetchInterval: 15_000 });
export const useRuntimes = () => useQuery({ queryKey: qk.runtimes, queryFn: api.system.runtimes, staleTime: Infinity });
export const useIntegration = () => useQuery({ queryKey: qk.integration, queryFn: api.system.integration, staleTime: 60_000 });
// A verification in progress (one starts by itself when a project is added or a new commit is read).
const VERIFYING_POLL = 3000;
export const useProjects = () => useQuery({
  queryKey: qk.projects, queryFn: api.projects.list,
  refetchInterval: (q) => (q.state.data?.some((p) => p.freshness === "running") ? VERIFYING_POLL : false),
});
export const useProject = (id: string) => useQuery({
  queryKey: qk.project(id), queryFn: () => api.projects.get(id), enabled: !!id,
  refetchInterval: (q) => (q.state.data?.freshness === "running" ? VERIFYING_POLL : false),
});
export const useCatalog = () => useQuery({ queryKey: qk.catalog, queryFn: api.catalog.list, staleTime: 5 * 60_000 });
export const useStores = () => useQuery({ queryKey: qk.stores, queryFn: api.stores.list });
export const useStoreKeys = (id: string | null | undefined) =>
  useQuery({ queryKey: qk.storeKeys(id ?? "-"), queryFn: () => api.stores.keys(id!), enabled: !!id });
export const useGitCredentials = () => useQuery({ queryKey: qk.gitCredentials, queryFn: api.gitCredentials.list });
export const useProviders = () => useQuery({ queryKey: qk.providers, queryFn: api.providers.list });
