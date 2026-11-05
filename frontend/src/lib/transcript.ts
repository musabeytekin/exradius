// Turns stored transcript chunks (one Claude Agent SDK message each, serialised by the
// runner as dataclass fields plus `type`) into a conversation timeline.

import type { SdkMessage, TranscriptChunk } from "./types";

export interface ToolResult {
  content: unknown;
  text: string;
  isError: boolean;
  /** `tool_use_result` from the UserMessage, when the SDK provides structured output. */
  structured: unknown;
}

export type TranscriptItem =
  | { kind: "session"; key: string; seq: number; model: string | null; cwd: string | null; tools: string[]; mcpServers: { name: string; status: string }[]; raw: SdkMessage }
  | { kind: "system"; key: string; seq: number; subtype: string; summary: string; raw: SdkMessage }
  | { kind: "text"; key: string; seq: number; role: "assistant" | "user"; text: string; raw: SdkMessage }
  | { kind: "thinking"; key: string; seq: number; text: string; raw: SdkMessage }
  | {
      kind: "tool";
      key: string;
      seq: number;
      id: string;
      name: string;
      server: string | null;
      tool: string;
      input: Record<string, unknown>;
      summary: string;
      result: ToolResult | null;
      children: TranscriptItem[];
      raw: SdkMessage;
    }
  | {
      kind: "result";
      key: string;
      seq: number;
      subtype: string;
      isError: boolean;
      /** Ended by an interrupt (the runner pausing for an answer), not by a failure. */
      interrupted: boolean;
      text: string | null;
      durationMs: number | null;
      costUsd: number | null;
      turns: number | null;
      usage: Record<string, unknown> | null;
      errors: string[];
      raw: SdkMessage;
    }
  | { kind: "error"; key: string; seq: number; text: string; raw: SdkMessage }
  | { kind: "unknown"; key: string; seq: number; type: string; raw: SdkMessage };

type Block = { type?: string } & Record<string, unknown>;

const str = (v: unknown): string | null => (typeof v === "string" ? v : null);
const num = (v: unknown): number | null => (typeof v === "number" ? v : null);

/** `mcp__github__create_issue` → server `github`, tool `create_issue`. */
export function splitToolName(name: string): { server: string | null; tool: string } {
  const m = /^mcp__(.+?)__(.+)$/.exec(name);
  return m ? { server: m[1], tool: m[2] } : { server: null, tool: name };
}

const SUMMARY_KEYS = [
  "command", "file_path", "path", "notebook_path", "pattern", "url", "query",
  "question", "description", "prompt", "skill", "subject", "title", "name",
];

/** The one-line form of a tool call: its most telling argument. */
export function summarizeInput(input: Record<string, unknown>): string {
  for (const key of SUMMARY_KEYS) {
    const v = input[key];
    if (typeof v === "string" && v.trim()) return oneLine(v);
  }
  for (const v of Object.values(input)) {
    if (typeof v === "string" && v.trim()) return oneLine(v);
    if (typeof v === "number" || typeof v === "boolean") return String(v);
  }
  const keys = Object.keys(input);
  return keys.length ? `{ ${keys.join(", ")} }` : "";
}

function oneLine(s: string): string {
  const line = s.replace(/\s+/g, " ").trim();
  return line.length > 160 ? `${line.slice(0, 159)}…` : line;
}

/** Tool result content is a string or a list of content blocks. */
export function resultText(content: unknown): string {
  if (content === null || content === undefined) return "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((b: Block) => {
        if (b && typeof b === "object") {
          if (typeof b.text === "string") return b.text;
          if (b.type === "image") return "[image]";
        }
        return typeof b === "string" ? b : JSON.stringify(b);
      })
      .join("\n");
  }
  return JSON.stringify(content, null, 2);
}

function blocksOf(msg: SdkMessage): Block[] {
  const c = msg.content;
  if (Array.isArray(c)) return c as Block[];
  if (typeof c === "string") return [{ type: "TextBlock", text: c }];
  // Some serialisations nest the API message: { message: { content: [...] } }.
  const inner = (msg.message as { content?: unknown } | undefined)?.content;
  if (Array.isArray(inner)) return inner as Block[];
  if (typeof inner === "string") return [{ type: "TextBlock", text: inner }];
  return [];
}

const isType = (b: Block, ...names: string[]) => names.includes(String(b.type));

