import { useQuery } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import type { ReactNode } from "react";
import { HrefLink } from "@/components/HrefLink";
import { StateChip } from "@/components/ui/data";
import { Icon } from "@/components/ui/icons";
import { Tooltip } from "@/components/ui/overlays";
import { Button, Card, CardHeader, Dot, EmptyState, Mono, PageHeader, Skeleton, type Tone } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { buttonClass } from "@/lib/buttonStyles";
import { FreshnessChip } from "@/components/FreshnessChip";
import { HealthChip } from "@/components/HealthChip";
import { projectHealth, projectName, storeNameOf } from "@/lib/projectConfig";
import { useStoreKeyIndex } from "@/lib/useStoreKeyIndex";
import { relativeTime } from "@/lib/format";
import { describeProvider, providerKinds } from "@/lib/providers";
import { qk, useProjects, useRuntimes, useStatus, useStores } from "@/lib/queries";
import { TERMINAL_STATES, type ProviderKindSpec, type Runtime, type SystemStatus } from "@/lib/types";
import { workerRuntimes, type RuntimeSummary } from "@/lib/workers";

export function OverviewPage() {
  const navigate = useNavigate();
  return (
    <>
      <PageHeader
        title="Overview"
        actions={
          <>
            <Button variant="secondary" icon="plus" onClick={() => navigate({ to: "/projects", search: { create: "1" } })}>Add project</Button>
            <Button variant="primary" icon="send" onClick={() => navigate({ to: "/tasks", search: { send: "1" } })}>Send a task</Button>
          </>
        }
      />
      <div className="flex flex-col gap-6">
        <StatusStrip />
        <div className="grid grid-cols-[1.6fr_1fr] gap-6">
          <ProjectsHealth />
          <StoresSummary />
        </div>
        <RecentTasks />
      </div>
    </>
  );
}

// --- System status -------------------------------------------------------------------------

interface StatusItem { key: string; label: string; tone: Tone; value: ReactNode; sub?: ReactNode; fix?: ReactNode; href?: string }

function statusItems(s: SystemStatus, kinds: ProviderKindSpec[], declared: Runtime[] | undefined): StatusItem[] {
  const workers = s.workers;
  const slots = workers.reduce((n, w) => n + w.concurrency, 0);
  const rts = workerRuntimes(workers, declared);
  const op = s.operator;
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
      fix: workers.length ? undefined : <>No worker has sent a heartbeat in 60 s: check the <code>exradius-worker</code> pods.</>,
    },
    {
      key: "operator", label: "Operator", tone: !op ? "neutral" : op.alive ? "ok" : "err",
      value: !op ? "unknown" : op.alive ? "running" : "not running",
      sub: op?.alive && op.renewed_at ? `lease renewed ${relativeTime(op.renewed_at)}` : undefined,
      fix: op && !op.alive ? <>{op.detail ?? "The operator's leader Lease is not renewed"}: runs cannot start. Check the <code>exradius-operator</code> pods.</> : undefined,
    },
    runtimesItem(rts),
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

// --- Projects and stores ------------------------------------------------------------------

function ProjectsHealth() {
  const projects = useProjects();
  const storeKeys = useStoreKeyIndex();
  const list = projects.data ?? [];
  return (
    <Card>
      <CardHeader
        title="Projects"
        actions={<Link to="/projects" search={{}} className={buttonClass("ghost", "sm")}>All projects<Icon name="arrowRight" size={13} /></Link>}
      />
      {projects.isLoading ? (
        <div className="flex flex-col gap-2 p-4">{[0, 1].map((i) => <Skeleton key={i} className="h-8" />)}</div>
      ) : projects.isError ? (
        <p className="px-4 py-5 text-center text-sm text-muted">Could not load projects: {(projects.error as Error).message}</p>
      ) : !list.length ? (
        <EmptyState compact icon="projects" title="No projects" />
      ) : (
        <ul>
          {list.map((p) => (
            <li key={p.id}>
              <Link
                to="/projects/$projectId"
                params={{ projectId: p.id }}
                search={{}}
                className="flex h-11 items-center gap-3 border-b border-border px-4 text-sm last:border-b-0 hover:bg-surface-2/60"
              >
                <Mono className="w-40 truncate font-medium">{p.slug}</Mono>
                <span className="min-w-0 flex-1 truncate text-xs text-muted">{projectName(p)}</span>
                <FreshnessChip project={p} />
                <HealthChip info={projectHealth(p, storeKeys(storeNameOf(p)))} />
              </Link>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function StoresSummary() {
  const stores = useStores();
  const n = stores.data?.length ?? 0;
  return (
    <Card>
      <CardHeader
        title="Stores"
        actions={<Link to="/stores" search={{}} className={buttonClass("ghost", "sm")}>All stores<Icon name="arrowRight" size={13} /></Link>}
      />
      <div className="px-4 py-4">
        {stores.isLoading ? (
          <Skeleton className="h-10" />
        ) : stores.isError ? (
          <p className="text-sm text-muted">Not available on this API.</p>
        ) : (
          <>
            <div className="text-[26px] font-semibold tabular leading-none text-fg">{n}</div>
            <div className="mt-1 text-xs text-muted">{n === 1 ? "secret store" : "secret stores"}</div>
            {n > 0 && (
              <div className="mt-3 flex flex-wrap gap-1.5">
                {stores.data!.map((s) => (
                  <Link key={s.id} to="/stores/$storeId" params={{ storeId: s.id }} className="rounded-full border border-border bg-surface-2 px-2 py-0.5 font-mono text-[11px] text-muted hover:text-fg">
                    {s.name}
                  </Link>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </Card>
  );
}

// --- Tasks -----------------------------------------------------------------------------------

function RecentTasks() {
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
        <EmptyState compact icon="tasks" title="No tasks" />
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
