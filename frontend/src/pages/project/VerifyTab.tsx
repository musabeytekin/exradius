import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { StateChip } from "@/components/ui/data";
import { Icon } from "@/components/ui/icons";
import { Button, Card, CardHeader, EmptyState, Mono, Skeleton, Spinner } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { absoluteTime, between, duration, relativeTime } from "@/lib/format";
import { shortCommit } from "@/lib/projectConfig";
import { qk } from "@/lib/queries";
import { toast } from "@/lib/toast";
import type { Project, Verification, VerificationStep } from "@/lib/types";

const isLive = (v: Verification | undefined) => v?.state === "pending" || v?.state === "running";
const SETUP = "Setup: ";

export function VerifyTab({ project }: { project: Project }) {
  const qc = useQueryClient();
  const history = useQuery({
    queryKey: qk.verifications(project.id),
    queryFn: () => api.projects.verifications(project.id),
    refetchInterval: (q) => (isLive(q.state.data?.[0]) ? 1000 : false),
  });
  const [selected, setSelected] = useState<string | null>(null);
  const shown = history.data?.find((v) => v.id === selected) ?? history.data?.[0];
  const live = isLive(history.data?.[0]);

  const start = useMutation({
    mutationFn: () => api.projects.verify(project.id),
    onSuccess: ({ verification_id }) => {
      setSelected(verification_id);
      qc.invalidateQueries({ queryKey: qk.project(project.id) }); // the header's freshness and the history
    },
    onError: (e) => toast.err("Could not start the verification", e),
  });

  return (
    <div className="grid grid-cols-[minmax(0,1fr)_260px] gap-6">
      <Card>
        <CardHeader
          title="Verify"
          actions={
            <Button variant="primary" icon="play" loading={start.isPending} disabled={live} onClick={() => start.mutate()}>
              {live ? "Verifying…" : history.data?.length ? "Verify again" : "Verify"}
            </Button>
          }
        />
        {history.isLoading ? (
          <div className="p-4"><Skeleton className="h-40" /></div>
        ) : !shown ? (
          <EmptyState icon="shield" title="Not verified" />
        ) : (
          <VerificationView key={shown.id} v={shown} />
        )}
      </Card>
      <Card className="self-start">
        <CardHeader title="History" />
        {!history.data?.length ? (
          <p className="px-4 py-6 text-center text-xs text-subtle">—</p>
        ) : (
          <ul className="p-1">
            {history.data.map((v) => (
              <li key={v.id}>
                <button
                  type="button"
                  onClick={() => setSelected(v.id)}
                  className={cn("flex w-full items-center justify-between gap-2 rounded-md px-3 py-2 text-left hover:bg-surface-2", v.id === shown?.id && "bg-surface-2")}
                >
                  <span className="flex min-w-0 items-center gap-2 text-xs text-muted">
                    <span title={absoluteTime(v.created_at)}>{relativeTime(v.created_at)}</span>
                    {v.commit && <Mono className="text-[11px] text-subtle" title={v.commit}>{shortCommit(v.commit)}</Mono>}
                  </span>
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

/** The outcome first (setup-and-verify.md §1.4), then every step, collapsed. */
export function VerificationView({ v }: { v: Verification }) {
  const failedIndex = v.steps.findIndex((s) => s.state === "failed");
  const failed = failedIndex >= 0 ? v.steps[failedIndex] : undefined;
  const current = v.steps.find((s) => s.state === "running");
  const total = between(v.started_at ?? v.created_at, v.ended_at);
  return (
    <div className="flex flex-col gap-4 p-4">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
        <StateChip state={v.state} />
        {v.commit && <Mono className="text-xs text-muted" title={v.commit}>{shortCommit(v.commit)}</Mono>}
        <span className="text-xs text-subtle" title={absoluteTime(v.ended_at ?? v.started_at ?? v.created_at)}>
          {relativeTime(v.ended_at ?? v.started_at ?? v.created_at)}
        </span>
        {v.ended_at && <span className="text-xs text-subtle">· {duration(total)}</span>}
      </div>

      {v.state === "failed" && (failed ? <Failure step={failed} diagnosis={v.diagnosis} /> : v.error && (
        <pre className="rounded-md border border-err/30 bg-err-soft px-3 py-2 font-mono text-xs whitespace-pre-wrap text-err">{v.error}</pre>
      ))}

      {current && (
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <Spinner size={13} />
            <Mono className="text-[13px] text-fg">{current.name}</Mono>
            {current.started_at && <span className="text-xs tabular text-subtle">{duration(between(current.started_at, null))}</span>}
          </div>
          {current.log && <Log text={current.log} />}
        </div>
      )}

      {v.steps.length > 0 && <Steps steps={v.steps} skip={failedIndex} />}
    </div>
  );
}

function Failure({ step, diagnosis }: { step: VerificationStep; diagnosis?: string | null }) {
  const command = step.name.startsWith(SETUP) ? step.name.slice(SETUP.length) : null;
  return (
    <div className="flex flex-col gap-2.5 rounded-lg border border-err/30 p-3">
      <div className="flex items-center gap-2">
        <span className="flex size-[18px] items-center justify-center rounded-full bg-err text-white"><Icon name="x" size={11} strokeWidth={2.5} /></span>
        <span className="text-sm font-medium text-fg">{command ? "Setup" : step.name}</span>
      </div>
      {diagnosis && <p className="text-sm text-fg">{diagnosis}</p>}
      {command && <pre className="rounded-md bg-surface-2 px-3 py-1.5 font-mono text-[12px] whitespace-pre-wrap text-fg">$ {command}</pre>}
      {step.log && <Log text={step.log} />}
    </div>
  );
}

function Log({ text }: { text: string }) {
  return (
    <pre className="max-h-80 overflow-auto rounded-md border border-border bg-code px-3 py-2 font-mono text-[11.5px] leading-[1.6] text-muted whitespace-pre-wrap">
      {text}
    </pre>
  );
}

function Steps({ steps, skip }: { steps: VerificationStep[]; skip: number }) {
  const [open, setOpen] = useState(false);
  const counts = steps.reduce<Record<string, number>>((acc, s) => ({ ...acc, [s.state]: (acc[s.state] ?? 0) + 1 }), {});
  return (
    <div className="border-t border-border pt-3">
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 text-left text-xs text-muted hover:text-fg">
        <Icon name="chevronDown" size={13} className={cn("transition-transform", !open && "-rotate-90")} />
        <span>{steps.length} steps</span>
        <span className="text-subtle">
          {(["passed", "failed", "running", "pending", "skipped"] as const).filter((k) => counts[k]).map((k) => `${counts[k]} ${k}`).join(" · ")}
        </span>
      </button>
      {open && (
        <ol className="relative mt-2 flex flex-col">
          {steps.map((s, i) => <Step key={i} step={s} last={i === steps.length - 1} quiet={i === skip} />)}
        </ol>
      )}
    </div>
  );
}

function Step({ step, last, quiet }: { step: VerificationStep; last: boolean; quiet: boolean }) {
  const [open, setOpen] = useState(false);
  const ms = step.started_at ? between(step.started_at, step.ended_at) : null;
  const icon = {
    passed: <Icon name="check" size={12} strokeWidth={2.5} />,
    failed: <Icon name="x" size={12} strokeWidth={2.5} />,
    running: <Spinner size={12} />,
    pending: null,
    skipped: <span className="block h-px w-2 bg-current" />,
  }[step.state];
  const expandable = !!step.log && !quiet; // the failed step's log is already shown above
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
          onClick={() => expandable && setOpen(!open)}
          className={cn("flex w-full items-center gap-2 rounded-md py-1.5 text-left", expandable && "hover:text-fg")}
        >
          <span className={cn("min-w-0 flex-1 truncate font-mono text-[12.5px]", step.state === "skipped" || step.state === "pending" ? "text-subtle" : "text-fg")}>
            {step.name}
          </span>
          <span className="text-xs tabular text-subtle">{step.ended_at || step.state === "running" ? duration(ms) : step.state}</span>
          {expandable && <Icon name="chevronDown" size={13} className={cn("text-subtle transition-transform", !open && "-rotate-90")} />}
        </button>
        {open && expandable && <div className="mt-1"><Log text={step.log} /></div>}
      </div>
    </li>
  );
}
