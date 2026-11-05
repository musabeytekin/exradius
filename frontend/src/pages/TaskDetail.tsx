import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useParams } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { FileDrop } from "@/components/FileDrop";
import { Markdown } from "@/components/Markdown";
import { TranscriptViewer } from "@/components/transcript/TranscriptViewer";
import { Tabs } from "@/components/ui/controls";
import { CodeBlock, CopyButton, JsonTree, KeyValue, StateChip } from "@/components/ui/data";
import { STATE_TONE } from "@/lib/states";
import { Icon } from "@/components/ui/icons";
import { Dialog, Tooltip } from "@/components/ui/overlays";
import { Banner, Button, Card, CardHeader, Chip, Dot, EmptyState, Mono, PageHeader, Skeleton, Spinner, Textarea } from "@/components/ui/primitives";
import { api, ApiError } from "@/lib/api";
import { useConfirm } from "@/lib/confirm";
import { elideBase64, fileToAttachment } from "@/lib/files";
import { absoluteTime, between, bytes, clockTime, duration, pretty, relativeTime, shortId } from "@/lib/format";
import { qk, useProjects } from "@/lib/queries";
import { toast } from "@/lib/toast";
import { TERMINAL_STATES, type Artifact, type InfoRequest, type ResultPayload, type StatusPayload, type TaskDetail, type TaskState } from "@/lib/types";
import { useTaskFeed, type FeedEvent, type FeedStatus } from "@/lib/useTaskFeed";

export function TaskDetailPage() {
  const { taskId } = useParams({ from: "/app/tasks/$taskId" });
  const task = useQuery({
    queryKey: qk.task(taskId),
    queryFn: () => api.tasks.get(taskId),
    retry: false,
    // A task just sent is not stored until the worker takes it.
    refetchInterval: (q) => (q.state.error instanceof ApiError && q.state.error.status === 404 ? 2000 : false),
  });
  const notFound = task.error instanceof ApiError && task.error.status === 404;
  const terminalAtLoad = task.data ? TERMINAL_STATES.includes(task.data.state) : notFound ? false : undefined;
  const feed = useTaskFeed(taskId, terminalAtLoad);

  // The stream is ahead of the task query; take the newest state from it.
  const lastStatus = [...feed.events].reverse().find((e) => e.type === "status")?.envelope.payload as unknown as StatusPayload | undefined;
  const result = feed.events.find((e) => e.type === "result")?.envelope.payload as unknown as ResultPayload | undefined;
  const state: TaskState | undefined = result?.state ?? lastStatus?.state ?? task.data?.state;
  const live = !!state && !TERMINAL_STATES.includes(state);
  const rejected = state === "rejected";

  return (
    <>
      <Header taskId={taskId} task={task.data} state={state} feedStatus={feed.status} />
      {task.isLoading ? (
        <Skeleton className="h-96" />
      ) : notFound && !feed.events.length ? (
        <Card>
          <EmptyState icon="clock" title="Waiting for the task to arrive" action={<Spinner />}>
            It was published to <code className="font-mono text-xs">exradius.tasks</code>. It appears here once a worker takes it; with every slot busy it waits in the queue.
          </EmptyState>
        </Card>
      ) : notFound && rejected ? (
        <Rejected events={feed.events} />
      ) : task.data ? (
        <Body task={task.data} state={state ?? task.data.state} live={live} feed={feed} result={result} />
      ) : (
        <Card className="p-4 text-sm text-err">Could not load the task: {(task.error as Error)?.message}</Card>
      )}
    </>
  );
}

