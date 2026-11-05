import { useQueries, useQuery } from "@tanstack/react-query";
import { api } from "./api";
import { deriveChecklist, type ChecklistItem } from "./checklist";
import { qk, useAgents, useProjects, useStatus } from "./queries";

export function useChecklist(): { items: ChecklistItem[]; loading: boolean } {
  const status = useStatus();
  const projects = useProjects();
  const agents = useAgents();
  const completed = useQuery({
    queryKey: qk.tasks({ state: "completed", limit: 1 }),
    queryFn: () => api.tasks.list({ state: "completed", limit: 1 }),
  });
  const list = projects.data ?? [];
  const repos = useQueries({ queries: list.map((p) => ({ queryKey: qk.repositories(p.id), queryFn: () => api.repositories.list(p.id) })) });
  const verifs = useQueries({ queries: list.map((p) => ({ queryKey: qk.verifications(p.id), queryFn: () => api.projects.verifications(p.id) })) });
  const attached = useQueries({ queries: list.map((p) => ({ queryKey: qk.projectAgents(p.id), queryFn: () => api.projectAgents.list(p.id) })) });

  const loading =
    status.isLoading || projects.isLoading || agents.isLoading || completed.isLoading ||
    repos.some((q) => q.isLoading) || verifs.some((q) => q.isLoading) || attached.some((q) => q.isLoading);

  const items = deriveChecklist({
    provider: status.data?.provider,
    projects: list,
    repositories: Object.fromEntries(list.map((p, i) => [p.id, repos[i]?.data ?? []])),
    verifications: Object.fromEntries(list.map((p, i) => [p.id, verifs[i]?.data ?? []])),
    attachments: Object.fromEntries(list.map((p, i) => [p.id, attached[i]?.data ?? []])),
    agentCount: agents.data?.length ?? 0,
    completedTaskCount: completed.data?.length ?? 0,
  });
  return { items, loading };
}
