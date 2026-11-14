import { useState, type KeyboardEvent, type RefObject } from "react";
import { cn } from "@/lib/cn";
import { matching, typing, type Candidate } from "@/lib/mentions";
import { Avatar } from "./ui";
import { inputClass } from "@/lib/styles";

/** A textarea that offers people (and Exradius, when a mention would do something) after `@`. */
export function MentionBox({ value, onChange, candidates, onChoose, textarea, placeholder, rows = 3, label }: {
  value: string;
  onChange: (value: string) => void;
  candidates: Candidate[];
  onChoose: (candidate: Candidate) => void;
  textarea: RefObject<HTMLTextAreaElement | null>;
  placeholder?: string;
  rows?: number;
  label: string;
}) {
  const [caret, setCaret] = useState(0);
  const [active, setActive] = useState(0);
  const [closed, setClosed] = useState(false);
  const at = typing(value, caret);
  const options = at && !closed ? matching(candidates, at.query) : [];

  function choose(candidate: Candidate) {
    if (!at) return;
    const next = `${value.slice(0, at.start)}@${candidate.name} ${value.slice(caret)}`;
    const position = at.start + candidate.name.length + 2;
    onChange(next);
    onChoose(candidate);
    setCaret(position);
    requestAnimationFrame(() => {
      textarea.current?.focus();
      textarea.current?.setSelectionRange(position, position);
    });
  }

  function onKey(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (!options.length) return;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setActive((a) => (a + (e.key === "ArrowDown" ? 1 : options.length - 1)) % options.length);
    } else if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      choose(options[Math.min(active, options.length - 1)]);
    } else if (e.key === "Escape") {
      setClosed(true);
    }
  }

  return (
    <div className="relative">
      <textarea
        ref={textarea}
        aria-label={label}
        rows={rows}
        value={value}
        placeholder={placeholder}
        className={cn(inputClass, "min-h-[4.5rem] resize-y")}
        onChange={(e) => {
          onChange(e.target.value);
          setCaret(e.target.selectionStart ?? e.target.value.length);
          setClosed(false);
          setActive(0);
        }}
        onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
        onKeyDown={onKey}
      />
      {options.length > 0 && (
        <ul role="listbox" aria-label="Mention"
          className="absolute left-0 z-20 mt-1 max-h-60 w-full max-w-xs overflow-y-auto rounded-md border border-border bg-surface p-1 shadow-pop">
          {options.map((c, i) => (
            <li key={c.token} role="option" aria-selected={i === active}>
              <button
                type="button"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => choose(c)}
                className={cn("flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm",
                  i === active ? "bg-surface-2" : "hover:bg-surface-2")}
              >
                <Avatar name={c.name} exradius={c.exradius} />
                {c.name}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
