import { bytes, relativeTime } from "@/lib/format";
import { useImages } from "@/lib/queries";
import { SuggestInput } from "./ui/controls";
import { Field, Textarea } from "./ui/primitives";

/**
 * The project image: any image reference, with the images on the worker's daemon as
 * suggestions. Empty means the default project image (docs/runtime-bundles.md §2.3).
 */
export function ImageField({ value, onChange, id = "project-image" }: { value: string; onChange: (v: string) => void; id?: string }) {
  const images = useImages();
  // Alpine tags are musl-based, which runs do not support: flagged and listed last.
  const musl = (tag: string) => /alpine/i.test(tag);
  const suggestions = (images.data ?? [])
    .filter((i) => i.tag && !i.tag.includes("<none>"))
    .sort((a, b) => Number(musl(a.tag)) - Number(musl(b.tag)))
    .map((i) => ({
      value: i.tag,
      description: musl(i.tag)
        ? "Alpine (musl): not supported"
        : [i.size ? bytes(i.size) : null, i.created_at ? `built ${relativeTime(i.created_at)}` : null].filter(Boolean).join(" · ") || undefined,
    }));
  return (
    <Field
      label="Image"
      htmlFor={id}
      optional
      hint={
        <>
          Any glibc-based Linux image with <code className="font-mono">git</code> works, official language images such as{" "}
          <code className="font-mono">rust:1.82</code> or <code className="font-mono">maven:3-eclipse-temurin-21</code> included.
          Alpine and other musl images are not supported. Empty uses the default image (Ubuntu LTS with git and build tools).
        </>
      }
    >
      <SuggestInput id={id} mono value={value} onChange={onChange} suggestions={suggestions} placeholder="Default image" spellCheck={false} />
    </Field>
  );
}

const COMMON_COMMANDS: { ecosystem: string; command: string }[] = [
  { ecosystem: "Node", command: "npm ci" },
  { ecosystem: "Node", command: "pnpm install --frozen-lockfile" },
  { ecosystem: "Python", command: "uv sync" },
  { ecosystem: "Python", command: "pip install -r requirements.txt" },
  { ecosystem: "Go", command: "go mod download" },
  { ecosystem: "Ruby", command: "bundle install" },
  { ecosystem: "PHP", command: "composer install" },
];

/** One command per line; chips append common commands. */
export function SetupCommandsEditor({ value, onChange, folders = [] }: { value: string; onChange: (v: string) => void; folders?: string[] }) {
  const lines = value.split("\n").map((l) => l.trim());
  const append = (cmd: string) => {
    const trimmed = value.replace(/\s+$/, "");
    onChange(trimmed ? `${trimmed}\n${cmd}` : cmd);
  };
  const prefix = folders.length === 1 ? `cd ${folders[0]} && ` : "";
  return (
    <div className="flex flex-col gap-2">
      <Textarea
        mono
        rows={Math.min(8, Math.max(3, lines.length + 1))}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={"cd api && uv sync\ncd web && npm ci"}
        spellCheck={false}
      />
      <div className="flex flex-wrap gap-1.5">
        {COMMON_COMMANDS.map((c) => {
          const cmd = prefix + c.command;
          const present = lines.includes(cmd);
          return (
            <button
              key={c.command}
              type="button"
              disabled={present}
              onClick={() => append(cmd)}
              title={`${c.ecosystem}: append “${cmd}”`}
              className="inline-flex h-6 items-center gap-1 rounded-full border border-border bg-surface px-2 font-mono text-[11px] text-muted transition-colors hover:border-border-strong hover:text-fg disabled:opacity-40"
            >
              <span className="text-subtle">+</span>
              {c.command}
            </button>
          );
        })}
      </div>
    </div>
  );
}
