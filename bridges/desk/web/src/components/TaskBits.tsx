import { RUN_LABEL, STATUS_LABEL, runTone } from "@/lib/format";
import type { Assignee, RunState, Status } from "@/lib/types";
import { Avatar, Chip } from "./ui";

export function StatusChip({ status }: { status: Status }) {
  return <Chip tone={status === "done" ? "ok" : status === "in_progress" ? "accent" : "muted"}>{STATUS_LABEL[status]}</Chip>;
}

export function RunChip({ state }: { state: RunState }) {
  const live = !["completed", "failed", "cancelled", "rejected"].includes(state);
  return <Chip tone={runTone(state)} pulse={live && state !== "waiting_for_input"}>{RUN_LABEL[state]}</Chip>;
}

export function AssigneeLabel({ assignee }: { assignee: Assignee }) {
  if (assignee.kind === "none") return <span className="text-subtle">Unassigned</span>;
  return (
    <span className="inline-flex items-center gap-1.5">
      <Avatar name={assignee.name ?? "?"} exradius={assignee.kind === "exradius"} />
      <span className="truncate">{assignee.name}</span>
    </span>
  );
}
