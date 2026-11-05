import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState, type ReactNode } from "react";
import { HrefLink } from "@/components/HrefLink";
import { StateChip } from "@/components/ui/data";
import { Icon } from "@/components/ui/icons";
import { Tooltip } from "@/components/ui/overlays";
import { Button, Card, CardHeader, Dot, EmptyState, Mono, PageHeader, Skeleton, type Tone } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { buttonClass } from "@/lib/buttonStyles";
import { checklistProgress, type ChecklistItem } from "@/lib/checklist";
import { cn } from "@/lib/cn";
import { relativeTime } from "@/lib/format";
import { describeProvider, providerKinds } from "@/lib/providers";
import { qk, useRuntimes, useStatus } from "@/lib/queries";
import { TERMINAL_STATES, type ProviderKindSpec, type Runtime, type SystemStatus } from "@/lib/types";
import { workerRuntimes, type RuntimeSummary } from "@/lib/workers";
import { useChecklist } from "@/lib/useChecklist";

export function OverviewPage() {
  const { items, loading } = useChecklist();
  const progress = checklistProgress(items);
  const navigate = useNavigate();
  const [open, setOpen] = useState<boolean | null>(null);
  // Open while there is work left; once complete it collapses unless reopened.
  const expanded = open ?? !progress.complete;

  return (
    <>
      <PageHeader
        title="Overview"
        description="Readiness of this installation, and what is left to set up."
        actions={
          <>
            <Button variant="secondary" icon="plus" onClick={() => navigate({ to: "/projects", search: { create: "1" } })}>New project</Button>
            <Button variant="secondary" icon="plus" onClick={() => navigate({ to: "/agents", search: { create: "1" } })}>New agent</Button>
            <Button variant="primary" icon="send" onClick={() => navigate({ to: "/tasks", search: { send: "1" } })}>Send a task</Button>
          </>
        }
      />
      <div className="flex flex-col gap-6">
        <StatusStrip />
        {loading ? (
          <Skeleton className="h-[360px] rounded-lg" />
        ) : (
          <Checklist items={items} expanded={expanded} onToggle={() => setOpen(!expanded)} />
        )}
        <div className="grid grid-cols-[1.4fr_1fr] gap-6">
          <RecentTasks />
          <WaitingTasks />
        </div>
      </div>
    </>
  );
}

// --- System status -------------------------------------------------------------------------

interface StatusItem { key: string; label: string; tone: Tone; value: ReactNode; sub?: ReactNode; fix?: ReactNode; href?: string }

function statusItems(s: SystemStatus, kinds: ProviderKindSpec[], declared: Runtime[] | undefined): StatusItem[] {
  const workers = s.workers;
  const slots = workers.reduce((n, w) => n + w.concurrency, 0);
  const dockerBad = workers.filter((w) => !w.docker_ok);
  const imageBad = workers.filter((w) => w.runner_image_ok === false);
  const rts = workerRuntimes(workers, declared);
  return [
    {
      key: "database", label: "Database", tone: s.database.ok ? "ok" : "err",
      value: s.database.ok ? "reachable" : "unreachable",
      sub: s.database.ok ? s.database.detail : undefined,
      fix: s.database.ok ? undefined : <>The API cannot query Postgres: {s.database.detail}. Check the <code>postgres</code> service.</>,
    },
    {
      key: "broker", label: "Broker", tone: s.broker.ok ? "ok" : "err",
      value: s.broker.ok ? "connected" : "unreachable",
      fix: s.broker.ok ? undefined : <>{s.broker.detail}. Check the <code>rabbitmq</code> service.</>,
    },
    {
      key: "workers", label: "Workers", tone: workers.length ? "ok" : "err",
      value: workers.length ? `${workers.length} live` : "none",
      sub: workers.length ? `${slots} slot${slots === 1 ? "" : "s"}` : undefined,
      fix: workers.length ? undefined : <>No worker has sent a heartbeat in 60 s. Start one: <code>docker compose up -d worker</code>.</>,
    },
    {
      key: "docker", label: "Docker", tone: !workers.length ? "neutral" : dockerBad.length ? "err" : "ok",
      value: !workers.length ? "unknown" : dockerBad.length ? "unreachable" : "reachable",
      fix: dockerBad.length ? <>Worker {dockerBad[0].hostname}: {dockerBad[0].docker_detail}. Mount <code>/var/run/docker.sock</code> into the worker.</> : undefined,
    },
    rts ? runtimesItem(rts) : {
      key: "image", label: "Runner image", tone: !workers.length ? "neutral" : imageBad.length ? "err" : "ok",
      value: <Mono className="text-[12.5px]" title={workers[0]?.runner_image}>{workers[0]?.runner_image ?? "unknown"}</Mono>,
      sub: imageBad.length ? "missing" : workers.length ? "present" : undefined,
      fix: imageBad.length ? <>Image <code>{imageBad[0].runner_image}</code> is missing on the worker's daemon.</> : undefined,
    },
    providerItem(s, kinds),
  ];
}

