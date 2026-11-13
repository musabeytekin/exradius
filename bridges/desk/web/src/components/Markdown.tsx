import { Fragment, type ReactNode } from "react";
import { pieces } from "@/lib/mentions";

// A small, safe Markdown renderer: paragraphs, headings, lists, quotes, fenced code, inline
// code, bold, italics, links and mentions. It builds React elements; nothing is injected as HTML.

type Block =
  | { t: "code"; text: string }
  | { t: "h"; text: string }
  | { t: "ul" | "ol"; items: string[] }
  | { t: "quote"; text: string }
  | { t: "p"; text: string };

function blocks(src: string): Block[] {
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  const out: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = /^\s*(```|~~~)/.exec(line);
    if (fence) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
      i++;
      out.push({ t: "code", text: body.join("\n") });
    } else if (!line.trim()) {
      i++;
    } else if (/^#{1,6}\s+/.test(line)) {
      out.push({ t: "h", text: line.replace(/^#{1,6}\s+/, "") });
      i++;
    } else if (/^\s*[-*+]\s+/.test(line) || /^\s*\d+[.)]\s+/.test(line)) {
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const pattern = ordered ? /^\s*\d+[.)]\s+/ : /^\s*[-*+]\s+/;
      const items: string[] = [];
      while (i < lines.length && pattern.test(lines[i])) items.push(lines[i++].replace(pattern, ""));
      out.push({ t: ordered ? "ol" : "ul", items });
    } else if (/^\s*>/.test(line)) {
      const body: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) body.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push({ t: "quote", text: body.join("\n") });
    } else {
      const body: string[] = [];
      while (i < lines.length && lines[i].trim() && !/^\s*(```|~~~|#{1,6}\s|>|[-*+]\s|\d+[.)]\s)/.test(lines[i])) {
        body.push(lines[i++]);
      }
      if (!body.length) body.push(lines[i++]);
      out.push({ t: "p", text: body.join("\n") });
    }
  }
  return out;
}

const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\s][^*]*\*|_[^_\s][^_]*_|\[[^\]]+\]\([^)\s]+\)|https?:\/\/[^\s)]+)/g;

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let n = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const s = m[0];
    const k = `${key}-${n++}`;
    if (s.startsWith("`")) out.push(<code key={k} className="rounded bg-code px-1 py-0.5 text-[0.85em]">{s.slice(1, -1)}</code>);
    else if (s.startsWith("**")) out.push(<strong key={k}>{s.slice(2, -2)}</strong>);
    else if (s.startsWith("*") || s.startsWith("_")) out.push(<em key={k}>{s.slice(1, -1)}</em>);
    else {
      const link = /^\[([^\]]+)\]\(([^)\s]+)\)$/.exec(s);
      const href = link ? link[2] : s;
      const label = link ? link[1] : s;
      out.push(/^https?:\/\//.test(href)
        ? <a key={k} href={href} target="_blank" rel="noreferrer noopener" className="text-accent underline-offset-2 hover:underline">{label}</a>
        : label);
    }
    last = m.index + s.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function withMentions(text: string, key: string, names: Record<string, string>): ReactNode[] {
  return pieces(text, names).flatMap((p, i) =>
    "text" in p
      ? inline(p.text, `${key}.${i}`).map((node, j) => <Fragment key={`${key}.${i}.${j}`}>{node}</Fragment>)
      : [<span key={`${key}.${i}`} className={p.exradius ? "font-medium text-accent" : "rounded bg-accent-soft px-1 font-medium text-accent"}>@{p.mention}</span>],
  );
}

function lines(text: string, key: string, names: Record<string, string>): ReactNode[] {
  return text.split("\n").flatMap((line, i) => [
    ...(i ? [<br key={`${key}br${i}`} />] : []),
    ...withMentions(line, `${key}l${i}`, names),
  ]);
}

export function Markdown({ text, names = {} }: { text: string; names?: Record<string, string> }) {
  return (
    <div className="space-y-2 break-words text-base leading-relaxed sm:text-sm">
      {blocks(text).map((b, i) => {
        const k = `b${i}`;
        switch (b.t) {
          case "code":
            return <pre key={k} className="overflow-x-auto rounded-md bg-code p-3 font-mono text-xs leading-5">{b.text}</pre>;
          case "h":
            return <p key={k} className="font-semibold">{lines(b.text, k, names)}</p>;
          case "ul":
          case "ol": {
            const List = b.t;
            return (
              <List key={k} className={b.t === "ul" ? "list-disc space-y-0.5 pl-5" : "list-decimal space-y-0.5 pl-5"}>
                {b.items.map((item, j) => <li key={j}>{lines(item, `${k}i${j}`, names)}</li>)}
              </List>
            );
          }
          case "quote":
            return <blockquote key={k} className="border-l-2 border-border-strong pl-3 text-muted">{lines(b.text, k, names)}</blockquote>;
          default:
            return <p key={k}>{lines(b.text, k, names)}</p>;
        }
      })}
    </div>
  );
}
