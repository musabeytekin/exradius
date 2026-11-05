import { useRef, useState } from "react";
import { cn } from "@/lib/cn";
import { bytes } from "@/lib/format";
import { Icon } from "./ui/icons";
import { Button } from "./ui/primitives";

/** File picker with drop support. Names and content types come from the files. */
export function FileDrop({ files, onChange, compact }: { files: File[]; onChange: (f: File[]) => void; compact?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const add = (list: FileList | null) => {
    if (!list) return;
    const next = [...files];
    for (const f of Array.from(list)) if (!next.some((x) => x.name === f.name)) next.push(f);
    onChange(next);
  };
  return (
    <div className="flex flex-col gap-2">
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          add(e.dataTransfer.files);
        }}
        className={cn(
          "flex items-center gap-3 rounded-md border border-dashed px-3 text-sm text-muted transition-colors",
          compact ? "py-2" : "py-3",
          over ? "border-accent bg-accent-soft/50" : "border-border-strong",
        )}
      >
        <Icon name="upload" size={15} className="text-subtle" />
        <span className="flex-1">Drop files here, or</span>
        <Button size="sm" variant="secondary" onClick={() => input.current?.click()}>Choose files</Button>
        <input ref={input} type="file" multiple hidden onChange={(e) => { add(e.target.files); e.target.value = ""; }} />
      </div>
      {files.length > 0 && (
        <ul className="flex flex-col gap-1">
          {files.map((f) => (
            <li key={f.name} className="flex items-center gap-2 rounded-md bg-surface-2 px-2.5 py-1.5 text-xs">
              <Icon name="file" size={13} className="text-subtle" />
              <span className="min-w-0 flex-1 truncate font-mono">{f.name}</span>
              <span className="text-subtle">{f.type || "application/octet-stream"}</span>
              <span className="tabular text-subtle">{bytes(f.size)}</span>
              <button type="button" aria-label={`Remove ${f.name}`} onClick={() => onChange(files.filter((x) => x !== f))} className="text-subtle hover:text-fg">
                <Icon name="x" size={13} />
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
