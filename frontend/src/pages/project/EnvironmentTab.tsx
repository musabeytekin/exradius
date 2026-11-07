import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ImageField, SetupCommandsEditor } from "@/components/projectFields";
import { Table } from "@/components/ui/controls";
import { Button, Card, CardHeader, EmptyState, Field, Input, Mono, SkeletonRows } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { useConfirm } from "@/lib/confirm";
import { qk, useEnvVars, useProjectSecrets, useRepositories, useServices } from "@/lib/queries";
import { serviceExample } from "@/lib/services";
import { ENV_KEY_RE } from "@/lib/slug";
import { toast } from "@/lib/toast";
import type { EnvVar, Project } from "@/lib/types";

export function EnvironmentTab({ project }: { project: Project }) {
  return (
    <div className="flex flex-col gap-6">
      <RuntimeCard project={project} />
      <EnvVarsCard project={project} />
    </div>
  );
}

function RuntimeCard({ project }: { project: Project }) {
  const qc = useQueryClient();
  const repos = useRepositories(project.id);
  const [image, setImage] = useState(project.image ?? "");
  const [setup, setSetup] = useState(project.setup_commands.join("\n"));
  const commands = setup.split("\n").map((l) => l.trim()).filter(Boolean);
  const dirty = image !== (project.image ?? "") || commands.join("\n") !== project.setup_commands.join("\n");
  const save = useMutation({
    mutationFn: () => api.projects.update(project.id, { image: image.trim() || null, setup_commands: commands }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok("Runtime saved", "Verify the project to run the setup commands once.");
    },
    onError: (e) => toast.err("Could not save", e),
  });
  return (
    <Card>
      <CardHeader
        title="Runtime"
        description="The image every run of this project starts from, and the commands that prepare it."
        actions={
          <>
            {dirty && (
              <Button size="sm" variant="ghost" onClick={() => { setImage(project.image ?? ""); setSetup(project.setup_commands.join("\n")); }}>
                Discard
              </Button>
            )}
            <Button size="sm" variant="primary" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate()}>Save</Button>
          </>
        }
      />
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)] gap-6 p-4">
        <ImageField value={image} onChange={setImage} />
        <Field label="Setup commands" hint="One per line, run in /work in this image. Not repeated on resume.">
          <SetupCommandsEditor value={setup} onChange={setSetup} folders={repos.data?.map((r) => r.name)} />
        </Field>
      </div>
    </Card>
  );
}

