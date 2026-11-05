import { QueryClient, useQuery } from "@tanstack/react-query";
import { api } from "./api";

export const qk = {
  me: ["me"] as const,
  status: ["system", "status"] as const,
  images: ["system", "images"] as const,
  runtimes: ["system", "runtimes"] as const,
  integration: ["system", "integration"] as const,
  projects: ["projects"] as const,
  project: (id: string) => ["projects", id] as const,
  repositories: (pid: string) => ["projects", pid, "repositories"] as const,
  envVars: (pid: string) => ["projects", pid, "env-vars"] as const,
  projectSecrets: (pid: string) => ["projects", pid, "secrets"] as const,
  globalSecrets: ["secrets"] as const,
  projectAgents: (pid: string) => ["projects", pid, "agents"] as const,
  resolved: (pid: string, aid: string) => ["projects", pid, "agents", aid, "resolved"] as const,
  verifications: (pid: string) => ["projects", pid, "verifications"] as const,
  connections: ["connections"] as const,
  agents: ["agents"] as const,
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
export const useImages = () => useQuery({ queryKey: qk.images, queryFn: api.system.images, staleTime: 60_000 });
export const useRuntimes = () => useQuery({ queryKey: qk.runtimes, queryFn: api.system.runtimes, staleTime: Infinity });
export const useIntegration = () => useQuery({ queryKey: qk.integration, queryFn: api.system.integration, staleTime: 60_000 });
export const useProjects = () => useQuery({ queryKey: qk.projects, queryFn: api.projects.list });
export const useProject = (id: string) => useQuery({ queryKey: qk.project(id), queryFn: () => api.projects.get(id) });
export const useRepositories = (pid: string) =>
  useQuery({ queryKey: qk.repositories(pid), queryFn: () => api.repositories.list(pid) });
export const useEnvVars = (pid: string) => useQuery({ queryKey: qk.envVars(pid), queryFn: () => api.envVars.list(pid) });
export const useProjectSecrets = (pid: string | null | undefined) =>
  useQuery({
    queryKey: qk.projectSecrets(pid ?? "-"),
    queryFn: () => api.secrets.listProject(pid!),
    enabled: !!pid,
  });
export const useGlobalSecrets = () => useQuery({ queryKey: qk.globalSecrets, queryFn: api.secrets.listGlobal });
export const useProjectAgents = (pid: string | null | undefined) =>
  useQuery({ queryKey: qk.projectAgents(pid ?? "-"), queryFn: () => api.projectAgents.list(pid!), enabled: !!pid });
export const useConnections = () => useQuery({ queryKey: qk.connections, queryFn: api.connections.list });
export const useAgents = () => useQuery({ queryKey: qk.agents, queryFn: api.agents.list });
export const useProviders = () => useQuery({ queryKey: qk.providers, queryFn: api.providers.list });