function Header({ taskId, task, state, feedStatus }: { taskId: string; task?: TaskDetail; state?: TaskState; feedStatus: FeedStatus }) {
  const confirm = useConfirm();
  const qc = useQueryClient();
  const cancel = useMutation({
    mutationFn: () => api.tasks.cancel(taskId),
    onSuccess: () => {
      toast.ok("Cancel requested", "The worker stops the container and publishes the result.");
      qc.invalidateQueries({ queryKey: qk.task(taskId) });
    },
    onError: (e) => toast.err("Could not cancel", e),
  });
  const cancellable = !!task && !!state && !TERMINAL_STATES.includes(state);
  const projects = useProjects();
  const project = projects.data?.find((p) => p.slug === task?.project);
  return (
    <PageHeader
      breadcrumb={
        <>
          <Link to="/tasks" search={{}} className="hover:text-fg">Tasks</Link>
          <Icon name="chevronRight" size={12} />
          <span className="text-muted">{taskId}</span>
        </>
      }
      title={
        <span className="flex items-center gap-3">
          <span className="font-mono text-[18px]">{taskId}</span>
          <CopyButton text={taskId} label="Copy" />
        </span>
      }
      meta={
        <>
          {state && <StateChip state={state} />}
          {task && (
            <>
              {project ? (
                <Link to="/projects/$projectId" params={{ projectId: project.id }} search={{}} className="hover:underline">
                  <Mono className="text-xs text-muted">{task.project}</Mono>
                </Link>
              ) : (
                <Mono className="text-xs text-muted">{task.project}</Mono>
              )}
              <span className="text-xs text-subtle">·</span>
              <Mono className="text-xs text-muted">{task.agent}</Mono>
              <span className="text-xs text-subtle">· received {relativeTime(task.received_at)}</span>
            </>
          )}
          {(feedStatus === "live" || feedStatus === "reconnecting") && (
            <Chip tone={feedStatus === "live" ? "info" : "warn"} dot pulse>{feedStatus === "live" ? "following live" : "reconnecting"}</Chip>
          )}
        </>
      }
      actions={
        cancellable && (
          <Button
            variant="secondary"
            icon="stop"
            loading={cancel.isPending}
            onClick={async () => {
              const ok = await confirm({
                title: `Cancel ${taskId}?`,
                description: state === "waiting_for_input" ? "The paused run is ended and its directory removed. A result with state cancelled is published." : "The worker stops the run container. Uncommitted work in the run directory is lost. A result with state cancelled is published.",
                confirmLabel: "Cancel task",
              });
              if (ok) cancel.mutate();
            }}
          >
            Cancel task
          </Button>
        )
      }
    />
  );
}

function Rejected({ events }: { events: FeedEvent[] }) {
  const ev = events.find((e) => (e.envelope.payload as unknown as StatusPayload).state === "rejected");
  return (
    <div className="flex flex-col gap-4">
      <Banner tone="err" title="The worker rejected this task">
        {(ev?.envelope.payload as unknown as StatusPayload | undefined)?.reason ?? "No reason given."}
      </Banner>
      {ev && <CodeBlock title="task.status" lang="json" code={pretty(ev.envelope)} />}
    </div>
  );
}

