import { Fragment, type ReactNode } from "react";
import { cn } from "@/lib/cn";
import { CodeBlock } from "./ui/data";

// A small, safe markdown renderer for assistant text and result summaries: headings,
// paragraphs, lists, fenced code, blockquotes, inline code, bold, italics and links.
// It builds React elements; nothing is injected as HTML.

type Block =
  | { t: "code"; lang: string; text: string }
  | { t: "h"; level: number; text: string }
  | { t: "ul" | "ol"; items: string[] }
  | { t: "quote"; text: string }
  | { t: "hr" }
  | { t: "p"; text: string }
  | { t: "table"; rows: string[][] };

function parse(src: string): Block[] {
  const lines = src.replace(/\r\n/g, "\n").split("\n");
  const blocks: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const fence = /^\s*(```|~~~)\s*([\w+-]*)/.exec(line);
    if (fence) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
      i++;
      blocks.push({ t: "code", lang: fence[2], text: body.join("\n") });
      continue;
    }
    if (!line.trim()) {
      i++;
      continue;
    }
    const h = /^(#{1,6})\s+(.*)$/.exec(line);
    if (h) {
      blocks.push({ t: "h", level: h[1].length, text: h[2] });
      i++;
      continue;
    }
    if (/^\s*(?:-\s*){3,}$|^\s*(?:\*\s*){3,}$|^\s*(?:_\s*){3,}$/.test(line)) {
      blocks.push({ t: "hr" });
      i++;
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const rows: string[][] = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        if (!/^\s*\|?\s*:?-{2,}/.test(lines[i])) rows.push(lines[i].trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim()));
        i++;
      }
      blocks.push({ t: "table", rows });
      continue;
    }
    if (/^\s*>/.test(line)) {
      const body: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) body.push(lines[i++].replace(/^\s*>\s?/, ""));
      blocks.push({ t: "quote", text: body.join("\n") });
      continue;
    }
    const ul = /^\s*[-*+]\s+/;
    const ol = /^\s*\d+[.)]\s+/;
    if (ul.test(line) || ol.test(line)) {
      const kind = ul.test(line) ? "ul" : "ol";
      const re = kind === "ul" ? ul : ol;
      const items: string[] = [];
      while (i < lines.length && (re.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
        if (re.test(lines[i])) items.push(lines[i].replace(re, ""));
        else items[items.length - 1] += ` ${lines[i].trim()}`;
        i++;
      }
      blocks.push({ t: kind, items });
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && lines[i].trim() && !/^(#{1,6}\s|\s*```|\s*~~~|\s*>|\s*[-*+]\s+|\s*\d+[.)]\s+)/.test(lines[i])) para.push(lines[i++]);
    if (para.length === 0) para.push(lines[i++]);
    blocks.push({ t: "p", text: para.join("\n") });
  }
  return blocks;
}

const INLINE = /(`[^`]+`)|(\*\*[^*]+\*\*|__[^_]+__)|(\*[^*\s][^*]*\*|_[^_\s][^_]*_)|(\[[^\]]+\]\((https?:\/\/[^)\s]+)\))|(https?:\/\/[^\s<>()]+[^\s<>().,;:!?'"])/g;

function inline(text: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let k = 0;
  for (const m of text.matchAll(INLINE)) {
    if (m.index! > last) out.push(text.slice(last, m.index));
    const [whole] = m;
    if (m[1]) out.push(<code key={k++} className="rounded bg-surface-2 px-1 py-px font-mono text-[0.86em] text-fg">{whole.slice(1, -1)}</code>);
    else if (m[2]) out.push(<strong key={k++} className="font-semibold text-fg">{inline(whole.slice(2, -2))}</strong>);
    else if (m[3]) out.push(<em key={k++}>{inline(whole.slice(1, -1))}</em>);
    else if (m[4]) {
      const label = /^\[([^\]]+)\]/.exec(whole)![1];
      out.push(<a key={k++} href={m[5]} target="_blank" rel="noreferrer noopener" className="text-accent underline-offset-2 hover:underline">{label}</a>);
    } else if (m[6]) out.push(<a key={k++} href={whole} target="_blank" rel="noreferrer noopener" className="break-all text-accent underline-offset-2 hover:underline">{whole}</a>);
    last = m.index! + whole.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

function withBreaks(text: string) {
  const parts = text.split("\n");
  return parts.map((p, i) => (
    <Fragment key={i}>
      {inline(p)}
      {i < parts.length - 1 && <br />}
    </Fragment>
  ));
}

export function Markdown({ text, className }: { text: string; className?: string }) {
  const blocks = parse(text);
  return (
    <div className={cn("flex flex-col gap-2.5 text-[13.5px] leading-[1.65] text-fg", className)}>
      {blocks.map((b, i) => {
        switch (b.t) {
          case "code":
            return <CodeBlock key={i} code={b.text} lang={b.lang === "json" ? "json" : b.lang === "bash" || b.lang === "sh" || b.lang === "shell" ? "bash" : b.lang === "python" || b.lang === "py" ? "python" : "text"} maxHeight={420} />;
          case "h":
            return <div key={i} className={cn("font-semibold text-fg", b.level <= 2 ? "mt-1 text-[15px]" : "text-[13.5px]")}>{inline(b.text)}</div>;
          case "ul":
            return <ul key={i} className="ml-5 flex list-disc flex-col gap-1 marker:text-subtle">{b.items.map((it, j) => <li key={j}>{inline(it)}</li>)}</ul>;
          case "ol":
            return <ol key={i} className="ml-5 flex list-decimal flex-col gap-1 marker:text-subtle">{b.items.map((it, j) => <li key={j}>{inline(it)}</li>)}</ol>;
          case "quote":
            return <blockquote key={i} className="border-l-2 border-border-strong pl-3 text-muted">{withBreaks(b.text)}</blockquote>;
          case "hr":
            return <hr key={i} className="border-border" />;
          case "table":
            return (
              <div key={i} className="overflow-x-auto rounded-md border border-border">
                <table className="w-full text-xs">
                  <tbody>
                    {b.rows.map((r, j) => (
                      <tr key={j} className={cn("border-b border-border last:border-0", j === 0 && "bg-surface-2 font-medium")}>
                        {r.map((c, x) => <td key={x} className="px-2.5 py-1.5">{inline(c)}</td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          default:
            return <p key={i} className="break-words">{withBreaks(b.text)}</p>;
        }
      })}
    </div>
  );
}
