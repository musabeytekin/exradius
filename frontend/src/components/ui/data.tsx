import { useState, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { toast } from "@/lib/toast";
import type { TaskState } from "@/lib/types";
import { Icon } from "./icons";
import { STATE_TONE } from "@/lib/states";
import { Button, Chip } from "./primitives";

// --- JSON tree ---------------------------------------------------------------------------

export function JsonTree({ value, defaultExpandDepth = 1, className }: { value: unknown; defaultExpandDepth?: number; className?: string }) {
  return (
    <div className={cn("font-mono text-[12px] leading-[1.65]", className)} role="tree">
      <Node value={value} depth={0} expandDepth={defaultExpandDepth} />
    </div>
  );
}

function Node({ name, value, depth, expandDepth, last = true }: { name?: string; value: unknown; depth: number; expandDepth: number; last?: boolean }) {
  const isObj = value !== null && typeof value === "object";
  const entries = isObj ? (Array.isArray(value) ? value.map((v, i) => [String(i), v] as const) : Object.entries(value as object)) : [];
  const [open, setOpen] = useState(depth < expandDepth || entries.length <= 1);
  const comma = last ? "" : ",";
  const key = name !== undefined && (
    <>
      <span className="text-[color:var(--info)]">{Array.isArray(value) || /^\d+$/.test(name) ? name : `"${name}"`}</span>
      <span className="text-subtle">: </span>
    </>
  );

  if (!isObj) {
    return (
      <div className="whitespace-pre-wrap break-all pl-4" role="treeitem">
        {key}
        <Scalar value={value} />
        <span className="text-subtle">{comma}</span>
      </div>
    );
  }
  const [o, c] = Array.isArray(value) ? ["[", "]"] : ["{", "}"];
  if (entries.length === 0) {
    return (
      <div className="pl-4" role="treeitem">
        {key}
        <span className="text-subtle">{o}{c}{comma}</span>
      </div>
    );
  }
  return (
    <div role="treeitem" aria-expanded={open}>
      <div className="group flex cursor-pointer items-start" onClick={() => setOpen(!open)}>
        <Icon name="chevronRight" size={12} className={cn("mt-[4px] mr-1 shrink-0 text-subtle transition-transform", open && "rotate-90")} />
        <span>
          {key}
          <span className="text-subtle">{o}</span>
          {!open && (
            <span className="rounded bg-surface-2 px-1 text-subtle group-hover:text-muted">
              {Array.isArray(value) ? `${entries.length} items` : entries.slice(0, 3).map(([k]) => k).join(", ") + (entries.length > 3 ? ", …" : "")}
            </span>
          )}
          {!open && <span className="text-subtle">{c}{comma}</span>}
        </span>
      </div>
      {open && (
        <>
          <div className="ml-[5px] border-l border-border pl-1.5">
            {entries.map(([k, v], i) => (
              <Node key={k} name={k} value={v} depth={depth + 1} expandDepth={expandDepth} last={i === entries.length - 1} />
            ))}
          </div>
          <div className="pl-4 text-subtle">{c}{comma}</div>
        </>
      )}
    </div>
  );
}

function Scalar({ value }: { value: unknown }) {
  if (value === null) return <span className="text-subtle">null</span>;
  if (typeof value === "string") {
    const long = value.length > 400;
    return <LongString value={value} long={long} />;
  }
  if (typeof value === "number") return <span className="text-[color:var(--warn)]">{value}</span>;
  if (typeof value === "boolean") return <span className="text-accent">{String(value)}</span>;
  return <span>{String(value)}</span>;
}

function LongString({ value, long }: { value: string; long: boolean }) {
  const [full, setFull] = useState(!long);
  return (
    <span className="text-[color:var(--ok)]">
      "{full ? value : value.slice(0, 400)}
      {!full && (
        <button type="button" onClick={(e) => { e.stopPropagation(); setFull(true); }} className="mx-1 rounded bg-surface-2 px-1 text-subtle hover:text-fg">
          +{value.length - 400} chars
        </button>
      )}"
    </span>
  );
}

// --- Code block ----------------------------------------------------------------------------

export function CopyButton({ text, label = "Copy", className, size = "xs" }: { text: string; label?: string; className?: string; size?: "xs" | "sm" }) {
  const [done, setDone] = useState(false);
  return (
    <Button
      size={size}
      variant="ghost"
      icon={done ? "check" : "copy"}
      className={className}
      aria-label={label}
      onClick={async (e) => {
        e.stopPropagation();
        try {
          await navigator.clipboard.writeText(text);
          setDone(true);
          setTimeout(() => setDone(false), 1400);
        } catch {
          toast.err("Could not copy to the clipboard");
        }
      }}
    >
      {done ? "Copied" : label}
    </Button>
  );
}

type Lang = "json" | "bash" | "python" | "text";

export function CodeBlock({ code, lang = "text", title, className, maxHeight, wrap }: {
  code: string; lang?: Lang; title?: ReactNode; className?: string; maxHeight?: number; wrap?: boolean;
}) {
  return (
    <div className={cn("group relative overflow-hidden rounded-lg border border-border bg-code", className)}>
      {title !== undefined ? (
        <div className="flex h-8 items-center justify-between border-b border-border px-3">
          <span className="text-xs font-medium text-muted">{title}</span>
          <CopyButton text={code} />
        </div>
      ) : (
        <CopyButton text={code} className="absolute right-1.5 top-1.5 opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100" />
      )}
      <pre
        className={cn("overflow-auto px-3.5 py-3 font-mono text-[12px] leading-[1.65] text-fg", wrap ? "whitespace-pre-wrap break-words" : "whitespace-pre")}
        style={maxHeight ? { maxHeight } : undefined}
      >
        <Highlighted code={code} lang={lang} />
      </pre>
    </div>
  );
}

const TOKEN_RES: Record<Exclude<Lang, "text">, [RegExp, string][]> = {
  json: [
    [/"(?:[^"\\]|\\.)*"(?=\s*:)/y, "text-[color:var(--info)]"],
    [/"(?:[^"\\]|\\.)*"/y, "text-[color:var(--ok)]"],
    [/-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/y, "text-[color:var(--warn)]"],
    [/\b(?:true|false|null)\b/y, "text-accent"],
  ],
  bash: [
    [/#[^\n]*/y, "text-subtle"],
    [/'(?:[^'])*'|"(?:[^"\\]|\\.)*"/y, "text-[color:var(--ok)]"],
    [/(?<=^|\n|\|\s?)[a-z][\w-]*/y, "text-accent"],
    [/--?[\w-]+/y, "text-[color:var(--info)]"],
  ],
  python: [
    [/#[^\n]*/y, "text-subtle"],
    [/[rbf]?"""[\s\S]*?"""|[rbf]?'(?:[^'\\\n]|\\.)*'|[rbf]?"(?:[^"\\\n]|\\.)*"/y, "text-[color:var(--ok)]"],
    [/\b(?:import|from|as|def|return|with|for|in|if|else|elif|async|await|while|try|except|class|None|True|False|lambda|print)\b/y, "text-accent"],
    [/\b\d+(?:\.\d+)?\b/y, "text-[color:var(--warn)]"],
  ],
};

