import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { qk, useGlobalSecrets, useProjectSecrets } from "@/lib/queries";
import { findRefs, insertRef, worstOf, type SecretIndex } from "@/lib/secretRefs";
import { ENV_KEY_RE, secretKey } from "@/lib/slug";
import { toast } from "@/lib/toast";
import type { Secret } from "@/lib/types";
import { PickList, Segmented } from "./ui/controls";
import { Icon } from "./ui/icons";
import { Dialog, Popover } from "./ui/overlays";
import { Button, Field, Input, Textarea } from "./ui/primitives";

export function CreateSecretDialog({
  open, onOpenChange, projectId, projectSlug, defaultKey = "", allowScopeChoice = false, onCreated, context,
}: {
  open: boolean; onOpenChange: (o: boolean) => void;
  /** Scope: a project id, or null for a global secret. */
  projectId: string | null; projectSlug?: string;
  defaultKey?: string; allowScopeChoice?: boolean;
  onCreated?: (s: Secret) => void; context?: ReactNode;
}) {
  return open ? (
    <CreateSecretForm
      onOpenChange={onOpenChange} projectId={projectId} projectSlug={projectSlug} defaultKey={defaultKey}
      allowScopeChoice={allowScopeChoice} onCreated={onCreated} context={context}
    />
  ) : null;
}

function CreateSecretForm({
  onOpenChange, projectId, projectSlug, defaultKey, allowScopeChoice, onCreated, context,
}: {
  onOpenChange: (o: boolean) => void; projectId: string | null; projectSlug?: string; defaultKey: string;
  allowScopeChoice: boolean; onCreated?: (s: Secret) => void; context?: ReactNode;
}) {
  const qc = useQueryClient();
  const [scope, setScope] = useState<"project" | "global">(projectId ? "project" : "global");
  const pid = scope === "project" ? projectId : null;
  const existing = useProjectSecrets(pid);
  const globals = useGlobalSecrets();
  const [key, setKey] = useState(secretKey(defaultKey));
  const [value, setValue] = useState("");
  const taken = (pid ? existing.data : globals.data)?.some((s) => s.key === key);
  const keyError = !key ? null : !ENV_KEY_RE.test(key) ? "Letters, digits and underscores; not starting with a digit." : taken ? `${key} already exists here.` : null;

  const create = useMutation({
    mutationFn: () => api.secrets.create(pid, { key, value }),
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: pid ? qk.projectSecrets(pid) : qk.globalSecrets });
      qc.invalidateQueries({ queryKey: qk.status });
      toast.ok(`Secret ${s.key} created`, pid ? `In ${projectSlug ?? "this project"}.` : "Global: usable by reference everywhere.");
      onCreated?.(s);
      onOpenChange(false);
    },
    onError: (e) => toast.err("Could not create the secret", e),
  });

  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title="Create a secret"
      description={context ?? "The value is encrypted at rest and never shown again."}
      onSubmit={() => !keyError && key && value && create.mutate()}
      width={480}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={create.isPending} disabled={!key || !value || !!keyError}>Create secret</Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {allowScopeChoice && projectId && (
          <Field label="Scope">
            <Segmented
              value={scope}
              onChange={setScope}
              options={[
                { value: "project", label: `Project${projectSlug ? ` · ${projectSlug}` : ""}` },
                { value: "global", label: "Global" },
              ]}
            />
          </Field>
        )}
        <Field label="Key" htmlFor="secret-key" error={keyError} hint={pid ? "Exported into the run container's environment under this name." : "Global secrets are used only by reference: ${secret:NAME}."}>
          <Input id="secret-key" mono value={key} onChange={(e) => setKey(secretKey(e.target.value))} placeholder="GITHUB_TOKEN" autoFocus={!defaultKey} invalid={!!keyError} />
        </Field>
        <Field label="Value" htmlFor="secret-value" hint="Write-only. Paste it once; Exradius never returns it.">
          <Textarea
            id="secret-value" mono rows={3} value={value} onChange={(e) => setValue(e.target.value)}
            autoFocus={!!defaultKey} autoComplete="off" spellCheck={false}
            className="[-webkit-text-security:disc]"
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
          />
        </Field>
      </div>
    </Dialog>
  );
}

