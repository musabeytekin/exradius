import { shortCommit } from "@/lib/projectConfig";
import type { Project } from "@/lib/types";
import { Chip } from "./ui/primitives";

const TONE = { current: "ok", stale: "warn", failed: "err", running: "info", never: "neutral" } as const;

/** The latest verification as one word and the commit it verified (setup-and-verify.md §1.2). */
export function FreshnessChip({ project }: { project: Pick<Project, "freshness" | "verification"> }) {
  const freshness = project.freshness ?? "never";
  const commit = project.verification?.commit;
  return (
    <Chip tone={TONE[freshness]} dot pulse={freshness === "running"} title={commit ?? undefined}>
      {freshness}
      {commit && freshness !== "never" && <span className="font-mono text-[10.5px] opacity-80">{shortCommit(commit)}</span>}
    </Chip>
  );
}
