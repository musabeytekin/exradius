import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { EnvironmentPreview } from "@/components/EnvironmentPreview";
import { environmentTextOf } from "@/lib/environment";
import { KeyValueEditor } from "@/components/KeyValueEditor";
import { newRow, rowsFrom, rowsTo, type Row } from "@/lib/rows";
import { useSecretIndex } from "@/lib/useSecretIndex";
import { CodeBlock } from "@/components/ui/data";
import { PickList } from "@/components/ui/controls";
import { Icon } from "@/components/ui/icons";
import { Sheet, Tooltip } from "@/components/ui/overlays";
import { Banner, Button, Card, CardHeader, Chip, EmptyState, Field, Input, Mono, Skeleton, SkeletonRows, Textarea } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { buttonClass } from "@/lib/buttonStyles";
import { useConfirm } from "@/lib/confirm";
import { pretty } from "@/lib/format";
import { qk, useAgents, useConnections, useProjectAgents, useProjectSecrets } from "@/lib/queries";
import { buildSecretIndex, findRefs } from "@/lib/secretRefs";
import { toast } from "@/lib/toast";
import { modelLabel } from "@/lib/models";
import type { Agent, Connection, ConnectionOverride, Project, ProjectAgent, ResolvedAgent } from "@/lib/types";

export function AgentsTab({ project }: { project: Project }) {
  const attached = useProjectAgents(project.id);
  const agents = useAgents();
  const [editing, setEditing] = useState<ProjectAgent | "new" | null>(null);
  const available = (agents.data ?? []).filter((a) => !attached.data?.some((x) => x.agent_id === a.id));

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <CardHeader
          title="Attached agents"
          description="Tasks for this project may name only these agents. Overrides change a connection or a secret name for this project alone."
          actions={
            <Button size="sm" variant="primary" icon="plus" disabled={!available.length} title={available.length ? undefined : "Every agent is attached"} onClick={() => setEditing("new")}>
              Attach agent
            </Button>
          }
        />
        {attached.isLoading || agents.isLoading ? (
          <SkeletonRows rows={2} />
        ) : !attached.data?.length ? (
          <EmptyState
            icon="agents"
            title="No agents attached"
            action={
              agents.data?.length ? (
                <Button variant="secondary" icon="plus" onClick={() => setEditing("new")}>Attach an agent</Button>
              ) : (
                <Link to="/agents" search={{ create: "1" }} className={buttonClass("secondary")}><Icon name="plus" size={15} />Create an agent</Link>
              )
            }
          >
            {agents.data?.length ? "Pick one of your agents; you can override its connections for this project." : "There are no agents yet. Create one from a starter prompt, then attach it here."}
          </EmptyState>
        ) : (
          <ul className="divide-y divide-border">
            {attached.data.map((pa) => {
              const agent = agents.data?.find((a) => a.id === pa.agent_id);
              return agent ? <AttachedRow key={pa.agent_id} project={project} link={pa} agent={agent} onEdit={() => setEditing(pa)} /> : null;
            })}
          </ul>
        )}
      </Card>
      {editing && (
        <AttachSheet project={project} link={editing === "new" ? null : editing} available={available} onClose={() => setEditing(null)} />
      )}
    </div>
  );
}

