import { useState } from "react";
import { CopyButton } from "./ui/data";
import { Icon } from "./ui/icons";

/** The generated `# Your environment` section, read-only and collapsed until opened. */
export function EnvironmentPreview({ text, defaultOpen = false, title = "What the agent is told about this project" }: {
  text: string; defaultOpen?: boolean; title?: string;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="rounded-lg border border-border bg-surface">
      <div className="flex items-center gap-2 px-3 py-2">
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          className="flex min-w-0 flex-1 items-center gap-1.5 text-left text-xs font-medium text-muted hover:text-fg"
        >
          <Icon name="chevronRight" size={12} className={`shrink-0 transition-transform ${open ? "rotate-90" : ""}`} />
          {title}
        </button>
        {open && <CopyButton text={text} />}
      </div>
      {open && (
        <pre
          aria-label={title}
          className="max-h-96 overflow-auto border-t border-border bg-code px-3.5 py-3 font-mono text-[11.5px] leading-[1.65] text-fg whitespace-pre-wrap break-words"
        >
          {text}
        </pre>
      )}
    </div>
  );
}