function Body({ task, state, live, feed, result }: { task: TaskDetail; state: TaskState; live: boolean; feed: ReturnType<typeof useTaskFeed>; result?: ResultPayload }) {
  const [tab, setTab] = useState("transcript");
  const open = task.info_requests.filter((r) => !r.answered_at);
  // Info requests published since the task loaded: the stream knows before the task query.
  const streamed = feed.events.filter((e) => e.type === "info_request").map((e) => e.envelope.payload as { request_id: string; question: string; context: string | null });
  const pending = useMemo(() => {
    const ids = new Set(task.info_requests.map((r) => r.id));
    const extra: InfoRequest[] = streamed
      .filter((s) => !ids.has(s.request_id))
      .map((s) => ({ id: s.request_id, question: s.question, context: s.context, created_at: "", answered_at: null, answer: null, answer_attachments: null, late_answers: [] }));
    return [...open, ...extra];
  }, [open, streamed, task.info_requests]);

  return (
    <div className="grid grid-cols-[minmax(0,1fr)_300px] gap-6">
      <div className="flex min-w-0 flex-col gap-4">
        {state === "waiting_for_input" || pending.length > 0 ? pending.map((r) => <AnswerCard key={r.id} taskId={task.task_id} request={r} />) : null}
        {(result || task.run?.ended_at) && <ResultCard task={task} result={result} />}
        <Tabs.Root value={tab} onValueChange={setTab}>
          <Tabs.List>
            <Tabs.Trigger value="transcript">Transcript</Tabs.Trigger>
            <Tabs.Trigger value="input">Task</Tabs.Trigger>
            <Tabs.Trigger value="events" count={feed.events.length || task.events.length}>Events</Tabs.Trigger>
          </Tabs.List>
          <Tabs.Content value="transcript">
            <TranscriptViewer
              chunks={feed.chunks}
              live={live}
              loading={feed.loading}
              infoRequests={task.info_requests}
              downloadUrl={task.run_id ? api.runTranscriptUrl(task.run_id) : null}
              emptyHint={state === "queued" ? "Queued: waiting for a free worker slot." : state === "preparing" ? "Preparing: cloning repositories and running setup commands." : undefined}
            />
          </Tabs.Content>
          <Tabs.Content value="input" className="pt-4"><TaskInput task={task} /></Tabs.Content>
          <Tabs.Content value="events" className="pt-4"><EventsList events={feed.events.length ? feed.events : task.events.map((e) => ({ id: e.id, type: e.type, envelope: e.payload }))} /></Tabs.Content>
        </Tabs.Root>
      </div>
      <aside className="sticky top-6 flex max-h-[calc(100vh-48px)] flex-col gap-4 self-start overflow-y-auto">
        <Timeline events={feed.events.length ? feed.events : task.events.map((e) => ({ id: e.id, type: e.type, envelope: e.payload }))} live={live} />
        <RunCard task={task} />
        <Artifacts artifacts={task.artifacts} />
      </aside>
    </div>
  );
}

// --- Answer --------------------------------------------------------------------------------

function AnswerCard({ taskId, request }: { taskId: string; request: InfoRequest }) {
  const [answer, setAnswer] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [sent, setSent] = useState<Record<string, unknown> | null>(null);
  const qc = useQueryClient();
  const send = useMutation({
    mutationFn: async () => api.tasks.answer(taskId, { request_id: request.id, answer, attachments: await Promise.all(files.map(fileToAttachment)) }),
    onSuccess: (p) => {
      setSent(p.message);
      toast.ok("Answer published", `To ${p.queue}. The run resumes when a worker picks it up.`);
      qc.invalidateQueries({ queryKey: qk.task(taskId) });
    },
    onError: (e) => toast.err("Could not publish the answer", e),
  });
  return (
    <Card className="overflow-hidden border-warn/40 animate-pop-in">
      <div className="flex gap-3 bg-warn-soft/60 px-4 py-3">
        <Icon name="question" size={18} className="mt-0.5 shrink-0 text-warn" />
        <div className="min-w-0">
          <div className="text-2xs font-medium uppercase tracking-wide text-warn">The agent is waiting for an answer{request.created_at && ` · asked ${relativeTime(request.created_at)}`}</div>
          <p className="mt-1 text-[14px] font-medium text-fg">{request.question}</p>
          {request.context && <p className="mt-1 whitespace-pre-wrap text-sm text-muted">{request.context}</p>}
        </div>
      </div>
      {sent ? (
        <div className="flex flex-col gap-2 px-4 py-3">
          <div className="flex items-center gap-2 text-sm text-ok"><Icon name="check" size={14} />Published to exradius.answers</div>
          <CodeBlock lang="json" code={pretty(elideBase64(sent))} maxHeight={200} />
        </div>
      ) : (
        <form
          className="flex flex-col gap-3 px-4 py-3"
          onSubmit={(e) => {
            e.preventDefault();
            if (answer.trim()) send.mutate();
          }}
        >
          <Textarea
            autoFocus
            rows={3}
            value={answer}
            onChange={(e) => setAnswer(e.target.value)}
            placeholder="Your answer. The run resumes with it as the next message."
            aria-label="Answer"
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <FileDrop files={files} onChange={setFiles} compact />
          <div className="flex items-center justify-between">
            <span className="text-xs text-subtle">Published exactly as an integration would. <span className="font-mono">⌘↵</span> sends.</span>
            <Button type="submit" variant="primary" icon="send" loading={send.isPending} disabled={!answer.trim()}>Send answer</Button>
          </div>
        </form>
      )}
    </Card>
  );
}