function AttachedRow({ project, link, agent, onEdit }: { project: Project; link: ProjectAgent; agent: Agent; onEdit: () => void }) {
  const [open, setOpen] = useState(false);
  const qc = useQueryClient();
  const confirm = useConfirm();
  const navigate = useNavigate();
  const resolved = useQuery({ queryKey: qk.resolved(project.id, agent.id), queryFn: () => api.projectAgents.resolved(project.id, agent.id) });
  const unresolved = resolved.data?.connections.flatMap((c) => c.unresolved) ?? [];
  const overrides = Object.keys(link.connection_overrides).length + Object.keys(link.secret_overrides).length;
  const detach = useMutation({
    mutationFn: () => api.projectAgents.detach(project.id, agent.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.projectAgents(project.id) });
      toast({
        title: `${agent.slug} detached`,
        tone: "ok",
        action: {
          label: "Undo",
          onClick: () =>
            api.projectAgents
              .attach(project.id, { agent_id: agent.id, connection_overrides: link.connection_overrides, secret_overrides: link.secret_overrides })
              .then(() => qc.invalidateQueries({ queryKey: qk.projectAgents(project.id) })),
        },
      });
    },
    onError: (e) => toast.err("Could not detach", e),
  });

  return (
    <li>
      <div className="flex items-center gap-4 px-4 py-3">
        <button type="button" onClick={() => setOpen(!open)} className="flex min-w-0 flex-1 items-center gap-3 text-left" aria-expanded={open}>
          <Icon name="chevronRight" size={14} className={`text-subtle transition-transform ${open ? "rotate-90" : ""}`} />
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="font-medium text-fg">{agent.name}</span>
              <Mono className="text-xs text-muted">{agent.slug}</Mono>
            </div>
            <div className="mt-0.5 flex items-center gap-2 text-xs text-subtle">
              <span>{agent.connection_ids.length} connection{agent.connection_ids.length === 1 ? "" : "s"}</span>
              {overrides > 0 && <span>· {overrides} override{overrides === 1 ? "" : "s"}</span>}
              {"model" in agent && <span>· {modelLabel(agent.model)}</span>}
            </div>
          </div>
        </button>
        {resolved.isLoading ? (
          <Skeleton className="h-5 w-24" />
        ) : unresolved.length ? (
          <Tooltip content={`Unresolved: ${[...new Set(unresolved)].join(", ")}`}>
            <span><Chip tone="err" dot>{new Set(unresolved).size} unresolved</Chip></span>
          </Tooltip>
        ) : (
          <Chip tone="ok" dot>resolves</Chip>
        )}
        <div className="flex gap-1">
          <Button size="sm" variant="ghost" icon="send" onClick={() => navigate({ to: "/tasks", search: { send: "1", project: project.slug, agent: agent.slug } })}>Send task</Button>
          <Button size="sm" variant="ghost" icon="edit" onClick={onEdit}>Overrides</Button>
          <Button
            size="sm"
            variant="ghost"
            square
            icon="trash"
            aria-label={`Detach ${agent.slug}`}
            onClick={async () => {
              if (await confirm({ title: `Detach ${agent.slug} from ${project.slug}?`, description: "New tasks naming this pair will be rejected. Running tasks continue. You can undo right after.", confirmLabel: "Detach" })) detach.mutate();
            }}
          />
        </div>
      </div>
      {open && (
        <div className="border-t border-border bg-surface-2/40 px-4 py-4">
          {resolved.isLoading ? <Skeleton className="h-24" /> : <ResolvedView resolved={resolved.data} />}
        </div>
      )}
    </li>
  );
}

