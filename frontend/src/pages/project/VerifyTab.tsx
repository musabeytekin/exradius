import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { StateChip } from "@/components/ui/data";
import { Icon } from "@/components/ui/icons";
import { Banner, Button, Card, CardHeader, EmptyState, Skeleton, Spinner } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { between, duration, relativeTime } from "@/lib/format";
import { qk } from "@/lib/queries";
import { toast } from "@/lib/toast";
import type { Project, Verification, VerificationStep } from "@/lib/types";

const isLive = (v: Verification | undefined) => v?.state === "pending" || v?.state === "running";

export function VerifyTab({ project }: { project: Project }) {
  const qc = useQueryClient();
  const history = useQuery({
    queryKey: qk.verifications(project.id),
    queryFn: () => api.projects.verifications(project.id),
    refetchInterval: (q) => (isLive(q.state.data?.[0]) ? 1000 : false),
  });
  const [selected, setSelected] = useState<string | null>(null);
  const shown = history.data?.find((v) => v.id === selected) ?? history.data?.[0];

  const start = useMutation({
    mutationFn: () => api.projects.verify(project.id),
    onSuccess: ({ verification_id }) => {
      setSelected(verification_id);
      qc.invalidateQueries({ queryKey: qk.verifications(project.id) });
    },
    onError: (e) => toast.err("Could not start the verification", e),
  });

  return (
    <div className="grid grid-cols-[minmax(0,1fr)_260px] gap-6">
      <Card>
        <CardHeader
          title="Verify project"
          description="Reads the config, builds images, clones, starts services and runs setup in a throwaway directory. No agent runs."
          actions={
            <Button variant="primary" icon="play" loading={start.isPending} disabled={isLive(history.data?.[0])} onClick={() => start.mutate()}>
              {isLive(history.data?.[0]) ? "Verifying…" : history.data?.length ? "Verify again" : "Verify"}
            </Button>
          }
        />
        {history.isLoading ? (
          <div className="p-4"><Skeleton className="h-40" /></div>
        ) : !shown ? (
          <EmptyState
            icon="shield"
            title="Not verified yet"
            action={<Button variant="secondary" icon="play" onClick={() => start.mutate()} loading={start.isPending}>Run the dry run</Button>}
          >
            No verification has run for this project.
          </EmptyState>
        ) : (
          <VerificationView v={shown} />
        )}
      </Card>
      <Card className="self-start">
        <CardHeader title="History" description="Last five runs" />
        {!history.data?.length ? (
          <p className="px-4 py-6 text-center text-xs text-subtle">Nothing yet</p>
        ) : (
          <ul className="p-1">
            {history.data.map((v) => (
              <li key={v.id}>
                <button
                  type="button"
                  onClick={() => setSelected(v.id)}
                  className={cn("flex w-full items-center justify-between gap-2 rounded-md px-3 py-2 text-left hover:bg-surface-2", v.id === shown?.id && "bg-surface-2")}
                >
                  <span className="text-xs text-muted">{relativeTime(v.created_at)}</span>
                  <StateChip state={v.state} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function VerificationView({ v }: { v: Verification }) {
  const total = between(v.started_at ?? v.created_at, v.ended_at);
  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex items-center gap-3 text-sm">
        <StateChip state={v.state} />
        <span className="text-muted">
          {v.state === "pending" ? "Waiting for a worker slot…" : `Started ${relativeTime(v.started_at ?? v.created_at)}`}
        </span>
        {v.ended_at && <span className="text-subtle">· took {duration(total)}</span>}
      </div>
      {v.error && <Banner tone="err" title="Verification failed"><span className="font-mono text-xs">{v.error}</span></Banner>}
      <ol className="relative flex flex-col">
        {v.steps.map((s, i) => (
          <Step key={`${v.id}:${i}`} step={s} last={i === v.steps.length - 1} />
        ))}
        {v.steps.length === 0 && <li className="text-sm text-subtle">The worker has not recorded its plan yet.</li>}
      </ol>
    </div>
  );
}

function Step({ step, last }: { step: VerificationStep; last: boolean }) {
  // Failed and running steps open by themselves until the reader toggles them.
  const [toggled, setOpen] = useState<boolean | null>(null);
  const open = toggled ?? (step.state === "failed" || step.state === "running");
  const ms = step.started_at ? between(step.started_at, step.ended_at) : null;
  const icon = {
    passed: <Icon name="check" size={12} strokeWidth={2.5} />,
    failed: <Icon name="x" size={12} strokeWidth={2.5} />,
    running: <Spinner size={12} />,
    pending: null,
    skipped: <span className="block h-px w-2 bg-current" />,
  }[step.state];
  return (
    <li className="relative flex gap-3 pb-1">
      {!last && <span className="absolute left-[11px] top-6 bottom-0 w-px bg-border" />}
      <span
        className={cn(
          "relative z-10 mt-1.5 flex size-[23px] shrink-0 items-center justify-center rounded-full border-2 border-surface",
          step.state === "passed" && "bg-ok text-white",
          step.state === "failed" && "bg-err text-white",
          step.state === "running" && "bg-info-soft text-info",
          step.state === "pending" && "bg-surface-3",
          step.state === "skipped" && "bg-surface-2 text-subtle",
        )}
      >
        {icon}
      </span>
      <div className="min-w-0 flex-1 pb-2">
        <button
          type="button"
          onClick={() => step.log && setOpen(!open)}
          className={cn("flex w-full items-center gap-2 rounded-md py-1.5 text-left", step.log && "hover:text-fg")}
        >
          <span className={cn("min-w-0 flex-1 truncate font-mono text-[12.5px]", step.state === "skipped" || step.state === "pending" ? "text-subtle" : "text-fg")}>
            {step.name}
          </span>
          <span className="text-xs tabular text-subtle">{step.state === "running" ? duration(ms) : step.ended_at ? duration(ms) : step.state}</span>
          {step.log && <Icon name="chevronDown" size={13} className={cn("text-subtle transition-transform", !open && "-rotate-90")} />}
        </button>
        {open && step.log && (
          <pre className="mt-1 max-h-72 overflow-auto rounded-md border border-border bg-code px-3 py-2 font-mono text-[11.5px] leading-[1.6] text-muted whitespace-pre-wrap">
            {step.log}
          </pre>
        )}
      </div>
    </li>
  );
}
