import { useMutation, useQuery } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { FileDrop } from "@/components/FileDrop";
import { PickList, Table } from "@/components/ui/controls";
import { CodeBlock, StateChip } from "@/components/ui/data";
import { Icon } from "@/components/ui/icons";
import { Dialog, Tooltip } from "@/components/ui/overlays";
import { Button, Card, EmptyState, Field, Input, Mono, PageHeader, SkeletonRows, Textarea } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { elideBase64, fileToAttachment } from "@/lib/files";
import { absoluteTime, pretty, relativeTime } from "@/lib/format";
import { agentSlugsOf, agentsOf, configOf, projectHealth, projectName } from "@/lib/projectConfig";
import { qk, useProject, useProjects } from "@/lib/queries";
import { toast } from "@/lib/toast";
import { ALL_STATES, type ConfigAgent, type TaskMessage } from "@/lib/types";
import type { TasksSearch } from "@/router";

const PAGE = 50;

export function TasksPage() {
  const search = useSearch({ from: "/app/tasks" });
  const navigate = useNavigate();
  const projects = useProjects();
  const [query, setQuery] = useState("");
  const page = search.page ?? 1;
  const filters = { state: search.state, project: search.project, agent: search.agent, limit: PAGE + 1, offset: (page - 1) * PAGE };
  const tasks = useQuery({ queryKey: qk.tasks(filters), queryFn: () => api.tasks.list(filters), refetchInterval: 5000, placeholderData: (prev) => prev });
  const rows = (tasks.data ?? []).slice(0, PAGE).filter((t) => !query || t.task_id.toLowerCase().includes(query.toLowerCase()));
  const hasNext = (tasks.data?.length ?? 0) > PAGE;
  const filtered = !!(search.state || search.project || search.agent);
  // Agent slugs from the projects' snapshots, plus any seen on this page or in the filter.
  const agentSlugs = [...new Set([
    ...(projects.data ?? []).filter((p) => !search.project || p.slug === search.project).flatMap((p) => agentSlugsOf(p)),
    ...(tasks.data ?? []).map((t) => t.agent),
    ...(search.agent ? [search.agent] : []),
  ])].sort();
  const setSearch = (patch: Partial<TasksSearch>) => navigate({ to: "/tasks", search: { ...search, page: undefined, ...patch }, replace: true });

  return (
    <>
      <PageHeader
        title="Tasks"
        actions={<Button variant="primary" icon="send" onClick={() => setSearch({ send: "1" })}>Send a task</Button>}
      />
      <div className="mb-3 flex items-center gap-2">
        <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search task ids on this page" leading={<Icon name="search" size={14} />} className="w-72" aria-label="Search task ids" />
        <div className="w-44">
          <PickList size="sm" aria-label="State" value={search.state ?? null} onChange={(v) => setSearch({ state: v ?? undefined })} noneLabel="Any state" options={ALL_STATES.map((s) => ({ value: s, label: <StateChip state={s} />, text: s.replace(/_/g, " ") }))} />
        </div>
        <div className="w-44">
          <PickList size="sm" mono aria-label="Project" value={search.project ?? null} onChange={(v) => setSearch({ project: v ?? undefined })} noneLabel="Any project" options={(projects.data ?? []).map((p) => ({ value: p.slug, label: p.slug }))} />
        </div>
        <div className="w-44">
          <PickList size="sm" mono aria-label="Agent" value={search.agent ?? null} onChange={(v) => setSearch({ agent: v ?? undefined })} noneLabel="Any agent" options={agentSlugs.map((a) => ({ value: a, label: a }))} />
        </div>
        {filtered && <Button size="sm" variant="ghost" icon="x" onClick={() => navigate({ to: "/tasks", search: {}, replace: true })}>Clear</Button>}
        {tasks.isFetching && !tasks.isLoading && <span className="ml-auto size-1.5 animate-pulse-dot rounded-full bg-info" aria-label="Refreshing" />}
      </div>
      <Card>
        {tasks.isLoading ? (
          <SkeletonRows rows={5} />
        ) : rows.length === 0 ? (
          filtered || query ? (
            <EmptyState compact icon="filter" title="No tasks match" action={<Button size="sm" variant="secondary" onClick={() => { setQuery(""); navigate({ to: "/tasks", search: {}, replace: true }); }}>Clear filters</Button>}>
              Nothing on this page matches the filters.
            </EmptyState>
          ) : (
            <EmptyState icon="tasks" title="No tasks" action={<Button variant="primary" icon="send" onClick={() => setSearch({ send: "1" })}>Send a task</Button>} />
          )
        ) : (
          <Table.Root>
            <Table.Head>
              <tr>
                <Table.Th>Task</Table.Th>
                <Table.Th>Project</Table.Th>
                <Table.Th>Agent</Table.Th>
                <Table.Th>State</Table.Th>
                <Table.Th className="text-right">Received</Table.Th>
              </tr>
            </Table.Head>
            <tbody>
              {rows.map((t) => (
                <Table.Row key={t.task_id} onClick={() => navigate({ to: "/tasks/$taskId", params: { taskId: t.task_id } })}>
                  <Table.Td><Mono className="font-medium">{t.task_id}</Mono></Table.Td>
                  <Table.Td><Mono className="text-xs text-muted">{t.project}</Mono></Table.Td>
                  <Table.Td><Mono className="text-xs text-muted">{t.agent}</Mono></Table.Td>
                  <Table.Td>
                    <span className="flex items-center gap-2">
                      <StateChip state={t.state} />
                      {t.waiting_for === "capacity" && t.state === "preparing" && <span className="text-xs text-muted">waiting for cluster capacity</span>}
                    </span>
                  </Table.Td>
                  <Table.Td className="text-right">
                    <Tooltip content={absoluteTime(t.received_at)}><span className="text-xs tabular text-subtle">{relativeTime(t.received_at)}</span></Tooltip>
                  </Table.Td>
                </Table.Row>
              ))}
            </tbody>
          </Table.Root>
        )}
        {(page > 1 || hasNext) && (
          <div className="flex items-center justify-between border-t border-border px-4 py-2 text-xs text-muted">
            <span>Page {page}</span>
            <div className="flex gap-1">
              <Button size="xs" variant="ghost" icon="chevronLeft" disabled={page <= 1} onClick={() => navigate({ to: "/tasks", search: { ...search, page: page - 1 > 1 ? page - 1 : undefined } })}>Newer</Button>
              <Button size="xs" variant="ghost" iconRight="chevronRight" disabled={!hasNext} onClick={() => navigate({ to: "/tasks", search: { ...search, page: page + 1 } })}>Older</Button>
            </div>
          </div>
        )}
      </Card>
      {search.send && <SendTaskDialog initialProject={search.project} initialAgent={search.agent} onClose={() => setSearch({ send: undefined })} />}
    </>
  );
}