/** The resolved configuration of an attachment: secret names only, never values. */
export function ResolvedView({ resolved }: { resolved: ResolvedAgent | undefined }) {
  const connections = resolved?.connections ?? [];
  const env = environmentTextOf(resolved);
  return (
    <div className="flex flex-col gap-3">
      {resolved && "model" in resolved && (
        <div className="flex items-center gap-2 text-xs">
          <span className="text-subtle">Model</span>
          {resolved.model ? <Mono className="text-xs">{resolved.model}</Mono> : <span className="text-muted">runtime default</span>}
        </div>
      )}
      {env && <EnvironmentPreview text={env} />}
      {!connections.length ? (
        <p className="text-sm text-muted">This agent has no connections beyond the built-in exradius tools.</p>
      ) : (
        <p className="text-xs text-muted">What a run of this agent in this project gets. References are shown as written; values are never read.</p>
      )}
      {connections.map((c) => (
        <div key={c.slug} className="rounded-lg border border-border bg-surface">
          <div className="flex items-center gap-2 border-b border-border px-3 py-2">
            <Mono className="font-medium">{c.slug}</Mono>
            <Chip>{c.transport}</Chip>
            {c.overridden && <Chip tone="accent">overridden here</Chip>}
            {c.unresolved.length > 0 && <Chip tone="err" dot>{c.unresolved.length} unresolved</Chip>}
          </div>
          <div className="grid grid-cols-[110px_1fr] gap-x-4 gap-y-1.5 px-3 py-2.5 text-xs">
            {c.transport === "stdio" ? (
              <>
                <span className="text-subtle">command</span>
                <Mono className="text-xs break-all">{[c.command, ...c.args].join(" ")}</Mono>
              </>
            ) : (
              <>
                <span className="text-subtle">url</span>
                <Mono className="text-xs break-all">{c.url}</Mono>
              </>
            )}
            {Object.entries({ ...c.env, ...c.headers }).map(([k, v]) => (
              <div key={k} className="contents">
                <span className="truncate font-mono text-subtle">{k}</span>
                <Mono className="text-xs break-all">{v}</Mono>
              </div>
            ))}
          </div>
          {c.references.length > 0 && (
            <div className="flex flex-wrap gap-1.5 border-t border-border px-3 py-2">
              {c.references.map((r) => (
                <Chip key={r.name} tone={r.scope ? "ok" : "err"} mono title={r.scope ? `${r.name} → ${r.resolves_to} (${r.scope})` : `${r.resolves_to} not found`}>
                  {r.name}
                  {r.resolves_to !== r.name && <> → {r.resolves_to}</>}
                  <span className="opacity-70">· {r.scope ?? "missing"}</span>
                </Chip>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// --- Attach / overrides -----------------------------------------------------------------------

interface OverrideDraft {
  slug: string;
  command: string;
  args: string;
  url: string;
  env: Row[];
  headers: Row[];
}

function draftFor(c: Connection, existing?: ConnectionOverride): OverrideDraft {
  return {
    slug: c.slug,
    command: existing?.command ?? c.command ?? "",
    args: (existing?.args ?? c.args).join("\n"),
    url: existing?.url ?? c.url ?? "",
    env: rowsFrom({ ...c.env, ...(existing?.env ?? {}) }),
    headers: rowsFrom({ ...c.headers, ...(existing?.headers ?? {}) }),
  };
}

/** Only what differs from the connection: replaced fields, and changed or added env/header keys. */
function diffOverride(c: Connection, d: OverrideDraft): ConnectionOverride {
  const o: ConnectionOverride = {};
  const args = d.args.split("\n").map((a) => a.trim()).filter(Boolean);
  if (c.transport === "stdio") {
    if (d.command.trim() && d.command.trim() !== (c.command ?? "")) o.command = d.command.trim();
    if (JSON.stringify(args) !== JSON.stringify(c.args)) o.args = args;
  } else if (d.url.trim() && d.url.trim() !== (c.url ?? "")) o.url = d.url.trim();
  const env = Object.fromEntries(Object.entries(rowsTo(d.env)).filter(([k, v]) => c.env[k] !== v));
  const headers = Object.fromEntries(Object.entries(rowsTo(d.headers)).filter(([k, v]) => c.headers[k] !== v));
  if (Object.keys(env).length) o.env = env;
  if (Object.keys(headers).length) o.headers = headers;
  return o;
}

function AttachSheet({ project, link, available, onClose }: { project: Project; link: ProjectAgent | null; available: Agent[]; onClose: () => void }) {
  const qc = useQueryClient();
  const agents = useAgents();
  const connections = useConnections();
  const projectSecrets = useProjectSecrets(project.id);
  const { index: fullIndex, globals } = useSecretIndex();
  const [agentId, setAgentId] = useState<string | null>(link?.agent_id ?? available[0]?.id ?? null);
  const agent = agents.data?.find((a) => a.id === agentId);
  const agentConns = (connections.data ?? []).filter((c) => agent?.connection_ids.includes(c.id));
  const [drafts, setDrafts] = useState<OverrideDraft[]>(() =>
    link ? Object.entries(link.connection_overrides).flatMap(([slug, o]) => {
      const c = connections.data?.find((x) => x.slug === slug);
      return c ? [draftFor(c, o)] : [];
    }) : [],
  );
  const [secretRows, setSecretRows] = useState<{ id: number; from: string | null; to: string | null }[]>(() =>
    Object.entries(link?.secret_overrides ?? {}).map(([from, to], i) => ({ id: i + 1, from, to: String(to) })),
  );

  // For this project, project secrets resolve too.
  const index = useMemo(
    () => buildSecretIndex([...globals, ...(projectSecrets.data ?? [])], []),
    [globals, projectSecrets.data],
  );
  const insertNames = [
    ...(projectSecrets.data ?? []).map((s) => ({ name: s.key, scope: "project" as const, where: project.slug })),
    ...globals.map((s) => ({ name: s.key, scope: "global" as const })),
  ];
  const referenced = useMemo(() => {
    const names = new Set<string>();
    for (const c of agentConns) for (const v of [...Object.values(c.env), ...Object.values(c.headers)]) for (const r of findRefs(v, fullIndex)) if (r.name && r.status !== "invalid") names.add(r.name);
    for (const d of drafts) for (const r of [...d.env, ...d.headers]) for (const ref of findRefs(r.value, fullIndex)) if (ref.name && ref.status !== "invalid") names.add(ref.name);
    return [...names].sort();
  }, [agentConns, drafts, fullIndex]);

  const connectionOverrides = Object.fromEntries(
    drafts.flatMap((d) => {
      const c = agentConns.find((x) => x.slug === d.slug);
      if (!c) return [];
      const o = diffOverride(c, d);
      return Object.keys(o).length ? [[d.slug, o]] : [];
    }),
  );
  const secretOverrides = Object.fromEntries(secretRows.filter((r) => r.from && r.to).map((r) => [r.from!, r.to!]));
  const body = { agent_id: agentId, connection_overrides: connectionOverrides, secret_overrides: secretOverrides };

  const save = useMutation({
    mutationFn: () =>
      link
        ? api.projectAgents.update(project.id, link.agent_id, { connection_overrides: connectionOverrides, secret_overrides: secretOverrides })
        : api.projectAgents.attach(project.id, { agent_id: agentId!, connection_overrides: connectionOverrides, secret_overrides: secretOverrides }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.projectAgents(project.id) });
      qc.invalidateQueries({ queryKey: ["projects", project.id, "agents"] });
      toast.ok(link ? "Overrides saved" : `${agent?.slug} attached to ${project.slug}`);
      onClose();
    },
    onError: (e) => toast.err(link ? "Could not save the overrides" : "Could not attach", e),
  });

  const notOverridden = agentConns.filter((c) => !drafts.some((d) => d.slug === c.slug));

  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={link ? `Overrides for ${agent?.slug ?? "agent"}` : `Attach an agent to ${project.slug}`}
      description="Overrides apply only to this project. Leave everything as is to use the agent unchanged."
      width={760}
      onSubmit={() => agentId && save.mutate()}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" loading={save.isPending} disabled={!agentId}>{link ? "Save overrides" : "Attach agent"}</Button>
        </>
      }
    >
      <div className="flex flex-col gap-6 px-6 py-5">
        {!link && (
          <Field label="Agent">
            <PickList
              value={agentId}
              onChange={(v) => {
                setAgentId(v);
                setDrafts([]);
                setSecretRows([]);
              }}
              options={available.map((a) => ({ value: a.id, label: a.name, text: a.name, description: `${a.slug} · ${a.connection_ids.length} connections`, meta: <Mono className="text-xs text-subtle">{a.slug}</Mono> }))}
            />
          </Field>
        )}

        <section className="flex flex-col gap-3">
          <div>
            <h3 className="text-sm font-semibold">Connection overrides</h3>
            <p className="text-xs text-muted">Pick a connection; its fields appear prefilled. Change what differs for this project. Only the differences are stored.</p>
          </div>
          {drafts.map((d) => {
            const c = agentConns.find((x) => x.slug === d.slug);
            if (!c) return null;
            const set = (patch: Partial<OverrideDraft>) => setDrafts((all) => all.map((x) => (x.slug === d.slug ? { ...x, ...patch } : x)));
            const diff = diffOverride(c, d);
            return (
              <div key={d.slug} className="rounded-lg border border-border">
                <div className="flex items-center gap-2 border-b border-border px-3 py-2">
                  <Mono className="font-medium">{c.slug}</Mono>
                  <Chip>{c.transport}</Chip>
                  <span className="text-xs text-subtle">{Object.keys(diff).length ? `${Object.keys(diff).join(", ")} changed` : "no changes yet"}</span>
                  <Button size="xs" variant="ghost" icon="x" className="ml-auto" onClick={() => setDrafts((all) => all.filter((x) => x.slug !== d.slug))}>Remove override</Button>
                </div>
                <div className="flex flex-col gap-3 p-3">
                  {c.transport === "stdio" ? (
                    <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-3">
                      <Field label="Command"><Input mono value={d.command} onChange={(e) => set({ command: e.target.value })} /></Field>
                      <Field label="Arguments" hint="One per line."><Textarea mono rows={Math.max(2, d.args.split("\n").length)} value={d.args} onChange={(e) => set({ args: e.target.value })} /></Field>
                    </div>
                  ) : (
                    <Field label="URL"><Input mono value={d.url} onChange={(e) => set({ url: e.target.value })} /></Field>
                  )}
                  <Field label="Environment">
                    <KeyValueEditor rows={d.env} onChange={(env) => set({ env })} index={index} names={insertNames} keyPlaceholder="KEY" valuePlaceholder="value or ${secret:NAME}" projectId={project.id} />
                  </Field>
                  {c.transport === "http" && (
                    <Field label="Headers">
                      <KeyValueEditor rows={d.headers} onChange={(headers) => set({ headers })} index={index} names={insertNames} keyPlaceholder="Header" valuePlaceholder="value" projectId={project.id} />
                    </Field>
                  )}
                </div>
              </div>
            );
          })}
          {agent && notOverridden.length > 0 ? (
            <div className="w-72">
              <PickList
                value={null}
                placeholder="Override a connection…"
                onChange={(v) => {
                  const c = agentConns.find((x) => x.id === v);
                  if (c) setDrafts((all) => [...all, draftFor(c)]);
                }}
                options={notOverridden.map((c) => ({ value: c.id, label: c.slug, description: c.name }))}
              />
            </div>
          ) : agent && !agentConns.length ? (
            <p className="text-xs text-subtle">{agent.slug} has no connections to override.</p>
          ) : null}
        </section>

        <section className="flex flex-col gap-3">
          <div>
            <h3 className="text-sm font-semibold">Secret overrides</h3>
            <p className="text-xs text-muted">Point a name the connections reference at a different secret of this project.</p>
          </div>
          {secretRows.map((r) => (
            <div key={r.id} className="grid grid-cols-[1fr_auto_1fr_auto] items-center gap-2">
              <PickList
                mono
                value={r.from}
                placeholder="Referenced name"
                onChange={(v) => setSecretRows((rows) => rows.map((x) => (x.id === r.id ? { ...x, from: v } : x)))}
                options={[...new Set([...referenced, ...(r.from ? [r.from] : [])])].map((n) => ({ value: n, label: n }))}
              />
              <Icon name="arrowRight" size={14} className="text-subtle" />
              <PickList
                mono
                value={r.to}
                placeholder="Project secret"
                onChange={(v) => setSecretRows((rows) => rows.map((x) => (x.id === r.id ? { ...x, to: v } : x)))}
                options={[...(projectSecrets.data ?? []).map((s) => ({ value: s.key, label: s.key })), ...(r.to && !projectSecrets.data?.some((s) => s.key === r.to) ? [{ value: r.to, label: r.to, description: "not a secret of this project" }] : [])]}
              />
              <Button variant="ghost" square icon="x" aria-label="Remove" onClick={() => setSecretRows((rows) => rows.filter((x) => x.id !== r.id))} />
            </div>
          ))}
          {referenced.length === 0 ? (
            <p className="text-xs text-subtle">The agent's connections reference no secrets.</p>
          ) : !projectSecrets.data?.length ? (
            <Banner tone="neutral">This project has no secrets to point at yet. Add them on the Secrets tab.</Banner>
          ) : (
            <div>
              <Button size="sm" variant="ghost" icon="plus" onClick={() => setSecretRows((rows) => [...rows, { id: newRow().id, from: referenced.find((n) => !rows.some((x) => x.from === n)) ?? null, to: null }])}>
                Add secret override
              </Button>
            </div>
          )}
        </section>

        <details className="group">
          <summary className="flex cursor-pointer list-none items-center gap-1.5 text-xs font-medium text-muted hover:text-fg">
            <Icon name="chevronRight" size={12} className="transition-transform group-open:rotate-90" />
            Request body
          </summary>
          <CodeBlock className="mt-2" lang="json" code={pretty(link ? { connection_overrides: connectionOverrides, secret_overrides: secretOverrides } : body)} />
        </details>
      </div>
    </Sheet>
  );
}