// --- Result --------------------------------------------------------------------------------

function ResultCard({ task, result }: { task: TaskDetail; result?: ResultPayload }) {
  const run = task.run;
  const state = result?.state ?? run?.state ?? task.state;
  const summary = result?.summary ?? run?.summary;
  const output = result ? result.output : run?.output;
  const error = result?.error ?? run?.error;
  const tone = STATE_TONE[state] ?? "neutral";
  return (
    <Card className={tone === "ok" ? "border-ok/30" : tone === "err" ? "border-err/30" : ""}>
      <CardHeader
        title={<span className="flex items-center gap-2">Result <StateChip state={state} /></span>}
        description={run?.ended_at ? `Ended ${absoluteTime(run.ended_at)} after ${duration(between(run.started_at, run.ended_at))}` : undefined}
      />
      <div className="flex flex-col gap-4 p-4">
        {error && <Banner tone="err" title="Error"><span className="whitespace-pre-wrap break-words font-mono text-xs">{error}</span></Banner>}
        {summary && <Markdown text={summary} />}
        {output !== null && output !== undefined && (
          <div>
            <div className="mb-1 flex items-center justify-between">
              <span className="text-2xs font-medium uppercase tracking-wide text-subtle">Structured output</span>
              <CopyButton text={pretty(output)} />
            </div>
            <div className="rounded-md border border-border bg-code px-3 py-2"><JsonTree value={output} defaultExpandDepth={2} /></div>
          </div>
        )}
        {!summary && !error && (output === null || output === undefined) && <p className="text-sm text-subtle">The run ended without a summary or output.</p>}
      </div>
    </Card>
  );
}

// --- Sidebar -------------------------------------------------------------------------------

