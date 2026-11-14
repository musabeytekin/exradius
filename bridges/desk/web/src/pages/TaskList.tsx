import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { AssigneeLabel, RunChip, StatusChip } from "@/components/TaskBits";
import { Loading, Problem, Select } from "@/components/ui";
import { api, type TaskFilters } from "@/lib/api";
import { STATUSES, STATUS_LABEL, ago } from "@/lib/format";
import { qk } from "@/lib/queries";

export function TaskList({ filters }: { filters: TaskFilters }) {
  const navigate = useNavigate();
  const me = useQuery({ queryKey: qk.me, queryFn: api.me, staleTime: 60_000 });
  const projects = useQuery({ queryKey: qk.projects, queryFn: api.projects });
  const people = useQuery({
    queryKey: qk.people(filters.project ?? ""),
    queryFn: () => api.people(filters.project!),
    enabled: !!filters.project,
  });
  const tasks = useQuery({ queryKey: qk.tasks(filters), queryFn: () => api.tasks(filters) });

  function set(change: Partial<TaskFilters>) {
    const next = { ...filters, ...change };
    if (change.project !== undefined && next.assignee?.startsWith("person:")) next.assignee = undefined;
    navigate({ to: "/tasks", search: Object.fromEntries(Object.entries(next).filter(([, v]) => v)) });
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <h1 className="mr-auto text-lg font-semibold tracking-[-0.01em]">Tasks</h1>
        <Select aria-label="Project" value={filters.project ?? ""} onChange={(e) => set({ project: e.target.value || undefined })}>
          <option value="">All projects</option>
          {projects.data?.map((p) => <option key={p.slug} value={p.slug}>{p.name}</option>)}
        </Select>
        <Select aria-label="Assignee" value={filters.assignee ?? ""} onChange={(e) => set({ assignee: e.target.value || undefined })}>
          <option value="">Anyone</option>
          <option value="me">Me</option>
          <option value="exradius">{me.data?.exradius ?? "Exradius"}</option>
          <option value="none">Unassigned</option>
          {people.data?.filter((p) => p.id !== me.data?.id).map((p) => <option key={p.id} value={`person:${p.id}`}>{p.name}</option>)}
        </Select>
        <Select aria-label="Status" value={filters.status ?? ""} onChange={(e) => set({ status: e.target.value || undefined })}>
          <option value="">Any status</option>
          {STATUSES.map((s) => <option key={s} value={s}>{STATUS_LABEL[s]}</option>)}
        </Select>
      </div>

      {tasks.isPending && <Loading />}
      {tasks.error && <Problem>{tasks.error.message}</Problem>}
      {tasks.data && tasks.data.length === 0 && <p className="py-10 text-center text-sm text-subtle">No tasks</p>}
      {tasks.data && tasks.data.length > 0 && (
        <ul className="divide-y divide-border overflow-hidden rounded-lg border border-border bg-surface">
          {tasks.data.map((t) => (
            <li key={t.key}>
              <Link to="/tasks/$key" params={{ key: t.key }}
                className="flex flex-col gap-1.5 px-3 py-2.5 hover:bg-surface-2 sm:flex-row sm:items-center sm:gap-3">
                <div className="flex min-w-0 flex-1 items-center gap-2">
                  <span className="shrink-0 font-mono text-xs text-subtle">{t.key}</span>
                  <span className="truncate font-medium">{t.title}</span>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-sm text-muted sm:shrink-0">
                  {t.run_state && <RunChip state={t.run_state} />}
                  <StatusChip status={t.status} />
                  <span className="w-32 truncate"><AssigneeLabel assignee={t.assignee} /></span>
                  <span className="w-20 text-xs text-subtle sm:text-right">{ago(t.updated_at)}</span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