function Highlighted({ code, lang }: { code: string; lang: Lang }) {
  if (lang === "text") return <>{code}</>;
  // Fresh sticky copies: lastIndex is mutated while scanning.
  const rules = TOKEN_RES[lang].map(([re, cls]) => [new RegExp(re.source, "y"), cls] as const);
  const out: ReactNode[] = [];
  let i = 0;
  let plain = "";
  while (i < code.length) {
    let hit = false;
    for (const [re, cls] of rules) {
      re.lastIndex = i;
      const m = re.exec(code);
      if (m && m[0].length > 0) {
        if (plain) {
          out.push(plain);
          plain = "";
        }
        out.push(<span key={i} className={cls}>{m[0]}</span>);
        i += m[0].length;
        hit = true;
        break;
      }
    }
    if (!hit) {
      plain += code[i];
      i++;
    }
  }
  if (plain) out.push(plain);
  return <>{out}</>;
}

// --- Status chips --------------------------------------------------------------------------

const STATE_LABEL: Record<string, string> = {
  waiting_for_input: "waiting for input",
};

const LIVE = new Set(["preparing", "running", "resuming", "pending"]);

export function StateChip({ state, className }: { state: TaskState | string; className?: string }) {
  const tone = STATE_TONE[state] ?? "neutral";
  return (
    <Chip tone={tone} dot pulse={LIVE.has(state) || state === "waiting_for_input"} className={className}>
      {STATE_LABEL[state] ?? state}
    </Chip>
  );
}

export function KeyValue({ items, className }: { items: [ReactNode, ReactNode][]; className?: string }) {
  return (
    <dl className={cn("grid grid-cols-[minmax(110px,auto)_1fr] gap-x-6 gap-y-2.5 text-sm", className)}>
      {items.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0 text-fg">{v}</dd>
        </div>
      ))}
    </dl>
  );
}