/** Build the timeline. Subagent messages (with `parent_tool_use_id`) nest under the tool call that started them. */
export function buildTranscript(chunks: TranscriptChunk[]): TranscriptItem[] {
  const sorted = [...chunks].sort((a, b) => a.seq - b.seq);
  const root: TranscriptItem[] = [];
  const tools = new Map<string, Extract<TranscriptItem, { kind: "tool" }>>();

  const containerFor = (msg: SdkMessage): TranscriptItem[] => {
    const parent = str(msg.parent_tool_use_id);
    return (parent && tools.get(parent)?.children) || root;
  };

  for (const { seq, payload: msg } of sorted) {
    const type = String(msg.type ?? "");
    const base = (i: number | string) => `${seq}:${i}`;

    if (type === "SystemMessage") {
      const subtype = str(msg.subtype) ?? "system";
      // Progress estimates while the model thinks: nothing to read.
      if (subtype === "thinking_tokens") continue;
      const data = (msg.data ?? {}) as Record<string, unknown>;
      if (subtype === "init") {
        root.push({
          kind: "session", key: base(0), seq,
          model: str(data.model), cwd: str(data.cwd),
          tools: Array.isArray(data.tools) ? (data.tools as string[]) : [],
          mcpServers: Array.isArray(data.mcp_servers) ? (data.mcp_servers as { name: string; status: string }[]) : [],
          raw: msg,
        });
      } else {
        const summary = str(msg.description) ?? str(data.description) ?? str(data.message) ?? str(msg.status) ?? "";
        containerFor(msg).push({ kind: "system", key: base(0), seq, subtype, summary, raw: msg });
      }
      continue;
    }

    if (type === "AssistantMessage") {
      const target = containerFor(msg);
      if (msg.error) {
        target.push({ kind: "error", key: base("e"), seq, text: `Assistant error: ${String(msg.error)}`, raw: msg });
      }
      blocksOf(msg).forEach((b, i) => {
        if (isType(b, "TextBlock", "text")) {
          const text = str(b.text) ?? "";
          if (text.trim()) target.push({ kind: "text", key: base(i), seq, role: "assistant", text, raw: msg });
        } else if (isType(b, "ThinkingBlock", "thinking")) {
          const text = str(b.thinking) ?? "";
          if (text.trim()) target.push({ kind: "thinking", key: base(i), seq, text, raw: msg });
        } else if (isType(b, "ToolUseBlock", "ServerToolUseBlock", "tool_use", "server_tool_use")) {
          const id = str(b.id) ?? base(i);
          const name = str(b.name) ?? "tool";
          const input = (b.input && typeof b.input === "object" ? b.input : {}) as Record<string, unknown>;
          const item = {
            kind: "tool" as const, key: `tool:${id}`, seq, id, name, ...splitToolName(name),
            input, summary: summarizeInput(input), result: null, children: [], raw: msg,
          };
          tools.set(id, item);
          target.push(item);
        } else if (isType(b, "ToolResultBlock", "ServerToolResultBlock", "tool_result")) {
          attachResult(b, msg, tools, target, base(i), seq);
        }
      });
      continue;
    }

    if (type === "UserMessage") {
      const target = containerFor(msg);
      blocksOf(msg).forEach((b, i) => {
        if (isType(b, "ToolResultBlock", "tool_result")) {
          attachResult(b, msg, tools, target, base(i), seq);
        } else if (isType(b, "TextBlock", "text")) {
          const text = str(b.text) ?? "";
          if (/^\[Request interrupted by user/.test(text.trim())) {
            // The runner interrupts the session right after request_information.
            target.push({ kind: "system", key: base(i), seq, subtype: "paused", summary: "The session stopped here to wait for the answer.", raw: msg });
            return;
          }
          // The subagent's prompt is already shown as the tool call's input.
          if (text.trim() && !msg.parent_tool_use_id) target.push({ kind: "text", key: base(i), seq, role: "user", text, raw: msg });
        }
      });
      continue;
    }

    if (type === "ResultMessage") {
      root.push({
        kind: "result", key: base(0), seq,
        subtype: str(msg.subtype) ?? "result",
        isError: msg.is_error === true,
        interrupted: /^aborted/.test(String(msg.terminal_reason ?? "")),
        text: str(msg.result),
        durationMs: num(msg.duration_ms),
        costUsd: num(msg.total_cost_usd),
        turns: num(msg.num_turns),
        usage: (msg.usage as Record<string, unknown>) ?? null,
        errors: Array.isArray(msg.errors) ? (msg.errors as string[]) : [],
        raw: msg,
      });
      continue;
    }

    // Partial stream events and rate-limit notices carry nothing worth a row.
    if (type === "StreamEvent" || type === "RateLimitEvent") continue;
    root.push({ kind: "unknown", key: base(0), seq, type: type || "message", raw: msg });
  }
  return root;
}

function attachResult(
  b: Block,
  msg: SdkMessage,
  tools: Map<string, Extract<TranscriptItem, { kind: "tool" }>>,
  target: TranscriptItem[],
  key: string,
  seq: number,
) {
  const id = str(b.tool_use_id);
  const result: ToolResult = {
    content: b.content,
    text: resultText(b.content),
    isError: b.is_error === true,
    structured: msg.tool_use_result ?? null,
  };
  const tool = id ? tools.get(id) : undefined;
  if (tool) tool.result = result;
  else target.push({ kind: "unknown", key, seq, type: "orphan tool result", raw: msg });
}

/** Flatten for search and counts. */
export function walk(items: TranscriptItem[], fn: (item: TranscriptItem, depth: number) => void, depth = 0) {
  for (const it of items) {
    fn(it, depth);
    if (it.kind === "tool") walk(it.children, fn, depth + 1);
  }
}

export function stats(items: TranscriptItem[]) {
  let tools = 0;
  let errors = 0;
  let messages = 0;
  walk(items, (it) => {
    if (it.kind === "tool") {
      tools++;
      if (it.result?.isError) errors++;
    }
    if (it.kind === "text" && it.role === "assistant") messages++;
  });
  return { tools, errors, messages };
}

/** Does an item (or anything under it) mention the query? */
export function matches(item: TranscriptItem, q: string): boolean {
  if (!q) return true;
  const needle = q.toLowerCase();
  const hay = (s: unknown) => (typeof s === "string" ? s : JSON.stringify(s ?? "")).toLowerCase().includes(needle);
  switch (item.kind) {
    case "text":
    case "thinking":
    case "error":
      return hay(item.text);
    case "tool":
      return hay(item.name) || hay(item.input) || hay(item.result?.text) || item.children.some((c) => matches(c, q));
    case "result":
      return hay(item.text);
    case "system":
      return hay(item.subtype) || hay(item.summary);
    default:
      return hay(item.raw);
  }
}

export const EXRADIUS_TOOLS = {
  request: "mcp__exradius__request_information",
  submit: "mcp__exradius__submit_result",
};
