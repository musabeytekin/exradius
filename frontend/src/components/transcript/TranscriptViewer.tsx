import { memo, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { duration, truncate, usd } from "@/lib/format";
import { buildTranscript, EXRADIUS_TOOLS, matches, stats, type ToolResult, type TranscriptItem } from "@/lib/transcript";
import type { InfoRequest, TranscriptChunk } from "@/lib/types";
import { Markdown } from "../Markdown";
import { CodeBlock, CopyButton, JsonTree } from "../ui/data";
import { Icon, type IconName } from "../ui/icons";
import { Button, Chip, EmptyState, Input, Skeleton, Spinner } from "../ui/primitives";

type ToolItem = Extract<TranscriptItem, { kind: "tool" }>;

export interface TranscriptViewerProps {
  chunks: TranscriptChunk[];
  /** The run is still going: tools without a result show as running, and the view follows new output. */
  live?: boolean;
  loading?: boolean;
  infoRequests?: InfoRequest[];
  downloadUrl?: string | null;
  /** Shown when there is nothing yet. */
  emptyHint?: ReactNode;
}

/**
 * The signature screen: a run read as a conversation. Assistant text is prose; each tool
 * call is one line (tool name and its most telling argument) that expands to the full
 * input and output, with JSON as a tree. Subagent work nests under the call that started it.
 */
export function TranscriptViewer({ chunks, live = false, loading = false, infoRequests = [], downloadUrl, emptyHint }: TranscriptViewerProps) {
  const items = useMemo(() => buildTranscript(chunks), [chunks]);
  const counts = useMemo(() => stats(items), [items]);
  const [query, setQuery] = useState("");
  const [showThinking, setShowThinking] = useState(true);
  const [expandAll, setExpandAll] = useState<{ v: boolean; n: number } | null>(null);
  const [follow, setFollow] = useState(true);
  const endRef = useRef<HTMLDivElement>(null);
  const searchRef = useRef<HTMLInputElement>(null);

  const visible = items.filter((it) => (showThinking || it.kind !== "thinking") && matches(it, query));

  useEffect(() => {
    if (live && follow && !query) endRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [chunks.length, live, follow, query]);

  // Stop following when the reader scrolls up; resume when they reach the bottom again.
  useEffect(() => {
    if (!live) return;
    const onScroll = () => {
      const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 80;
      setFollow(atBottom);
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, [live]);

  return (
    <div className="flex flex-col" data-testid="transcript">
      <div className="sticky top-0 z-10 -mx-1 flex items-center gap-2 border-b border-border bg-bg/90 px-1 py-2 backdrop-blur">
        <Input
          ref={searchRef}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Escape" && (setQuery(""), searchRef.current?.blur())}
          placeholder="Filter the transcript"
          aria-label="Filter the transcript"
          leading={<Icon name="search" size={14} />}
          className="w-64"
        />
        <div className="flex items-center gap-3 px-2 text-xs tabular text-subtle">
          <span>{counts.messages} messages</span>
          <span>{counts.tools} tool calls</span>
          {counts.errors > 0 && <span className="text-err">{counts.errors} failed</span>}
        </div>
        <div className="ml-auto flex items-center gap-1">
          <Button size="sm" variant={showThinking ? "ghost" : "subtle"} icon="brain" onClick={() => setShowThinking(!showThinking)} aria-pressed={!showThinking}>
            {showThinking ? "Hide thinking" : "Show thinking"}
          </Button>
          <Button size="sm" variant="ghost" icon={expandAll?.v ? "collapse" : "expand"} onClick={() => setExpandAll((e) => ({ v: !e?.v, n: (e?.n ?? 0) + 1 }))}>
            {expandAll?.v ? "Collapse tools" : "Expand tools"}
          </Button>
          {downloadUrl && (
            <a href={downloadUrl} className="inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-sm font-medium text-muted hover:bg-surface-2 hover:text-fg" download>
              <Icon name="download" size={14} />JSONL
            </a>
          )}
        </div>
      </div>

      {loading ? (
        <div className="flex flex-col gap-3 py-5">
          <Skeleton className="h-14" />
          <Skeleton className="h-8 w-3/4" />
          <Skeleton className="h-8 w-2/3" />
          <Skeleton className="h-20" />
        </div>
      ) : items.length === 0 ? (
        <EmptyState compact icon="terminal" title={live ? "Waiting for the agent" : "No transcript"}>
          {emptyHint ?? (live ? "Messages appear here as the runner forwards them." : "This run produced no messages.")}
        </EmptyState>
      ) : visible.length === 0 ? (
        <EmptyState compact icon="search" title="Nothing matches" action={<Button size="sm" variant="secondary" onClick={() => setQuery("")}>Clear filter</Button>}>
          No message or tool call mentions “{query}”.
        </EmptyState>
      ) : (
        <ol className="flex flex-col gap-1 py-4" aria-label="Transcript">
          {visible.map((it) => (
            <li key={it.key}>
              <Item item={it} live={live} expandAll={expandAll} infoRequests={infoRequests} query={query} />
            </li>
          ))}
        </ol>
      )}
      {live && items.length > 0 && (
        <div className="flex items-center gap-2 py-3 pl-1 text-xs text-subtle">
          <span className="flex gap-1">
            <span className="size-1.5 animate-pulse-dot rounded-full bg-info" />
            <span className="size-1.5 animate-pulse-dot rounded-full bg-info [animation-delay:200ms]" />
            <span className="size-1.5 animate-pulse-dot rounded-full bg-info [animation-delay:400ms]" />
          </span>
          Live
          {!follow && (
            <button type="button" className="ml-2 text-accent hover:underline" onClick={() => { setFollow(true); endRef.current?.scrollIntoView({ block: "end" }); }}>
              Jump to latest
            </button>
          )}
        </div>
      )}
      <div ref={endRef} />
    </div>
  );
}

const Item = memo(function Item({ item, live, expandAll, infoRequests, query, depth = 0 }: {
  item: TranscriptItem; live: boolean; expandAll: { v: boolean; n: number } | null; infoRequests: InfoRequest[]; query: string; depth?: number;
}) {
  switch (item.kind) {
    case "session":
      return <SessionRow item={item} />;
    case "text":
      return item.role === "assistant" ? (
        <div className="py-2 pl-8 pr-2">
          <Markdown text={item.text} />
        </div>
      ) : (
        <UserMessage text={item.text} />
      );
    case "thinking":
      return <Thinking text={item.text} />;
    case "tool":
      if (item.name === EXRADIUS_TOOLS.request) return <QuestionRow item={item} infoRequests={infoRequests} />;
      if (item.name === EXRADIUS_TOOLS.submit) return <SubmitRow item={item} live={live} />;
      return <ToolRow item={item} live={live} expandAll={expandAll} infoRequests={infoRequests} query={query} depth={depth} />;
    case "result":
      return <ResultRow item={item} />;
    case "system":
      return (
        <div className={cn("flex items-center gap-2 py-1 pl-8 text-xs", item.subtype === "paused" ? "text-warn" : "text-subtle")}>
          <Icon name={item.subtype === "paused" ? "clock" : "info"} size={12} />
          <span className="font-mono">{item.subtype}</span>
          {item.summary && <span className="truncate">{item.summary}</span>}
        </div>
      );
    case "error":
      return (
        <div className="ml-8 flex items-start gap-2 rounded-md border border-err/30 bg-err-soft px-3 py-2 text-sm text-err">
          <Icon name="alert" size={14} className="mt-0.5" />
          {item.text}
        </div>
      );
    default:
      return <RawRow label={item.type} raw={item.raw} />;
  }
});

function Gutter({ icon, tone = "neutral" }: { icon: IconName; tone?: "neutral" | "ok" | "err" | "warn" | "accent" | "info" }) {
  const tones = {
    neutral: "bg-surface-2 text-muted border-border",
    ok: "bg-ok-soft text-ok border-transparent",
    err: "bg-err-soft text-err border-transparent",
    warn: "bg-warn-soft text-warn border-transparent",
    accent: "bg-accent-soft text-accent border-transparent",
    info: "bg-info-soft text-info border-transparent",
  };
  return (
    <span className={cn("flex size-6 shrink-0 items-center justify-center rounded-md border", tones[tone])}>
      <Icon name={icon} size={13} />
    </span>
  );
}

function SessionRow({ item }: { item: Extract<TranscriptItem, { kind: "session" }> }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-2 rounded-lg border border-border bg-surface">
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full items-center gap-3 px-3 py-2 text-left" aria-expanded={open}>
        <Gutter icon="play" tone="accent" />
        <span className="text-sm font-medium">Session started</span>
        {item.model && <Chip mono>{item.model}</Chip>}
        {item.cwd && <span className="font-mono text-xs text-subtle">{item.cwd}</span>}
        <span className="ml-auto flex items-center gap-1.5 text-xs text-subtle">
          {item.tools.length} tools
          {item.mcpServers.map((s) => (
            <Chip key={s.name} mono tone={s.status === "connected" ? "ok" : s.status === "failed" ? "err" : "neutral"} title={s.status}>{s.name}</Chip>
          ))}
          <Icon name="chevronDown" size={13} className={cn("transition-transform", !open && "-rotate-90")} />
        </span>
      </button>
      {open && (
        <div className="border-t border-border px-3 py-3">
          <JsonTree value={item.raw} />
        </div>
      )}
    </div>
  );
}

function UserMessage({ text }: { text: string }) {
  const answer = /^Answer to your question:/i.test(text);
  return (
    <div className="my-2 flex gap-3 rounded-lg border border-border bg-surface-2/60 px-3 py-2.5">
      <Gutter icon={answer ? "send" : "user"} tone={answer ? "info" : "neutral"} />
      <div className="min-w-0 flex-1">
        <div className="mb-1 text-2xs font-medium uppercase tracking-wide text-subtle">{answer ? "Answer delivered · run resumed" : "User"}</div>
        <Markdown text={text} className="text-[13px]" />
      </div>
    </div>
  );
}

function Thinking({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="pl-8">
      <button type="button" onClick={() => setOpen(!open)} className="group flex max-w-full items-center gap-2 py-1 text-left text-xs text-subtle hover:text-muted" aria-expanded={open}>
        <Icon name="brain" size={13} />
        <span className="italic">Thinking</span>
        {!open && <span className="truncate italic opacity-70">{truncate(text, 110)}</span>}
        <Icon name="chevronDown" size={12} className={cn("shrink-0 transition-transform", !open && "-rotate-90")} />
      </button>
      {open && <div className="mb-2 whitespace-pre-wrap border-l-2 border-border pl-3 text-[13px] italic leading-relaxed text-muted">{text}</div>}
    </div>
  );
}

const TOOL_ICONS: Record<string, IconName> = {
  Bash: "terminal", Read: "file", Write: "edit", Edit: "edit", MultiEdit: "edit", NotebookEdit: "edit",
  Glob: "search", Grep: "search", WebFetch: "link", WebSearch: "search", Task: "agents", Agent: "agents",
  TodoWrite: "list", Skill: "sparkle",
};

function ToolRow({ item, live, expandAll, infoRequests, query, depth }: {
  item: ToolItem; live: boolean; expandAll: { v: boolean; n: number } | null; infoRequests: InfoRequest[]; query: string; depth: number;
}) {
  const [open, setOpen] = useState(false);
  const [lastToggle, setLastToggle] = useState(0);
  // "Expand tools" and "Collapse tools" apply to every row once per click.
  if (expandAll && expandAll.n !== lastToggle) {
    setLastToggle(expandAll.n);
    setOpen(expandAll.v);
  }
  const running = !item.result && live;
  const failed = item.result?.isError;
  const icon: IconName = item.server ? "connections" : TOOL_ICONS[item.tool] ?? "wrench";
  const hasChildren = item.children.length > 0;

  return (
    <div className={cn("rounded-md", open && "bg-surface ring-1 ring-border")}>
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className={cn("group flex w-full min-w-0 items-center gap-2.5 rounded-md px-1 py-1 text-left", !open && "hover:bg-surface-2/70")}
      >
        <Gutter icon={icon} tone={failed ? "err" : running ? "info" : "neutral"} />
        <span className="flex shrink-0 items-center gap-1 font-mono text-[12.5px]">
          {item.server && <span className="rounded bg-surface-2 px-1 text-[11px] text-subtle">{item.server}</span>}
          <span className={cn("font-medium", failed ? "text-err" : "text-fg")}>{item.tool}</span>
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-[12px] text-muted">{item.summary}</span>
        {hasChildren && <Chip>{item.children.filter((c) => c.kind === "tool").length} steps</Chip>}
        <span className="flex shrink-0 items-center gap-1.5 pr-1 text-xs">
          {running ? <Spinner size={12} className="text-info" /> : failed ? <Chip tone="err">error</Chip> : item.result ? <Icon name="check" size={13} className="text-ok" /> : <span className="text-subtle">no result</span>}
          <Icon name="chevronDown" size={13} className={cn("text-subtle transition-transform", !open && "-rotate-90")} />
        </span>
      </button>
      {open && (
        <div className="flex flex-col gap-3 px-3 pb-3 pl-[42px] pt-1">
          <ToolInput item={item} />
          {hasChildren && (
            <div className="border-l-2 border-accent/30 pl-3">
              <div className="mb-1 text-2xs font-medium uppercase tracking-wide text-subtle">Subagent</div>
              <ol className="flex flex-col gap-1">
                {item.children.filter((c) => matches(c, query)).map((c) => (
                  <li key={c.key}><Item item={c} live={live} expandAll={expandAll} infoRequests={infoRequests} query={query} depth={depth + 1} /></li>
                ))}
              </ol>
            </div>
          )}
          {item.result ? <ToolOutput result={item.result} /> : running ? (
            <div className="flex items-center gap-2 text-xs text-muted"><Spinner size={12} />Running…</div>
          ) : (
            <div className="text-xs text-subtle">The transcript has no result for this call.</div>
          )}
        </div>
      )}
    </div>
  );
}

function Section({ label, children, copy }: { label: string; children: ReactNode; copy?: string }) {
  return (
    <div className="min-w-0">
      <div className="mb-1 flex items-center justify-between">
        <span className="text-2xs font-medium uppercase tracking-wide text-subtle">{label}</span>
        {copy !== undefined && <CopyButton text={copy} />}
      </div>
      {children}
    </div>
  );
}

function ToolInput({ item }: { item: ToolItem }) {
  const i = item.input;
  const s = (k: string) => (typeof i[k] === "string" ? (i[k] as string) : null);
  if (item.tool === "Bash" && s("command")) {
    return (
      <Section label="Command">
        <CodeBlock code={s("command")!} lang="bash" wrap />
        {s("description") && <p className="mt-1 text-xs text-muted">{s("description")}</p>}
      </Section>
    );
  }
  if ((item.tool === "Edit" || item.tool === "MultiEdit") && s("file_path")) {
    const edits = item.tool === "MultiEdit" && Array.isArray(i.edits) ? (i.edits as { old_string: string; new_string: string }[]) : [{ old_string: s("old_string") ?? "", new_string: s("new_string") ?? "" }];
    return (
      <Section label={`Edit ${s("file_path")}`}>
        <div className="flex flex-col gap-2">
          {edits.map((e, n) => <Diff key={n} before={e.old_string} after={e.new_string} />)}
        </div>
      </Section>
    );
  }
  if (item.tool === "Write" && s("file_path") && s("content") !== null) {
    return (
      <Section label={`Write ${s("file_path")}`} copy={s("content")!}>
        <CodeBlock code={s("content")!} maxHeight={360} />
      </Section>
    );
  }
  if ((item.tool === "Task" || item.tool === "Agent") && s("prompt")) {
    return (
      <Section label={s("description") ?? "Prompt"}>
        <div className="rounded-md border border-border bg-surface-2/50 px-3 py-2"><Markdown text={s("prompt")!} className="text-[13px]" /></div>
      </Section>
    );
  }
  return (
    <Section label="Input" copy={JSON.stringify(i, null, 2)}>
      <div className="rounded-md border border-border bg-code px-3 py-2"><JsonTree value={i} defaultExpandDepth={2} /></div>
    </Section>
  );
}

function Diff({ before, after }: { before: string; after: string }) {
  return (
    <div className="overflow-hidden rounded-md border border-border font-mono text-[12px] leading-[1.6]">
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap bg-err-soft/60 px-3 py-1.5 text-fg">
        {before.split("\n").map((l, n) => <div key={n}><span className="select-none text-err">− </span>{l}</div>)}
      </pre>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap border-t border-border bg-ok-soft/60 px-3 py-1.5 text-fg">
        {after.split("\n").map((l, n) => <div key={n}><span className="select-none text-ok">+ </span>{l}</div>)}
      </pre>
    </div>
  );
}

const OUTPUT_LIMIT = 4000;

function ToolOutput({ result }: { result: ToolResult }) {
  const [full, setFull] = useState(false);
  const text = result.text;
  const json = useMemo(() => {
    const t = text.trim();
    if (!(t.startsWith("{") || t.startsWith("["))) return undefined;
    try {
      return JSON.parse(t) as unknown;
    } catch {
      return undefined;
    }
  }, [text]);
  const label = result.isError ? "Error" : "Output";
  if (json !== undefined) {
    return (
      <Section label={label} copy={text}>
        <div className={cn("rounded-md border px-3 py-2", result.isError ? "border-err/30 bg-err-soft/50" : "border-border bg-code")}>
          <JsonTree value={json} defaultExpandDepth={1} />
        </div>
      </Section>
    );
  }
  const long = text.length > OUTPUT_LIMIT;
  return (
    <Section label={label} copy={text}>
      {text ? (
        <pre className={cn("max-h-[480px] overflow-auto whitespace-pre-wrap break-words rounded-md border px-3 py-2 font-mono text-[12px] leading-[1.6]", result.isError ? "border-err/30 bg-err-soft/50 text-err" : "border-border bg-code text-muted")}>
          {full || !long ? text : `${text.slice(0, OUTPUT_LIMIT)}…`}
        </pre>
      ) : (
        <span className="text-xs italic text-subtle">empty</span>
      )}
      {long && (
        <button type="button" onClick={() => setFull(!full)} className="mt-1 text-xs text-accent hover:underline">
          {full ? "Show less" : `Show all ${text.length.toLocaleString()} characters`}
        </button>
      )}
    </Section>
  );
}

function QuestionRow({ item, infoRequests }: { item: ToolItem; infoRequests: InfoRequest[] }) {
  const question = typeof item.input.question === "string" ? item.input.question : "";
  const context = typeof item.input.context === "string" ? item.input.context : null;
  const ir = infoRequests.find((r) => r.question === question);
  return (
    <div className="my-2 flex gap-3 rounded-lg border border-warn/30 bg-warn-soft/50 px-3 py-3">
      <Gutter icon="question" tone="warn" />
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex items-center gap-2 text-2xs font-medium uppercase tracking-wide text-warn">
          Asked for information
          {ir && (ir.answered_at ? <Chip tone="ok">answered</Chip> : <Chip tone="warn" dot pulse>waiting</Chip>)}
        </div>
        <p className="text-[13.5px] font-medium text-fg">{question}</p>
        {context && <p className="mt-1 whitespace-pre-wrap text-sm text-muted">{context}</p>}
        {ir?.answer && (
          <div className="mt-2 border-l-2 border-ok/50 pl-3 text-sm text-fg">
            <span className="text-xs text-subtle">Answer: </span>
            {ir.answer}
          </div>
        )}
      </div>
    </div>
  );
}

function SubmitRow({ item, live }: { item: ToolItem; live: boolean }) {
  const [open, setOpen] = useState(true);
  const artifacts = Array.isArray(item.input.artifacts) ? (item.input.artifacts as string[]) : [];
  const failed = item.result?.isError;
  return (
    <div className={cn("my-2 rounded-lg border px-3 py-3", failed ? "border-err/30 bg-err-soft/40" : "border-ok/30 bg-ok-soft/40")}>
      <button type="button" className="flex w-full items-center gap-3 text-left" onClick={() => setOpen(!open)} aria-expanded={open}>
        <Gutter icon={failed ? "alert" : "check"} tone={failed ? "err" : "ok"} />
        <span className={cn("text-2xs font-medium uppercase tracking-wide", failed ? "text-err" : "text-ok")}>Submitted a result</span>
        {artifacts.length > 0 && <span className="text-xs text-muted">{artifacts.length} artifact{artifacts.length === 1 ? "" : "s"}</span>}
        {!item.result && live && <Spinner size={12} />}
        <Icon name="chevronDown" size={13} className={cn("ml-auto text-subtle transition-transform", !open && "-rotate-90")} />
      </button>
      {open && (
        <div className="mt-2 flex flex-col gap-2 pl-9">
          <div className="rounded-md border border-border bg-surface px-3 py-2"><JsonTree value={item.input.output} defaultExpandDepth={2} /></div>
          {artifacts.length > 0 && (
            <div className="flex flex-wrap gap-1.5">{artifacts.map((a) => <Chip key={a} mono><Icon name="file" size={11} />{a}</Chip>)}</div>
          )}
          {failed && item.result && <p className="font-mono text-xs text-err">{item.result.text}</p>}
        </div>
      )}
    </div>
  );
}

function ResultRow({ item }: { item: Extract<TranscriptItem, { kind: "result" }> }) {
  const tokens = item.usage ? Number(item.usage.input_tokens ?? 0) + Number(item.usage.output_tokens ?? 0) + Number(item.usage.cache_read_input_tokens ?? 0) + Number(item.usage.cache_creation_input_tokens ?? 0) : null;
  const interrupted = item.interrupted;
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 rounded-lg border border-border bg-surface px-3 py-2.5 text-xs text-muted">
      <span className="flex items-center gap-2">
        <Gutter icon={interrupted ? "clock" : item.isError ? "alert" : "check"} tone={interrupted ? "warn" : item.isError ? "err" : "ok"} />
        <span className="font-medium text-fg">{interrupted ? "Session paused for the answer" : item.isError ? "Session ended with an error" : "Session finished"}</span>
        <span className="font-mono text-subtle">{item.subtype}</span>
      </span>
      <span>Duration <span className="tabular text-fg">{duration(item.durationMs)}</span></span>
      {item.turns !== null && <span>Turns <span className="tabular text-fg">{item.turns}</span></span>}
      {tokens !== null && tokens > 0 && <span>Tokens <span className="tabular text-fg">{tokens.toLocaleString()}</span></span>}
      {item.costUsd !== null && <span>Cost <span className="tabular text-fg">{usd(item.costUsd)}</span></span>}
      {item.errors.length > 0 && <span className="w-full font-mono text-err">{item.errors.join("; ")}</span>}
    </div>
  );
}

function RawRow({ label, raw }: { label: string; raw: unknown }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="pl-8">
      <button type="button" onClick={() => setOpen(!open)} className="flex items-center gap-2 py-1 text-xs text-subtle hover:text-muted" aria-expanded={open}>
        <Icon name="info" size={12} />
        <span className="font-mono">{label}</span>
        <Icon name="chevronDown" size={12} className={cn("transition-transform", !open && "-rotate-90")} />
      </button>
      {open && <div className="rounded-md border border-border bg-code px-3 py-2"><JsonTree value={raw} /></div>}
    </div>
  );
}
