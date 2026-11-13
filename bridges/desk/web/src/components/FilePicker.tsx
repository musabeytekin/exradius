import { useRef } from "react";
import { size } from "@/lib/format";
import { Button } from "./ui";

export function FilePicker({ files, onChange, label = "Attach files" }:
  { files: File[]; onChange: (files: File[]) => void; label?: string }) {
  const input = useRef<HTMLInputElement>(null);
  return (
    <div className="flex flex-wrap items-center gap-2">
      <input
        ref={input}
        type="file"
        multiple
        className="hidden"
        aria-label={label}
        onChange={(e) => {
          onChange([...files, ...Array.from(e.target.files ?? [])]);
          e.target.value = "";
        }}
      />
      <Button small onClick={() => input.current?.click()}>{label}</Button>
      {files.map((f, i) => (
        <span key={`${f.name}-${i}`} className="inline-flex items-center gap-1 rounded bg-surface-2 px-2 py-0.5 text-xs text-muted">
          {f.name} <span className="text-subtle">{size(f.size)}</span>
          <button type="button" aria-label={`Remove ${f.name}`} className="ml-0.5 text-subtle hover:text-fg"
            onClick={() => onChange(files.filter((_, j) => j !== i))}>×</button>
        </span>
      ))}
    </div>
  );
}