function runtimesItem(rts: RuntimeSummary[]): StatusItem {
  const bad = rts.filter((r) => !r.ready);
  if (!rts.length) return { key: "runtimes", label: "Runtimes", tone: "err", value: "none", fix: <>The workers report no runtime bundle.</> };
  return {
    key: "runtimes", label: "Runtimes", tone: bad.length ? "err" : "ok",
    value: rts.map((r) => r.label).join(", "),
    sub: bad.length ? `${bad.map((r) => r.label).join(", ")} not ready` : rts.map((r) => r.versions.join("/")).filter(Boolean).join(", ") || "ready",
    fix: bad.length ? <>{bad.map((r) => `${r.label} is not ready on ${r.notReadyOn.join(", ")}${r.errors.length ? `: ${r.errors.join("; ")}` : ""}`).join(". ")}.</> : undefined,
  };
}

function providerItem(s: SystemStatus, kinds: ProviderKindSpec[]): StatusItem {
  const base = { key: "provider", label: "Provider", href: "/providers" };
  const p = s.provider;
  if (!p) return { ...base, tone: "neutral", value: "unknown", sub: "not reported" };
  const d = p.default;
  if (!p.configured || !d) {
    return { ...base, href: "/providers?create=1", tone: "err", value: "none", fix: <>Tasks are rejected until a provider exists. Add one on the Providers page.</> };
  }
  const t = d.last_test;
  const method = describeProvider(kinds, d).shortMethod;
  return {
    ...base,
    tone: !t ? "warn" : t.ok ? "ok" : "err",
    value: d.name,
    sub: <>{method} · {!t ? "not tested" : t.ok ? `tested ${relativeTime(t.tested_at)}` : "test failed"}</>,
    fix: !t ? <>{d.name} has not been tested yet.</> : !t.ok ? <>The last test of {d.name} failed{t.error ? `: ${t.error}` : "."}</> : undefined,
  };
}