/** Pick list of secrets (project first, then global) with inline create. */
export function SecretPicker({
  projectId, projectSlug, value, onChange, noneLabel = "None (public repository)", defaultCreateKey, invalid, id,
}: {
  projectId: string; projectSlug?: string; value: string | null; onChange: (id: string | null) => void;
  noneLabel?: string; defaultCreateKey?: string; invalid?: boolean; id?: string;
}) {
  const project = useProjectSecrets(projectId);
  const globals = useGlobalSecrets();
  const [creating, setCreating] = useState(false);
  const options = [
    ...(project.data ?? []).map((s) => ({ value: s.id, label: s.key, group: "This project" })),
    ...(globals.data ?? []).map((s) => ({ value: s.id, label: s.key, group: "Global" })),
  ];
  return (
    <>
      <PickList
        id={id}
        mono
        value={value}
        onChange={onChange}
        options={options}
        noneLabel={noneLabel}
        invalid={invalid}
        actions={[{ key: "create", label: "Create a secret…", onSelect: () => setCreating(true) }]}
      />
      <CreateSecretDialog
        open={creating}
        onOpenChange={setCreating}
        projectId={projectId}
        projectSlug={projectSlug}
        defaultKey={defaultCreateKey}
        allowScopeChoice
        onCreated={(s) => onChange(s.id)}
      />
    </>
  );
}

/**
 * A value field for connection env and headers: free text, plus an "insert secret"
 * control that inserts `${secret:NAME}` at the caret, plus inline create. Unknown and
 * malformed references are flagged on the field.
 */
export function SecretRefInput({
  value, onChange, index, names, placeholder, id, projectId, onCreateKey, "aria-label": ariaLabel,
}: {
  value: string; onChange: (v: string) => void; index: SecretIndex;
  /** Names offered by the insert control, in order. */
  names: { name: string; scope: "global" | "project"; where?: string }[];
  placeholder?: string; id?: string; projectId?: string | null;
  /** Prefill for the inline create (e.g. from a catalog entry). */
  onCreateKey?: string; "aria-label"?: string;
}) {
  const ref = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [filter, setFilter] = useState("");
  const refs = findRefs(value, index);
  const worst = worstOf(refs);
  const insert = (name: string) => {
    const caret = ref.current?.selectionStart ?? value.length;
    const next = insertRef(value, name, caret);
    onChange(next.value);
    setOpen(false);
    requestAnimationFrame(() => {
      ref.current?.focus();
      ref.current?.setSelectionRange(next.caret, next.caret);
    });
  };
  const shown = names.filter((n) => n.name.toLowerCase().includes(filter.toLowerCase()));

  return (
    <div className="flex min-w-0 flex-col gap-1">
      <div className="flex gap-1.5">
        <Input
          ref={ref}
          id={id}
          aria-label={ariaLabel}
          mono
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={placeholder}
          invalid={worst === "invalid" || worst === "unknown"}
          className={cn("min-w-0 flex-1", worst === "project" && "border-warn")}
          spellCheck={false}
          autoComplete="off"
        />
        <Popover
          open={open}
          onOpenChange={(o) => {
            setOpen(o);
            setFilter("");
          }}
          align="end"
          width={280}
          trigger={
            <Button size="md" variant="secondary" square icon="key" aria-label="Insert a secret reference" title="Insert ${secret:NAME}" />
          }
        >
          <div className="p-1">
            <Input autoFocus value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Find a secret…" className="mb-1" />
            <div className="max-h-56 overflow-y-auto">
              {shown.length === 0 && <div className="px-2 py-3 text-center text-xs text-subtle">No secrets{filter ? " match" : " yet"}</div>}
              {shown.map((n) => (
                <button
                  key={`${n.scope}:${n.name}`}
                  type="button"
                  onClick={() => insert(n.name)}
                  className="flex h-8 w-full items-center justify-between gap-2 rounded-md px-2 text-left hover:bg-surface-2"
                >
                  <span className="font-mono text-[12.5px] text-fg">{n.name}</span>
                  <span className="truncate text-2xs text-subtle">{n.scope === "global" ? "global" : n.where}</span>
                </button>
              ))}
            </div>
            <div className="mt-1 border-t border-border pt-1">
              <button
                type="button"
                onClick={() => {
                  setOpen(false);
                  setCreating(true);
                }}
                className="flex h-8 w-full items-center gap-2 rounded-md px-2 text-sm text-accent hover:bg-accent-soft"
              >
                <Icon name="plus" size={14} /> Create a secret…
              </button>
            </div>
          </div>
        </Popover>
      </div>
      {refs.filter((r) => r.status !== "global").map((r, i) => (
        <p key={i} className={cn("flex items-start gap-1 text-xs", r.status === "project" ? "text-warn" : "text-err")}>
          <Icon name="alert" size={12} className="mt-0.5 shrink-0" />
          <span>
            {r.message}
            {r.status === "unknown" && (
              <button type="button" className="ml-1.5 font-medium underline underline-offset-2" onClick={() => setCreating(true)}>
                Create it
              </button>
            )}
          </span>
        </p>
      ))}
      <CreateSecretDialog
        open={creating}
        onOpenChange={setCreating}
        projectId={projectId ?? null}
        allowScopeChoice={!!projectId}
        defaultKey={refs.find((r) => r.status === "unknown")?.name ?? onCreateKey ?? ""}
        onCreated={(s) => {
          if (!refs.some((r) => r.name === s.key)) insert(s.key);
        }}
      />
    </div>
  );
}
