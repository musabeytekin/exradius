import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Icon } from "@/components/ui/icons";
import { Tooltip } from "@/components/ui/overlays";
import { Card, CardHeader, Chip, EmptyState, Mono, SkeletonRows, type Tone } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { absoluteTime, relativeTime } from "@/lib/format";
import { commitOf, errorsOf } from "@/lib/projectConfig";
import { qk } from "@/lib/queries";
import type { AgentProfile, ProfileConnection, ProfileTool, Project, ToolAnnotations } from "@/lib/types";

export function AgentsTab({ project }: { project: Project }) {
  const v = project.verification;
  const profiles = useQuery({
    // A new commit or a finished Verify changes what the profiles hold.
    queryKey: [...qk.projectAgents(project.id), commitOf(project), v?.id ?? null, v?.state ?? null],
    queryFn: () => api.projects.agents(project.id),
  });
  if (profiles.isLoading) return <Card><SkeletonRows rows={3} /></Card>;
  if (profiles.isError) {
    return <Card className="px-4 py-6 text-center text-sm text-err">Could not load the agents: {(profiles.error as Error).message}</Card>;
  }
  return <AgentProfilesView profiles={profiles.data ?? []} configErrors={errorsOf(project).length > 0 || !commitOf(project)} />;
}

/** The profiles rendered read-only. No queries: tests render it from fixtures. */
export function AgentProfilesView({ profiles, configErrors }: { profiles: AgentProfile[]; configErrors?: boolean }) {
  if (!profiles.length) {
    return (
      <Card>
        <EmptyState compact icon="agents" title="No agents">{configErrors ? "Nothing to show until the file reads cleanly." : undefined}</EmptyState>
      </Card>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      {profiles.map((a) => <ProfileCard key={a.slug} a={a} />)}
    </div>
  );
}

function ProfileCard({ a }: { a: AgentProfile }) {
  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            {a.name || a.slug}
            {a.name && a.name !== a.slug && <Mono className="text-xs font-normal text-muted">{a.slug}</Mono>}
          </span>
        }
      />
      {a.role?.trim() && <Role text={a.role} />}
      {a.connections.length === 0 ? (
        <p className="px-4 py-3 text-xs text-subtle">No connections</p>
      ) : (
        <ul aria-label={`Connections of ${a.slug}`} className="divide-y divide-border">
          {a.connections.map((c) => <ConnectionTools key={c.name} c={c} />)}
        </ul>
      )}
    </Card>
  );
}

const ROLE_LINES = 8;
const ROLE_CHARS = 600;

function Role({ text }: { text: string }) {
  const long = text.split("\n").length > ROLE_LINES || text.length > ROLE_CHARS;
  const [open, setOpen] = useState(false);
  const clamped = long && !open;
  return (
    <div className="border-b border-border px-4 py-3">
      <div className="mb-1.5 text-2xs font-medium uppercase tracking-wide text-subtle">Role</div>
      <pre
        aria-label="Role"
        className={cn(
          "rounded-md border border-border bg-code px-3 py-2 font-mono text-[11.5px] leading-[1.6] text-fg whitespace-pre-wrap break-words",
          clamped ? "max-h-40 overflow-hidden" : "max-h-[560px] overflow-auto",
        )}
      >
        {text}
      </pre>
      {long && (
        <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="mt-1.5 flex items-center gap-1.5 text-xs font-medium text-muted hover:text-fg">
          <Icon name="chevronRight" size={12} className={cn("transition-transform", open && "rotate-90")} />
          {open ? "Show less" : "Show all"}
        </button>
      )}
    </div>
  );
}

const TOOLS_OPEN_UP_TO = 10;

function ConnectionTools({ c }: { c: ProfileConnection }) {
  const tools = c.tools;
  const [open, setOpen] = useState(!!tools && tools.length <= TOOLS_OPEN_UP_TO);
  return (
    <li className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <Mono className="font-medium">{c.name}</Mono>
        <Chip mono>{c.kind}</Chip>
        {!tools ? (
          <Chip tone="warn" dot>{c.tools_note ?? "not listed"}</Chip>
        ) : tools.length === 0 ? (
          <span className="text-xs text-subtle">no tools</span>
        ) : (
          <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="flex items-center gap-1 text-xs font-medium text-muted hover:text-fg">
            <Icon name="chevronRight" size={12} className={cn("transition-transform", open && "rotate-90")} />
            {tools.length === 1 ? "1 tool" : `${tools.length} tools`}
          </button>
        )}
        {tools && c.tools_listed_at && (
          <Tooltip content={absoluteTime(c.tools_listed_at)}>
            <span className="text-xs text-subtle">listed {relativeTime(c.tools_listed_at)}</span>
          </Tooltip>
        )}
      </div>
      {tools && open && tools.length > 0 && (
        <ul aria-label={`Tools of ${c.name}`} className="mt-2 divide-y divide-border rounded-md border border-border">
          {tools.map((t) => <ToolRow key={t.name} t={t} />)}
        </ul>
      )}
    </li>
  );
}

function ToolRow({ t }: { t: ProfileTool }) {
  const title = typeof t.annotations?.title === "string" ? t.annotations.title : null;
  return (
    <li className="px-3 py-2">
      <div className="flex flex-wrap items-center gap-2">
        <Mono className="text-xs font-medium">{t.name}</Mono>
        {title && title !== t.name && <span className="text-xs text-muted">{title}</span>}
        {annotationChips(t.annotations).map(([label, tone]) => <Chip key={label} tone={tone}>{label}</Chip>)}
      </div>
      {t.description && <p className="mt-0.5 whitespace-pre-wrap break-words text-xs text-muted">{t.description}</p>}
    </li>
  );
}

/** Hints the server declares (MCP tool annotations); `destructiveHint` means nothing for a read-only tool. */
function annotationChips(a: ToolAnnotations | null | undefined): [string, Tone][] {
  if (!a) return [];
  const out: [string, Tone][] = [];
  if (a.readOnlyHint === true) out.push(["read-only", "neutral"]);
  else if (a.destructiveHint === true) out.push(["destructive", "err"]);
  if (a.idempotentHint === true) out.push(["idempotent", "neutral"]);
  if (a.openWorldHint === true) out.push(["open world", "neutral"]);
  return out;
}