function StatusStrip() {
  const status = useStatus();
  const runtimes = useRuntimes();
  if (status.isLoading) return <Skeleton className="h-[74px] rounded-lg" />;
  if (status.isError || !status.data) {
    return (
      <Card className="px-4 py-3 text-sm text-err">
        <Icon name="alert" size={14} className="mr-2 inline" />The status endpoint did not answer: {(status.error as Error)?.message}
      </Card>
    );
  }
  const items = statusItems(status.data, providerKinds(runtimes.data), runtimes.data);
  const fixes = items.filter((i) => i.fix);
  return (
    <Card className="overflow-hidden">
      <div className="grid grid-cols-6 divide-x divide-border">
        {items.map((it) => {
          const cell = (
            <div className="flex min-w-0 flex-col gap-1 px-3.5 py-2.5">
              <div className="flex items-center gap-1.5 text-2xs font-medium uppercase tracking-wide text-subtle">
                <Dot tone={it.tone} />
                {it.label}
              </div>
              <div className="truncate text-sm font-medium text-fg">{it.value}</div>
              <div className="h-4 truncate text-xs text-subtle">{it.sub}</div>
            </div>
          );
          return (
            <Tooltip key={it.key} content={it.fix}>
              {it.href ? <HrefLink href={it.href} className="min-w-0 hover:bg-surface-2/60">{cell}</HrefLink> : cell}
            </Tooltip>
          );
        })}
      </div>
      {fixes.length > 0 && (
        <div className="flex flex-col gap-1 border-t border-border bg-surface-2/50 px-4 py-2.5">
          {fixes.map((f) => (
            <div key={f.key} className="flex items-start gap-2 text-xs text-muted [&_code]:font-mono [&_code]:text-fg">
              <Dot tone={f.tone} className="mt-1" />
              <span><span className="font-medium text-fg">{f.label}.</span> {f.fix}</span>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

// --- Checklist -------------------------------------------------------------------------------

function Checklist({ items, expanded, onToggle }: { items: ChecklistItem[]; expanded: boolean; onToggle: () => void }) {
  const progress = checklistProgress(items);
  const nextId = items.find((i) => i.state === "todo")?.id;
  return (
    <Card>
      <button
        type="button"
        onClick={onToggle}
        aria-expanded={expanded}
        className={cn("flex w-full items-center gap-4 px-4 py-3 text-left", expanded && "border-b border-border")}
      >
        {progress.complete ? (
          <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-ok text-white"><Icon name="check" size={13} strokeWidth={2.5} /></span>
        ) : null}
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold text-fg">{progress.complete ? "Setup complete" : "Set up Exradius"}</div>
          <div className="text-xs text-muted">
            {progress.complete ? "Every step is done. Open it to review." : "Computed from what exists, so it is always current. It collapses once everything is done."}
          </div>
        </div>
        <div className="flex items-center gap-2.5">
          <span className="text-xs tabular text-muted">{progress.done} of {progress.total}</span>
          <div className="h-1.5 w-28 overflow-hidden rounded-full bg-surface-3">
            <div className={cn("h-full rounded-full transition-[width] duration-500", progress.complete ? "bg-ok" : "bg-accent")} style={{ width: `${(progress.done / progress.total) * 100}%` }} />
          </div>
          <Icon name="chevronDown" size={15} className={cn("text-subtle transition-transform", expanded && "rotate-180")} />
        </div>
      </button>
      {expanded && (
        <ol>
          {items.map((it, i) => {
            const isNext = it.id === nextId;
            return (
              <li key={it.id} className={cn("flex items-center gap-4 border-b border-border px-4 py-3 last:border-b-0", isNext && "bg-accent-soft/40")}>
                <span
                  className={cn(
                    "flex size-6 shrink-0 items-center justify-center rounded-full text-2xs font-semibold tabular",
                    it.state === "done" ? "bg-ok text-white" : it.state === "optional" ? "border border-dashed border-border-strong text-subtle" : isNext ? "bg-accent text-accent-fg" : "border border-border-strong text-muted",
                  )}
                >
                  {it.state === "done" ? <Icon name="check" size={13} strokeWidth={2.5} /> : i + 1}
                </span>
                <div className="min-w-0 flex-1">
                  <div className={cn("text-sm font-medium", it.state === "todo" ? "text-fg" : "text-muted")}>
                    {it.title}
                    {it.state === "optional" && <span className="ml-2 text-xs font-normal text-subtle">optional</span>}
                  </div>
                  <div className="truncate text-xs text-subtle">{it.detail}</div>
                </div>
                {it.state !== "done" && (
                  <HrefLink href={it.href} className={buttonClass(isNext ? "primary" : "secondary", "sm")}>
                    {it.action}
                    <Icon name="arrowRight" size={13} />
                  </HrefLink>
                )}
              </li>
            );
          })}
        </ol>
      )}
    </Card>
  );
}

// --- Tasks -----------------------------------------------------------------------------------

function RecentTasks() {
  const navigate = useNavigate();
  const tasks = useQuery({
    queryKey: qk.tasks({ limit: 8 }),
    queryFn: () => api.tasks.list({ limit: 8 }),
    // Poll fast while any listed task is still moving, so its chip follows it.
    refetchInterval: (q) => (q.state.data?.some((t) => !TERMINAL_STATES.includes(t.state) && t.state !== "waiting_for_input") ? 3_000 : 10_000),
  });
  return (
    <Card>
      <CardHeader
        title="Recent tasks"
        actions={<Link to="/tasks" className={buttonClass("ghost", "sm")}>All tasks<Icon name="arrowRight" size={13} /></Link>}
      />
      {tasks.isLoading ? (
        <div className="flex flex-col gap-2 p-4">{[0, 1, 2].map((i) => <Skeleton key={i} className="h-8" />)}</div>
      ) : !tasks.data?.length ? (
        <EmptyState
          compact
          icon="tasks"
          title="No tasks yet"
          action={<Button size="sm" variant="secondary" icon="send" onClick={() => navigate({ to: "/tasks", search: { send: "1" } })}>Send a task</Button>}
        >
          Tasks arrive on <code className="font-mono text-xs">exradius.tasks</code>, or from this panel.
        </EmptyState>
      ) : (
        <ul>
          {tasks.data.map((t) => (
            <li key={t.task_id}>
              <Link
                to="/tasks/$taskId"
                params={{ taskId: t.task_id }}
                className="flex h-11 items-center gap-3 border-b border-border px-4 text-sm last:border-b-0 hover:bg-surface-2/60"
              >
                <Mono className="min-w-0 flex-1 truncate">{t.task_id}</Mono>
                <span className="w-40 truncate text-xs text-muted">{t.project} · {t.agent}</span>
                <span className="w-32"><StateChip state={t.state} /></span>
                <span className="w-16 text-right text-xs tabular text-subtle">{relativeTime(t.received_at)}</span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function WaitingTasks() {
  const waiting = useQuery({
    queryKey: qk.tasks({ state: "waiting_for_input", limit: 100 }),
    queryFn: () => api.tasks.list({ state: "waiting_for_input", limit: 100 }),
    refetchInterval: 20_000,
  });
  return (
    <Card>
      <CardHeader title="Waiting for input" description="Runs paused on a question. Their directories stay on disk until answered or cancelled." />
      {waiting.isLoading ? (
        <div className="p-4"><Skeleton className="h-8" /></div>
      ) : !waiting.data?.length ? (
        <EmptyState compact icon="question" title="No open questions">
          When an agent calls <code className="font-mono text-xs">request_information</code>, the run waits here for an answer.
        </EmptyState>
      ) : (
        <ul>
          {waiting.data.map((t) => (
            <li key={t.task_id}>
              <Link to="/tasks/$taskId" params={{ taskId: t.task_id }} className="flex h-11 items-center gap-3 border-b border-border px-4 last:border-b-0 hover:bg-surface-2/60">
                <Dot tone="warn" pulse />
                <Mono className="min-w-0 flex-1 truncate">{t.task_id}</Mono>
                <span className="text-xs text-subtle">{relativeTime(t.received_at)}</span>
                <Icon name="chevronRight" size={14} className="text-subtle" />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}
