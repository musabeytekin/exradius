import type { SecretIndex } from "@/lib/secretRefs";
import { SecretRefInput } from "./secrets";
import { Button, Input } from "./ui/primitives";
import { newRow, type Row } from "@/lib/rows";

/** Env or header rows whose values accept `${secret:NAME}` with an insert control. */
export function KeyValueEditor({
  rows, onChange, index, names, keyPlaceholder, valuePlaceholder, projectId, addLabel = "Add", createKeys = {},
}: {
  rows: Row[]; onChange: (rows: Row[]) => void; index: SecretIndex;
  names: { name: string; scope: "global" | "project"; where?: string }[];
  keyPlaceholder: string; valuePlaceholder: string; projectId?: string | null; addLabel?: string;
  /** Suggested secret names per row key, from a catalog entry. */
  createKeys?: Record<string, string>;
}) {
  const update = (id: number, patch: Partial<Row>) => onChange(rows.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  const dupes = new Set(rows.map((r) => r.key.trim()).filter((k, i, a) => k && a.indexOf(k) !== i));
  return (
    <div className="flex flex-col gap-2">
      {rows.map((r) => (
        <div key={r.id} className="grid grid-cols-[minmax(0,0.8fr)_minmax(0,1.6fr)_auto] items-start gap-2">
          <Input mono aria-label="Name" value={r.key} placeholder={keyPlaceholder} invalid={dupes.has(r.key.trim())} onChange={(e) => update(r.id, { key: e.target.value })} />
          <SecretRefInput
            aria-label={`Value of ${r.key || "new entry"}`}
            value={r.value}
            onChange={(v) => update(r.id, { value: v })}
            index={index}
            names={names}
            placeholder={valuePlaceholder}
            projectId={projectId}
            onCreateKey={createKeys[r.key]}
          />
          <Button variant="ghost" square icon="x" aria-label="Remove" onClick={() => onChange(rows.filter((x) => x.id !== r.id))} />
        </div>
      ))}
      <div>
        <Button size="sm" variant="ghost" icon="plus" onClick={() => onChange([...rows, newRow()])}>{addLabel}</Button>
      </div>
    </div>
  );
}
