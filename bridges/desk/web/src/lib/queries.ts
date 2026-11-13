import { QueryClient } from "@tanstack/react-query";
import { ApiError } from "./api";

export const qk = {
  me: ["me"] as const,
  projects: ["projects"] as const,
  people: (project: string) => ["people", project] as const,
  tasks: (filters: object) => ["tasks", filters] as const,
  task: (key: string) => ["task", key] as const,
};

export function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 10_000,
        retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
        refetchOnWindowFocus: true,
      },
    },
  });
}