export function SendTaskDialog({ initialProject, initialAgent, onClose }: { initialProject?: string; initialAgent?: string; onClose: () => void }) {
  const navigate = useNavigate();
  const projects = useProjects();
  const [projectSlug, setProjectSlug] = useState<string | null>(initialProject ?? null);
  const listed = projects.data?.find((p) => p.slug === projectSlug) ?? (projectSlug ? undefined : projects.data?.length === 1 ? projects.data[0] : undefined);
  // The detail view carries the snapshot even when the list leaves it out.
  const detail = useProject(listed?.id ?? "");
  const project = detail.data ?? listed;
  const configured = agentsOf(configOf(project));
  const agentOptions: [string, ConfigAgent][] = configured.length ? configured : (project?.agents ?? []).map((slug) => [slug, {}]);
  const health = project ? projectHealth(project) : null;
  // null: no agent in the message, Exradius picks one.
  const [agentSlug, setAgentSlug] = useState<string | null>(initialAgent ?? null);
  const agentEntry = agentOptions.find(([slug]) => slug === agentSlug);
  const agent = agentEntry ? { slug: agentEntry[0], name: agentEntry[1].name || agentEntry[0] } : undefined;
  // The ticket key: typed by the user, never invented (branch names start from it).
  const [taskId, setTaskId] = useState("");
  const effectiveId = taskId.trim();
  const [instructions, setInstructions] = useState("");
  const [context, setContext] = useState("");
  const [files, setFiles] = useState<File[]>([]);

  const contextParsed = useMemo((): { ok: true; value: unknown } | { ok: false; error: string } => {
    if (!context.trim()) return { ok: true, value: null };
    try {
      return { ok: true, value: JSON.parse(context) };
    } catch (e) {
      return { ok: false, error: (e as Error).message };
    }
  }, [context]);

  const preview: TaskMessage = {
    task_id: effectiveId,
    project: project?.slug ?? "",
    ...(agent ? { agent: agent.slug } : {}),
    instructions,
    context: contextParsed.ok ? contextParsed.value : "…invalid JSON…",
    attachments: files.map((f) => ({ name: f.name, content_type: f.type || "application/octet-stream", content_base64: "…" })),
  };

  const send = useMutation({
    mutationFn: async () => api.tasks.send({ ...preview, context: contextParsed.ok ? contextParsed.value : null, attachments: await Promise.all(files.map(fileToAttachment)) }),
    onSuccess: (p) => {
      toast.ok(`Task ${effectiveId} published`, `To ${p.queue}. Following it now.`);
      navigate({ to: "/tasks/$taskId", params: { taskId: effectiveId } });
    },
    onError: (e) => toast.err("Could not publish the task", e),
  });
  const ready = !!project && agentOptions.length > 0 && !!effectiveId.trim() && !!instructions.trim() && contextParsed.ok;

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="Send a task"
      width={880}
      onSubmit={() => ready && send.mutate()}
      footer={
        <>
          <span className="mr-auto text-xs text-subtle"><span className="font-mono">⌘↵</span> sends</span>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" icon="send" loading={send.isPending} disabled={!ready}>Publish task</Button>
        </>
      }
    >
      <div
        className="grid grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)] gap-5"
        onKeyDown={(e) => {
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
            e.preventDefault();
            (e.currentTarget.closest("form") as HTMLFormElement | null)?.requestSubmit();
          }
        }}
      >
        <div className="flex flex-col gap-4">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Project">
              <PickList
                mono
                aria-label="Project"
                value={project?.slug ?? null}
                placeholder="Choose a project"
                onChange={(v) => {
                  setProjectSlug(v);
                  setAgentSlug(null);
                }}
                options={(projects.data ?? []).map((p) => ({ value: p.slug, label: p.slug, description: projectName(p) }))}
              />
            </Field>
            <Field label="Agent" warning={project && !detail.isLoading && !agentOptions.length ? "The project's config defines no agents." : undefined}>
              <PickList
                mono
                aria-label="Agent"
                value={agent?.slug ?? null}
                noneLabel={project ? "Pick automatically" : undefined}
                placeholder="Pick a project first"
                disabled={!project}
                onChange={setAgentSlug}
                options={agentOptions.map(([slug, a]) => ({ value: slug, label: slug, description: a.name || undefined }))}
              />
            </Field>
          </div>
          {health && health.health !== "ok" && (
            <p className="-mt-2 flex items-start gap-1 text-xs text-warn"><Icon name="alert" size={13} className="mt-0.5 shrink-0" />Not runnable: {health.detail ?? health.label}. The worker will reject the task.</p>
          )}
          <Field label="Task id" htmlFor="task-id">
            <Input id="task-id" mono required autoFocus value={taskId} onChange={(e) => setTaskId(e.target.value)} placeholder="BANK-204" spellCheck={false} />
          </Field>
          <Field label="Instructions">
            <Textarea rows={6} value={instructions} onChange={(e) => setInstructions(e.target.value)} placeholder="Customers report invoices with negative totals since Monday. Find the cause and fix it." />
          </Field>
          <Field label="Context" optional error={!contextParsed.ok ? contextParsed.error : undefined} hint="JSON.">
            <Textarea mono rows={4} value={context} onChange={(e) => setContext(e.target.value)} invalid={!contextParsed.ok} placeholder={'{ "ticket": "JIRA-4821", "customer": "acme" }'} spellCheck={false} />
          </Field>
          <Field label="Attachments" optional>
            <FileDrop files={files} onChange={setFiles} compact />
          </Field>
        </div>
        <div className="flex min-w-0 flex-col gap-2">
          <span className="text-xs font-medium text-muted">Message to publish</span>
          <CodeBlock lang="json" code={pretty(elideBase64({ version: 1, ...preview }))} className="flex-1" maxHeight={520} wrap />
        </div>
      </div>
    </Dialog>
  );
}