function Timeline({ events, live }: { events: FeedEvent[]; live: boolean }) {
  const statuses = events;
  return (
    <Card>
      <CardHeader title="Timeline" />
      {statuses.length === 0 ? (
        <p className="px-4 py-5 text-center text-xs text-subtle">No events yet</p>
      ) : (
        <ol className="flex flex-col px-4 py-3">
          {statuses.map((e, i) => {
            const p = e.envelope.payload as Partial<StatusPayload & ResultPayload & { question: string }>;
            const next = statuses[i + 1];
            const spent = next ? between(e.envelope.timestamp, next.envelope.timestamp) : null;
            const label = e.type === "status" ? (p.state ?? "status").replace(/_/g, " ") : e.type === "info_request" ? "asked a question" : `result: ${p.state}`;
            const tone = e.type === "info_request" ? "warn" : STATE_TONE[p.state ?? ""] ?? "neutral";
            const isLast = i === statuses.length - 1;
            return (
              <li key={e.id || i} className="relative flex gap-3 pb-3 last:pb-0">
                {!isLast && <span className="absolute left-[3.5px] top-3 bottom-0 w-px bg-border" />}
                <Dot tone={tone} pulse={isLast && live} className="mt-1.5" />
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline justify-between gap-2">
                    <span className="text-sm font-medium text-fg">{label}</span>
                    <Tooltip content={absoluteTime(e.envelope.timestamp)}>
                      <span className="text-xs tabular text-subtle">{clockTime(e.envelope.timestamp)}</span>
                    </Tooltip>
                  </div>
                  {p.reason && <p className="text-xs text-muted">{p.reason}</p>}
                  {e.type === "info_request" && p.question && <p className="line-clamp-2 text-xs text-muted">{p.question}</p>}
                  {spent !== null && spent > 0 && <p className="text-2xs tabular text-subtle">{duration(spent)}</p>}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </Card>
  );
}

function RunCard({ task }: { task: TaskDetail }) {
  const run = task.run;
  if (!run) return null;
  return (
    <Card>
      <CardHeader title="Run" />
      <div className="p-4">
        <KeyValue
          className="text-xs"
          items={[
            ["Run", <span className="flex items-center gap-1"><Mono className="text-xs" title={run.id}>{shortId(run.id)}</Mono><CopyButton text={run.id} label="" /></span>],
            ["Started", run.started_at ? <Tooltip content={absoluteTime(run.started_at)}><span>{relativeTime(run.started_at)}</span></Tooltip> : "—"],
            ["Duration", duration(between(run.started_at, run.ended_at))],
            ["Container", run.container_id ? <Mono className="text-xs" title={run.container_id}>{run.container_id.slice(0, 12)}</Mono> : "—"],
            ["Session", run.runtime_session_id ? <Mono className="text-xs" title={run.runtime_session_id}>{shortId(run.runtime_session_id)}</Mono> : "—"],
          ]}
        />
      </div>
    </Card>
  );
}

const PREVIEWABLE = /^(text\/|application\/(json|x-ndjson|xml|yaml|x-yaml|javascript))|\+json$/;

function Artifacts({ artifacts }: { artifacts: Artifact[] }) {
  const [preview, setPreview] = useState<Artifact | null>(null);
  return (
    <Card>
      <CardHeader title="Artifacts" />
      {artifacts.length === 0 ? (
        <p className="px-4 py-5 text-center text-xs text-subtle">Files the agent passes to submit_result appear here.</p>
      ) : (
        <ul className="p-1">
          {artifacts.map((a) => {
            const canPreview = PREVIEWABLE.test(a.content_type) || a.content_type.startsWith("image/");
            return (
              <li key={a.id} className="flex items-center gap-2 rounded-md px-3 py-2 hover:bg-surface-2">
                <Icon name="file" size={14} className="text-subtle" />
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-[12px]">{a.name}</div>
                  <div className="text-2xs text-subtle">{a.content_type}</div>
                </div>
                {canPreview && <Button size="xs" variant="ghost" square icon="eye" aria-label={`Preview ${a.name}`} onClick={() => setPreview(a)} />}
                <a href={api.artifactUrl(a.id)} download={a.name} aria-label={`Download ${a.name}`} className="flex size-6 items-center justify-center rounded-md text-muted hover:bg-surface-3 hover:text-fg">
                  <Icon name="download" size={14} />
                </a>
              </li>
            );
          })}
        </ul>
      )}
      {preview && <ArtifactPreview artifact={preview} onClose={() => setPreview(null)} />}
    </Card>
  );
}

function ArtifactPreview({ artifact, onClose }: { artifact: Artifact; onClose: () => void }) {
  const isImage = artifact.content_type.startsWith("image/");
  const content = useQuery({
    queryKey: ["artifact", artifact.id],
    queryFn: async () => {
      const res = await fetch(api.artifactUrl(artifact.id), { credentials: "same-origin" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return res.text();
    },
    enabled: !isImage,
    staleTime: Infinity,
  });
  const json = artifact.content_type.includes("json") && content.data ? safeParse(content.data) : undefined;
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} title={<span className="font-mono">{artifact.name}</span>} description={artifact.content_type} width={860}>
      {isImage ? (
        <img src={api.artifactUrl(artifact.id)} alt={artifact.name} className="mx-auto max-h-[60vh] rounded-md border border-border" />
      ) : content.isLoading ? (
        <Skeleton className="h-64" />
      ) : content.isError ? (
        <p className="text-sm text-err">Could not load it: {(content.error as Error).message}</p>
      ) : json !== undefined ? (
        <JsonTree value={json} defaultExpandDepth={2} />
      ) : /markdown/.test(artifact.content_type) || artifact.name.endsWith(".md") ? (
        <Markdown text={content.data ?? ""} />
      ) : (
        <CodeBlock code={content.data ?? ""} maxHeight={560} />
      )}
      {!isImage && content.data && <p className="mt-2 text-right text-2xs text-subtle">{bytes(new Blob([content.data]).size)}</p>}
    </Dialog>
  );
}

function safeParse(s: string): unknown {
  try {
    return JSON.parse(s);
  } catch {
    return undefined;
  }
}

// --- Tabs ----------------------------------------------------------------------------------

function TaskInput({ task }: { task: TaskDetail }) {
  const projects = useProjects();
  const project = projects.data?.find((p) => p.slug === task.project);
  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader
          title="Instructions"
          actions={project && <Link to="/projects/$projectId" params={{ projectId: project.id }} search={{}} className="text-xs text-accent hover:underline">Open {project.slug}</Link>}
        />
        <div className="p-4"><Markdown text={task.instructions || "_No instructions._"} /></div>
      </Card>
      <div className="grid grid-cols-2 gap-4">
        <Card>
          <CardHeader title="Context" description="Rendered into the prompt under # Context." />
          <div className="p-4">{task.context === null || task.context === undefined ? <span className="text-sm text-subtle">null</span> : <JsonTree value={task.context} defaultExpandDepth={2} />}</div>
        </Card>
        <Card>
          <CardHeader title="Correlation" description="Echoed back unchanged on every event." />
          <div className="p-4">{task.correlation === null || task.correlation === undefined ? <span className="text-sm text-subtle">null</span> : <JsonTree value={task.correlation} defaultExpandDepth={2} />}</div>
        </Card>
      </div>
      <Card>
        <CardHeader title="Attachments" description="Written read-only to .exradius/attachments/ in the project directory." />
        {task.attachments.length === 0 ? (
          <p className="px-4 py-4 text-sm text-subtle">None</p>
        ) : (
          <ul className="divide-y divide-border">
            {task.attachments.map((a) => (
              <li key={a.id} className="flex items-center gap-3 px-4 py-2.5 text-sm">
                <Icon name="file" size={14} className="text-subtle" />
                <Mono className="flex-1">{a.name}</Mono>
                <span className="text-xs text-subtle">{a.content_type}</span>
                {a.source_url && <Chip>from URL</Chip>}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </div>
  );
}

function EventsList({ events }: { events: FeedEvent[] }) {
  if (!events.length) return <EmptyState compact icon="pulse" title="No events">Events appear as the worker publishes them to exradius.events.</EmptyState>;
  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs text-muted">Exactly what consumers of <code className="font-mono">exradius.events</code> received, oldest first.</p>
      {events.map((e, i) => (
        <details key={e.id || i} className="group rounded-lg border border-border bg-surface">
          <summary className="flex cursor-pointer list-none items-center gap-3 px-3 py-2">
            <Icon name="chevronRight" size={12} className="text-subtle transition-transform group-open:rotate-90" />
            <Mono className="text-xs">task.{e.type}</Mono>
            <span className="text-xs text-muted">{(e.envelope.payload as { state?: string }).state ?? (e.envelope.payload as { question?: string }).question ?? ""}</span>
            <span className="ml-auto text-xs tabular text-subtle">{clockTime(e.envelope.timestamp)}</span>
          </summary>
          <div className="border-t border-border px-3 py-2"><JsonTree value={e.envelope} defaultExpandDepth={2} /></div>
        </details>
      ))}
    </div>
  );
}
