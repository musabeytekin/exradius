import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo, useState, type DragEvent, type KeyboardEvent } from "react";
import { KeyValueEditor } from "@/components/KeyValueEditor";
import { CredentialInput, type CredentialDraft } from "@/components/secrets";
import { PickList, Segmented, SuggestInput } from "@/components/ui/controls";
import { StateChip } from "@/components/ui/data";
import { Icon } from "@/components/ui/icons";
import { Dialog, Sheet, Tooltip } from "@/components/ui/overlays";
import { Banner, Button, Card, CardHeader, Chip, EmptyState, Field, Input, Mono, SkeletonRows } from "@/components/ui/primitives";
import { api, ApiError } from "@/lib/api";
import { SERVICE_CATALOG, type ServiceCatalogEntry } from "@/lib/catalog";
import { cn } from "@/lib/cn";
import { useConfirm } from "@/lib/confirm";
import { storeCredential } from "@/lib/credentials";
import { relativeTime } from "@/lib/format";
import { qk, useProjectSecrets, useServices } from "@/lib/queries";
import { newRow, type Row } from "@/lib/rows";
import { buildSecretIndex } from "@/lib/secretRefs";
import {
  SERVICE_ENV_KEY_RE, SERVICE_NAME_RE, draftFromCatalog, draftFromService, draftToBody, generatePassword, joinCommand,
  parsePorts, readyLabel, readyMode, splitCommand, startStepName, type ReadyMode, type ServiceDraft,
} from "@/lib/services";
import { toast } from "@/lib/toast";
import { useSecretIndex } from "@/lib/useSecretIndex";
import type { Project, ProjectService, Verification, VerificationStep } from "@/lib/types";

/** Older APIs have no services endpoint. */
const servicesUnsupported = (e: unknown) => e instanceof ApiError && (e.status === 404 || e.status === 405);

type Editing = { service: ProjectService } | { entry: ServiceCatalogEntry | null };