function EnvVarsCard({ project }: { project: Project }) {
  const vars = useEnvVars(project.id);
  const secrets = useProjectSecrets(project.id);
  const services = useServices(project.id);
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const invalidate = () => qc.invalidateQueries({ queryKey: qk.envVars(project.id) });

  const keyTaken = vars.data?.some((v) => v.key === key);
  // One variable per service pointing at it, offered until a variable of that name exists.
  const snippets = (services.data ?? [])
    .map((sv) => ({ service: sv.name, ex: serviceExample(sv) }))
    .filter((x): x is { service: string; ex: { key: string; value: string } } => !!x.ex && !vars.data?.some((v) => v.key === x.ex!.key));
  const shadow = secrets.data?.some((s) => s.key === key);
  const keyError = !key ? null : !ENV_KEY_RE.test(key) ? "Letters, digits and underscores; not starting with a digit." : keyTaken ? `${key} is already set.` : null;

  const add = useMutation({
    mutationFn: () => api.envVars.create(project.id, { key, value }),
    onSuccess: () => {
      invalidate();
      setKey("");
      setValue("");
      document.getElementById("env-key")?.focus();
    },
    onError: (e) => toast.err("Could not add the variable", e),
  });
  const update = useMutation({
    mutationFn: (v: EnvVar) => api.envVars.update(project.id, v.id, { value: draft }),
    onSuccess: () => {
      invalidate();
      setEditing(null);
    },
    onError: (e) => toast.err("Could not save the variable", e),
  });
  const remove = useMutation({
    mutationFn: (v: EnvVar) => api.envVars.remove(project.id, v.id),
    onSuccess: (_, v) => {
      invalidate();
      toast({
        title: `${v.key} removed`,
        tone: "ok",
        action: { label: "Undo", onClick: () => api.envVars.create(project.id, { key: v.key, value: v.value }).then(invalidate) },
      });
    },
    onError: (e) => toast.err("Could not remove the variable", e),
  });

  return (
    <Card>
      <CardHeader
        title="Environment variables"
        description="Plain values set in every run container. Use Secrets for anything sensitive; these are shown in full."
      />
      {vars.isLoading ? (
        <SkeletonRows rows={2} />
      ) : (
        <Table.Root>
          {!!vars.data?.length && (
            <Table.Head>
              <tr>
                <Table.Th className="w-[34%]">Key</Table.Th>
                <Table.Th>Value</Table.Th>
                <Table.Th />
              </tr>
            </Table.Head>
          )}
          <tbody>
            {vars.data?.map((v) => (
              <Table.Row key={v.id}>
                <Table.Td><Mono className="font-medium">{v.key}</Mono></Table.Td>
                <Table.Td>
                  {editing === v.id ? (
                    <form className="flex gap-2" onSubmit={(e) => { e.preventDefault(); update.mutate(v); }}>
                      <Input
                        mono
                        autoFocus
                        value={draft}
                        onChange={(e) => setDraft(e.target.value)}
                        onKeyDown={(e) => e.key === "Escape" && (e.stopPropagation(), setEditing(null))}
                        className="flex-1"
                      />
                      <Button size="md" type="submit" variant="primary" loading={update.isPending}>Save</Button>
                    </form>
                  ) : (
                    <button
                      type="button"
                      onClick={() => { setEditing(v.id); setDraft(v.value); }}
                      className="block w-full truncate rounded px-1 -mx-1 text-left font-mono text-[12px] text-muted hover:bg-surface-2 hover:text-fg"
                      title="Click to edit"
                    >
                      {v.value || <span className="italic text-subtle">empty</span>}
                    </button>
                  )}
                </Table.Td>
                <Table.Td className="w-[1%] text-right">
                  <Button
                    size="sm"
                    variant="ghost"
                    square
                    icon="trash"
                    aria-label={`Remove ${v.key}`}
                    onClick={async () => {
                      if (await confirm({ title: `Remove ${v.key}?`, description: "Runs started after this will not have it. You can undo right after.", confirmLabel: "Remove" })) remove.mutate(v);
                    }}
                  />
                </Table.Td>
              </Table.Row>
            ))}
            {!vars.data?.length && (
              <tr>
                <td colSpan={3}>
                  <EmptyState compact icon="terminal" title="No variables">Add values such as <code className="font-mono text-xs">NODE_ENV=test</code> that every run should see.</EmptyState>
                </td>
              </tr>
            )}
          </tbody>
        </Table.Root>
      )}
      {snippets.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 border-t border-border px-4 py-2.5">
          <span className="mr-1 text-xs text-subtle">From services</span>
          {snippets.map(({ service, ex }) => (
            <button
              key={`${service}:${ex.key}`}
              type="button"
              onClick={() => {
                setKey(ex.key);
                setValue(ex.value);
                document.getElementById("env-key")?.focus();
              }}
              title={`Fill in ${ex.key}=${ex.value}`}
              className="inline-flex h-6 max-w-full items-center gap-1 truncate rounded-full border border-border bg-surface px-2 font-mono text-[11px] text-muted transition-colors hover:border-border-strong hover:text-fg"
            >
              <span className="text-subtle">+</span>
              <span className="text-fg">{ex.key}</span>=<span className="truncate">{ex.value}</span>
            </button>
          ))}
        </div>
      )}
      <form
        className="flex items-start gap-2 border-t border-border bg-surface-2/40 px-4 py-3"
        onSubmit={(e) => {
          e.preventDefault();
          if (key && !keyError) add.mutate();
        }}
      >
        <Field className="w-[34%]" error={keyError} warning={!keyError && shadow ? `A project secret named ${key} also exists.` : undefined}>
          <Input id="env-key" mono aria-label="Key" placeholder="KEY" value={key} invalid={!!keyError} onChange={(e) => setKey(e.target.value.replace(/\s/g, "_"))} />
        </Field>
        <Input mono aria-label="Value" placeholder="value" value={value} onChange={(e) => setValue(e.target.value)} className="flex-1" />
        <Button type="submit" variant="secondary" icon="plus" disabled={!key || !!keyError} loading={add.isPending}>Add</Button>
      </form>
    </Card>
  );
}