export function ServicesTab({ project }: { project: Project }) {
  const services = useServices(project.id);
  const verifications = useQuery({ queryKey: qk.verifications(project.id), queryFn: () => api.projects.verifications(project.id) });
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [picking, setPicking] = useState(false);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [dragId, setDragId] = useState<string | null>(null);
  const list = useMemo(() => [...(services.data ?? [])].sort((a, b) => a.position - b.position), [services.data]);
  const [dragOrder, setDragOrder] = useState<string[] | null>(null);
  const shown = dragOrder ? dragOrder.map((id) => list.find((s) => s.id === id)!).filter(Boolean) : list;
  const invalidate = () => qc.invalidateQueries({ queryKey: qk.services(project.id) });

  const reorder = useMutation({
    mutationFn: (ids: string[]) => api.services.reorder(project.id, ids),
    onMutate: (ids) => {
      qc.setQueryData<ProjectService[]>(qk.services(project.id), (old) =>
        old?.map((s) => ({ ...s, position: ids.indexOf(s.id) })),
      );
    },
    onError: (e) => toast.err("Could not reorder the services", e),
    onSettled: invalidate,
  });
  const move = (id: string, by: number) => {
    const ids = shown.map((s) => s.id);
    const i = ids.indexOf(id);
    const j = i + by;
    if (i < 0 || j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    reorder.mutate(ids);
  };
  const remove = useMutation({
    mutationFn: (s: ProjectService) => api.services.remove(project.id, s.id),
    onSuccess: (_, s) => {
      invalidate();
      toast({
        title: `${s.name} removed`,
        tone: "ok",
        action: {
          label: "Undo",
          onClick: () => api.services.create(project.id, { name: s.name, image: s.image, env: s.env, ports: s.ports, ready: s.ready }).then(invalidate),
        },
      });
    },
    onError: (e) => toast.err("Could not remove the service", e),
  });

  const onDragOver = (e: DragEvent, overId: string) => {
    if (!dragId || dragId === overId) return;
    e.preventDefault();
    // The dragged row takes the slot of the row under the pointer.
    const ids = [...(dragOrder ?? list.map((s) => s.id))];
    const to = ids.indexOf(overId);
    ids.splice(ids.indexOf(dragId), 1);
    ids.splice(to, 0, dragId);
    if (ids.join() !== (dragOrder ?? []).join()) setDragOrder(ids);
  };
  const endDrag = () => {
    if (dragOrder && dragOrder.join() !== list.map((s) => s.id).join()) reorder.mutate(dragOrder);
    setDragId(null);
    setDragOrder(null);
  };

  if (services.isError && servicesUnsupported(services.error)) {
    return (
      <Card>
        <CardHeader title="Services" description="Dependencies started next to the agent for each task, reachable by name." />
        <EmptyState compact icon="database" title="Not available on this API">
          The API does not serve project services yet. Update Exradius to run databases and other dependencies next to the agent.
        </EmptyState>
      </Card>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader
          title="Services"
          description="Dependencies started next to the agent for each task, reachable by name."
          actions={<Button size="sm" variant="primary" icon="plus" disabled={services.isLoading} onClick={() => setPicking(true)}>Add service</Button>}
        />
        {services.isLoading ? (
          <SkeletonRows rows={2} />
        ) : services.isError ? (
          <div className="p-4"><Banner tone="err" title="Could not load the services">{(services.error as Error).message}</Banner></div>
        ) : !shown.length ? (
          <EmptyState
            icon="database"
            title="No services"
            action={<Button variant="secondary" icon="plus" onClick={() => setPicking(true)}>Add a service</Button>}
          >
            A database, cache or broker the code needs while the agent works. Each task gets fresh ones on its own network.
          </EmptyState>
        ) : (
          <ul className="divide-y divide-border" aria-label="Services, in start order">
            {shown.map((s, i) => (
              <ServiceRow
                key={s.id}
                service={s}
                first={i === 0}
                last={i === shown.length - 1}
                dragging={dragId === s.id}
                verify={lastVerify(verifications.data, s.name)}
                onDragStart={(e) => {
                  setDragId(s.id);
                  e.dataTransfer.effectAllowed = "move";
                  e.dataTransfer.setData("text/plain", s.name);
                }}
                onDragOver={(e) => onDragOver(e, s.id)}
                onDragEnd={endDrag}
                onMove={(by) => move(s.id, by)}
                onEdit={() => setEditing({ service: s })}
                onRemove={async () => {
                  if (await confirm({ title: `Remove ${s.name}?`, description: "Tasks started after this run without it. You can undo right after.", confirmLabel: "Remove" })) remove.mutate(s);
                }}
              />
            ))}
          </ul>
        )}
      </Card>
      {picking && (
        <CatalogDialog
          onClose={() => setPicking(false)}
          onPick={(entry) => {
            setPicking(false);
            setEditing({ entry });
          }}
        />
      )}
      {editing && (
        <ServiceSheet
          project={project}
          existing={"service" in editing ? editing.service : null}
          initial={"service" in editing ? draftFromService(editing.service) : draftFromCatalog(editing.entry, list.map((s) => s.name))}
          taken={list.filter((s) => !("service" in editing) || s.id !== editing.service.id).map((s) => s.name)}
          onClose={() => setEditing(null)}
        />
      )}
    </div>
  );
}

interface VerifyOutcome { step: VerificationStep; at: string }

/** The newest verification that started this service. */
function lastVerify(history: Verification[] | undefined, name: string): VerifyOutcome | null {
  for (const v of history ?? []) {
    const step = v.steps?.find((s) => s.name === startStepName(name));
    if (step) return { step, at: v.started_at ?? v.created_at };
  }
  return null;
}

function ServiceRow({
  service: s, first, last, dragging, verify, onDragStart, onDragOver, onDragEnd, onMove, onEdit, onRemove,
}: {
  service: ProjectService; first: boolean; last: boolean; dragging: boolean; verify: VerifyOutcome | null;
  onDragStart: (e: DragEvent) => void; onDragOver: (e: DragEvent) => void; onDragEnd: () => void;
  onMove: (by: number) => void; onEdit: () => void; onRemove: () => void;
}) {
  const onKeyDown = (e: KeyboardEvent) => {
    if (!e.altKey) return;
    const by = e.key === "ArrowUp" && !first ? -1 : e.key === "ArrowDown" && !last ? 1 : 0;
    if (!by) return;
    e.preventDefault();
    onMove(by);
  };
  const tail = verify?.step.log?.trim().split("\n").slice(-6).join("\n");
  return (
    <li
      draggable
      onDragStart={onDragStart}
      onDragOver={onDragOver}
      onDrop={(e) => e.preventDefault()}
      onDragEnd={onDragEnd}
      onKeyDown={onKeyDown}
      className={cn("grid grid-cols-[auto_minmax(0,1.3fr)_minmax(0,0.7fr)_minmax(0,1fr)_auto_auto] items-center gap-4 px-4 py-3", dragging && "bg-accent-soft/40 opacity-70")}
    >
      <span className="cursor-grab text-subtle active:cursor-grabbing" title="Drag to reorder; Alt+↑/↓ also moves it">
        <Icon name="grip" size={16} strokeWidth={3} />
      </span>
      <div className="min-w-0">
        <Mono className="font-medium text-fg">{s.name}</Mono>
        <Mono className="block truncate text-xs text-muted" title={s.image}>{s.image}</Mono>
      </div>
      <div className="flex flex-wrap gap-1">
        {s.ports?.length ? s.ports.map((p) => <Chip key={p} mono>{p}</Chip>) : <span className="text-xs text-subtle">no ports</span>}
      </div>
      <span className="truncate font-mono text-[11.5px] text-muted" title={`Ready when: ${readyLabel(s.ready)}`}>{readyLabel(s.ready)}</span>
      <div className="flex justify-end">
        {!verify ? (
          <span className="text-xs text-subtle">not verified</span>
        ) : (
          <Tooltip content={tail ? <pre className="max-w-80 whitespace-pre-wrap font-mono text-[11px]">{tail}</pre> : `Verified ${relativeTime(verify.at)}`}>
            <span className="flex items-center gap-1.5">
              <StateChip state={verify.step.state} />
              <span className="text-xs text-subtle">{relativeTime(verify.at)}</span>
            </span>
          </Tooltip>
        )}
      </div>
      <div className="flex gap-0.5">
        <Button size="sm" variant="ghost" square aria-label={`Move ${s.name} up`} disabled={first} onClick={() => onMove(-1)}>
          <Icon name="chevronDown" size={14} className="rotate-180" />
        </Button>
        <Button size="sm" variant="ghost" square icon="chevronDown" aria-label={`Move ${s.name} down`} disabled={last} onClick={() => onMove(1)} />
        <Button size="sm" variant="ghost" square icon="edit" aria-label={`Edit ${s.name}`} onClick={onEdit} />
        <Button size="sm" variant="ghost" square icon="trash" aria-label={`Remove ${s.name}`} onClick={onRemove} />
      </div>
    </li>
  );
}

function CatalogDialog({ onClose, onPick }: { onClose: () => void; onPick: (e: ServiceCatalogEntry | null) => void }) {
  const tile = "flex flex-col gap-1 rounded-lg border border-border bg-surface px-3 py-2.5 text-left transition-colors hover:border-border-strong hover:bg-surface-2/60 focus:outline-none focus:border-accent focus:ring-3 focus:ring-[var(--ring)]";
  return (
    <Dialog open onOpenChange={(o) => !o && onClose()} title="Add a service" description="Pick one to prefill its image, settings and readiness check." width={620}>
      <div className="grid grid-cols-2 gap-2">
        {SERVICE_CATALOG.map((e) => (
          <button key={e.key} type="button" className={tile} onClick={() => onPick(e)}>
            <span className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium text-fg">{e.name}</span>
              <Mono className="truncate text-2xs text-subtle">{e.image}</Mono>
            </span>
            <span className="text-xs text-muted">{e.description}</span>
          </button>
        ))}
        <button type="button" className={cn(tile, "border-dashed")} onClick={() => onPick(null)}>
          <span className="text-sm font-medium text-fg">Custom image</span>
          <span className="text-xs text-muted">Any image; the fields start empty.</span>
        </button>
      </div>
    </Dialog>
  );
}

function ServiceSheet({ project, existing, initial, taken, onClose }: {
  project: Project; existing: ProjectService | null; initial: ServiceDraft; taken: string[]; onClose: () => void;
}) {
  const qc = useQueryClient();
  const projectSecrets = useProjectSecrets(project.id);
  const { globals } = useSecretIndex();
  const [name, setName] = useState(initial.name);
  const [image, setImage] = useState(initial.image);
  const [portsText, setPortsText] = useState(initial.ports.join(", "));
  const [mode, setMode] = useState<ReadyMode>(readyMode(initial.ready));
  const [tcp, setTcp] = useState<string>(initial.ready && "tcp" in initial.ready ? String(initial.ready.tcp) : String(initial.ports[0] ?? ""));
  const [command, setCommand] = useState(initial.ready && "command" in initial.ready ? joinCommand(initial.ready.command) : "");
  const [env, setEnv] = useState<Row[]>(() => initial.env.map((r) => newRow(r.key, r.value)));
  const saved = (secret: string) => !!projectSecrets.data?.some((s) => s.key === secret);
  const [creds, setCreds] = useState<Record<string, CredentialDraft>>(() =>
    Object.fromEntries(initial.credentials.map((f) => [f.secret, { value: "", replacing: false }])),
  );
  const [generated] = useState<Record<string, string>>(() => Object.fromEntries(initial.credentials.map((f) => [f.secret, generatePassword()])));
  // A password nobody has to remember: generated for every credential not saved yet.
  const credDraft = (secret: string): CredentialDraft => {
    const d = creds[secret] ?? { value: "", replacing: false };
    return !saved(secret) && !d.replacing && !d.value ? { value: generated[secret] ?? "", replacing: false } : d;
  };

  const index = useMemo(() => buildSecretIndex([...globals, ...(projectSecrets.data ?? [])], []), [globals, projectSecrets.data]);
  const insertNames = [
    ...(projectSecrets.data ?? []).map((s) => ({ name: s.key, scope: "project" as const, where: project.slug })),
    ...globals.map((s) => ({ name: s.key, scope: "global" as const })),
  ];

  const ports = parsePorts(portsText);
  const nameError = !name ? null : !SERVICE_NAME_RE.test(name) ? "Lowercase letters, digits and dashes, starting with a letter." : taken.includes(name) ? "Another service of this project uses this name." : null;
  const portsError = ports === null ? "Port numbers from 1 to 65535, separated by commas." : null;
  const badKey = env.find((r) => r.key.trim() && !SERVICE_ENV_KEY_RE.test(r.key.trim()));
  const tcpPort = Number(tcp);
  const ready =
    mode === "image" ? null : mode === "tcp" ? { tcp: tcpPort } : { command: splitCommand(command) };
  const readyError = mode === "tcp" && !(Number.isInteger(tcpPort) && tcpPort > 0 && tcpPort < 65536) ? "Pick the port to wait for." : mode === "command" && !splitCommand(command).length ? "Enter the command to run in the service container." : null;
  const missingCred = initial.credentials.find((f) => !saved(f.secret) && !credDraft(f.secret).value);
  const ok = !!name && !nameError && !!image.trim() && !portsError && !badKey && !readyError && !missingCred;
  const draft: ServiceDraft = {
    name, image, ports: ports ?? [], ready, credentials: initial.credentials,
    env: env.map((r) => ({ key: r.key, value: r.value })),
  };

  const save = useMutation({
    mutationFn: async () => {
      for (const f of initial.credentials) {
        const d = credDraft(f.secret);
        if (d.value) await storeCredential(project.id, f.secret, d.value, projectSecrets.data ?? []);
      }
      const body = draftToBody(draft);
      return existing ? api.services.update(project.id, existing.id, body) : api.services.create(project.id, body);
    },
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: qk.services(project.id) });
      qc.invalidateQueries({ queryKey: qk.projectSecrets(project.id) });
      toast.ok(existing ? `${s.name} saved` : `${s.name} added`, "Verify the project to start it once.");
      onClose();
    },
    onError: (e) => toast.err(existing ? "Could not save the service" : "Could not add the service", e),
  });

  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={existing ? `Edit ${existing.name}` : "Add a service"}
      description="Started on the task's own network before the setup commands, and removed when the task ends."
      width={680}
      onSubmit={() => ok && save.mutate()}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" loading={save.isPending} disabled={!ok}>{existing ? "Save service" : "Add service"}</Button>
        </>
      }
    >
      <div className="flex flex-col gap-5 px-6 py-5">
        <div className="grid grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)] gap-3">
          <Field label="Name" htmlFor="svc-name" error={nameError} hint={!nameError ? <>The host name: the agent reaches it as <code className="font-mono">{name || "name"}</code>.</> : undefined}>
            <Input id="svc-name" mono autoFocus={!existing} value={name} invalid={!!nameError} onChange={(e) => setName(e.target.value.toLowerCase())} spellCheck={false} />
          </Field>
          <Field label="Image" htmlFor="svc-image">
            <SuggestInput
              id="svc-image"
              mono
              value={image}
              onChange={setImage}
              spellCheck={false}
              placeholder="postgres:16"
              suggestions={SERVICE_CATALOG.map((e) => ({ value: e.image, description: e.name }))}
            />
          </Field>
        </div>

        {initial.credentials.map((f) => (
          <CredentialInput
            key={f.secret}
            id={`svc-cred-${f.secret}`}
            label={f.label}
            help={f.help}
            saved={saved(f.secret)}
            draft={credDraft(f.secret)}
            error={!saved(f.secret) && !credDraft(f.secret).value ? `Enter the ${f.label.toLowerCase()}.` : null}
            onChange={(d) => setCreds((m) => ({ ...m, [f.secret]: d.replacing || saved(f.secret) ? d : { ...d, replacing: true } }))}
          />
        ))}

        <div className="grid grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)] gap-3">
          <Field label="Ports" htmlFor="svc-ports" error={portsError} hint={!portsError ? "Told to the agent; nothing is published." : undefined}>
            <Input id="svc-ports" mono value={portsText} invalid={!!portsError} onChange={(e) => setPortsText(e.target.value)} placeholder="5432" />
          </Field>
          <Field label="Ready when" error={readyError}>
            <div className="flex flex-col gap-2">
              <Segmented
                aria-label="Readiness"
                size="sm"
                value={mode}
                onChange={(m) => {
                  setMode(m);
                  if (m === "tcp" && !tcp && ports?.[0]) setTcp(String(ports[0]));
                }}
                options={[
                  { value: "image", label: "Image healthcheck" },
                  { value: "tcp", label: "Port open" },
                  { value: "command", label: "Command passes" },
                ]}
              />
              {mode === "tcp" && (
                ports?.length ? (
                  <PickList mono aria-label="Port to wait for" value={tcp || null} onChange={(v) => setTcp(v ?? "")} options={ports.map((p) => ({ value: String(p), label: String(p) }))} />
                ) : (
                  <Input mono aria-label="Port to wait for" value={tcp} onChange={(e) => setTcp(e.target.value)} placeholder="6379" />
                )
              )}
              {mode === "command" && (
                <Input mono aria-label="Readiness command" value={command} onChange={(e) => setCommand(e.target.value)} placeholder="pg_isready -U app" spellCheck={false} />
              )}
              {mode === "image" && <p className="text-xs text-subtle">The image's HEALTHCHECK, or just running when it has none.</p>}
            </div>
          </Field>
        </div>

        <Field label="Environment" error={badKey ? `${badKey.key.trim()}: letters, digits and underscores; not starting with a digit.` : undefined} hint={!badKey ? "Values may reference secrets as ${secret:NAME}." : undefined}>
          <KeyValueEditor rows={env} onChange={setEnv} index={index} names={insertNames} keyPlaceholder="NAME" valuePlaceholder="value" projectId={project.id} addLabel="Add variable" />
        </Field>
      </div>
    </Sheet>
  );
}
