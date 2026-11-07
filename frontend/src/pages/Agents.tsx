import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { Checkbox, PickList, Table } from "@/components/ui/controls";
import { CodeBlock } from "@/components/ui/data";
import { Icon } from "@/components/ui/icons";
import { Tooltip } from "@/components/ui/overlays";
import { Button, Card, CardHeader, Chip, EmptyState, Field, Input, Mono, PageHeader, SkeletonRows, Spinner, Textarea } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { buttonClass } from "@/lib/buttonStyles";
import { cn } from "@/lib/cn";
import { useConfirm } from "@/lib/confirm";
import { relativeTime } from "@/lib/format";
import { qk, useAgents, useConnections, useProjects, useProviders, useRuntimes } from "@/lib/queries";
import { SLUG_RE, slugify } from "@/lib/slug";
import { emailError, needsEmail } from "@/lib/identity";
import { modelLabel, modelSelection, modelsFor, OTHER, RUNTIME_DEFAULT } from "@/lib/models";
import { providerKinds, runtimeAccepts, testDetail } from "@/lib/providers";
import { runProviderTest, testOk } from "@/lib/providerTest";
import { TASK_LAYOUT, TEMPLATES } from "@/lib/templates";
import { toast } from "@/lib/toast";
import type { Agent, Connection, ProviderModel } from "@/lib/types";

export function AgentsPage() {
  const { edit, create } = useSearch({ from: "/app/agents" });
  const agents = useAgents();
  if (create || edit) {
    const agent = edit ? agents.data?.find((a) => a.id === edit) : undefined;
    if (edit && agents.isLoading) return <SkeletonRows rows={6} />;
    if (edit && !agent) {
      return (
        <EmptyState icon="agents" title="Agent not found" action={<Link to="/agents" search={{}} className={buttonClass("secondary")}>All agents</Link>}>
          It may have been deleted.
        </EmptyState>
      );
    }
    return <AgentEditor key={agent?.id ?? "new"} agent={agent ?? null} />;
  }
  return <AgentList />;
}

function AgentList() {
  const agents = useAgents();
  const connections = useConnections();
  const projects = useProjects();
  const providers = useProviders();
  const navigate = useNavigate();
  const showProvider = (providers.data?.length ?? 0) > 1;
  const providerName = (a: Agent) => {
    const p = providers.data?.find((x) => (a.provider_id ? x.id === a.provider_id : x.is_default));
    return p ? (a.provider_id ? p.name : `Default (${p.name})`) : "—";
  };
  const showModel = (agents.data ?? []).some((a) => "model" in a);
  const attachments = useQueries({ queries: (projects.data ?? []).map((p) => ({ queryKey: qk.projectAgents(p.id), queryFn: () => api.projectAgents.list(p.id) })) });
  const projectsOf = (a: Agent) => (projects.data ?? []).filter((_, i) => attachments[i]?.data?.some((x) => x.agent_id === a.id));

  return (
    <>
      <PageHeader
        title="Agents"
        description="A prompt and a set of connections. Attach an agent to a project to let tasks run it there."
        actions={<Button variant="primary" icon="plus" onClick={() => navigate({ to: "/agents", search: { create: "1" } })}>New agent</Button>}
      />
      <Card>
        {agents.isLoading ? (
          <SkeletonRows rows={3} />
        ) : !agents.data?.length ? (
          <EmptyState
            icon="agents"
            title="No agents yet"
            action={<Button variant="primary" icon="plus" onClick={() => navigate({ to: "/agents", search: { create: "1" } })}>Create from a starter prompt</Button>}
          >
            Start from the bug fixer, feature integrator or tracer. They show how an agent asks for help and reports its result.
          </EmptyState>
        ) : (
          <Table.Root>
            <Table.Head>
              <tr>
                <Table.Th>Agent</Table.Th>
                <Table.Th>Connections</Table.Th>
                {showProvider && <Table.Th>Provider</Table.Th>}
                {showModel && <Table.Th>Model</Table.Th>}
                <Table.Th>Projects</Table.Th>
                <Table.Th className="text-right">Created</Table.Th>
              </tr>
            </Table.Head>
            <tbody>
              {agents.data.map((a) => {
                const conns = (connections.data ?? []).filter((c) => a.connection_ids.includes(c.id));
                const ps = projectsOf(a);
                return (
                  <Table.Row key={a.id} onClick={() => navigate({ to: "/agents", search: { edit: a.id } })}>
                    <Table.Td>
                      <div className="flex items-center gap-2 font-medium">{a.name}{needsEmail(a) && <Chip tone="warn" dot>set an email</Chip>}</div>
                      <Mono className="text-xs text-muted">{a.slug}</Mono>
                    </Table.Td>
                    <Table.Td>
                      <div className="flex flex-wrap gap-1">
                        {conns.map((c) => <Chip key={c.id} mono tone={c.last_test && !c.last_test.ok ? "err" : "neutral"}>{c.slug}</Chip>)}
                        {!conns.length && <span className="text-xs text-subtle">built-in tools only</span>}
                      </div>
                    </Table.Td>
                    {showProvider && <Table.Td><span className="text-xs text-muted">{providerName(a)}</span></Table.Td>}
                    {showModel && (
                      <Table.Td>
                        {a.model ? <Mono className="text-xs text-muted">{a.model}</Mono> : <span className="text-xs text-subtle">runtime default</span>}
                      </Table.Td>
                    )}
                    <Table.Td>
                      {ps.length ? <span className="text-xs text-muted">{ps.map((p) => p.slug).join(", ")}</span> : <Chip tone="warn">not attached</Chip>}
                    </Table.Td>
                    <Table.Td className="text-right text-xs text-subtle">{relativeTime(a.created_at)}</Table.Td>
                  </Table.Row>
                );
              })}
            </tbody>
          </Table.Root>
        )}
      </Card>
    </>
  );
}

function AgentEditor({ agent }: { agent: Agent | null }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const confirm = useConfirm();
  const agents = useAgents();
  const connections = useConnections();
  const runtimes = useRuntimes();
  const providers = useProviders();
  const [template, setTemplate] = useState<string | null>(agent ? null : "bug-fixer");
  const initial = TEMPLATES.find((t) => t.key === template);
  const [name, setName] = useState(agent?.name ?? initial?.name ?? "");
  const [slug, setSlug] = useState(agent?.slug ?? "");
  const [slugTouched, setSlugTouched] = useState(!!agent);
  const [prompt, setPrompt] = useState(agent?.prompt ?? initial?.prompt ?? "");
  const [runtime, setRuntime] = useState(agent?.runtime ?? "claude_code");
  const [connIds, setConnIds] = useState<string[]>(agent?.connection_ids ?? []);
  const [providerId, setProviderId] = useState<string | null>(agent?.provider_id ?? null);
  // Only send provider_id to an API that has providers.
  const hasProviders = providers.isSuccess;
  const providerList = providers.data ?? [];
  const defaultProvider = providerList.find((p) => p.is_default);
  const runtimeSpec = runtimes.data?.find((r) => r.name === runtime);
  const usableProviders = providerList.filter((p) => runtimeAccepts(runtimeSpec, p));
  // The model list follows the provider the agent will use: its own, or the default.
  const effectiveProvider = providerList.find((p) => p.id === providerId) ?? defaultProvider;
  const providerKind = effectiveProvider?.kind ?? (providers.isSuccess ? null : "anthropic");
  const kindSpec = runtimeSpec?.providers?.find((k) => k.kind === providerKind) ?? providerKinds(runtimes.data).find((k) => k.kind === providerKind);
  // The provider's own list first, then its kind's catalog entry, then the static fallback.
  const models = modelsFor(effectiveProvider?.models?.length ? { kind: effectiveProvider.kind, models: effectiveProvider.models } : kindSpec, providerKind);
  const apiListsModels = !!(effectiveProvider?.models?.length || kindSpec?.models?.length);
  const modelSupported = agent ? "model" in agent : agents.data?.length ? agents.data.some((a) => "model" in a) : true;
  const [model, setModel] = useState<string | null>(agent?.model ?? null);
  const [otherMode, setOtherMode] = useState(false);
  const modelSel = otherMode ? OTHER : modelSelection(model, models);
  const modelOut = model?.trim() || null;
  const [promptTouched, setPromptTouched] = useState(!!agent);
  const [idName, setIdName] = useState(agent?.identity_name ?? agent?.name ?? "");
  // It follows the name only while creating; the API does not rename identities later.
  const [idNameTouched, setIdNameTouched] = useState(!!agent);
  const [idEmail, setIdEmail] = useState(agent?.identity_email ?? "");
  // Required on create; an agent from an API without identities has nothing to require.
  const hasIdentity = !agent || typeof agent.identity_email === "string";
  const effectiveIdName = idNameTouched ? idName : name;
  // The backfilled placeholder is flagged, but other edits can still be saved while it stays.
  const keepsPlaceholder = !!agent && needsEmail(agent) && idEmail.trim() === (agent.identity_email ?? "").trim();
  const idEmailError = keepsPlaceholder ? null : emailError(idEmail);
  const identityOk = !hasIdentity || (!!idEmail.trim() && !idEmailError);

  const effectiveSlug = slugTouched ? slug : slugify(name);
  const taken = agents.data?.some((a) => a.slug === effectiveSlug && a.id !== agent?.id);
  const slugError = !effectiveSlug ? null : !SLUG_RE.test(effectiveSlug) ? "Lowercase letters, digits, dot, dash and underscore." : taken ? "Another agent uses this slug." : null;
  const body = {
    name: name.trim(), slug: effectiveSlug, prompt, runtime, connection_ids: connIds,
    ...(hasProviders ? { provider_id: providerId } : {}),
    ...(modelSupported ? { model: modelOut } : {}),
    ...(hasIdentity ? { identity_name: effectiveIdName.trim() || name.trim(), identity_email: idEmail.trim() } : {}),
  };
  const identityDirty = !!agent && hasIdentity && ((effectiveIdName.trim() || name.trim()) !== (agent.identity_name ?? agent.name) || idEmail.trim() !== (agent.identity_email ?? ""));
  const dirty = !agent || identityDirty || (modelSupported && modelOut !== (agent.model ?? null)) || (hasProviders && providerId !== (agent.provider_id ?? null)) || name !== agent.name || effectiveSlug !== agent.slug || prompt !== agent.prompt || runtime !== agent.runtime || JSON.stringify([...connIds].sort()) !== JSON.stringify([...agent.connection_ids].sort());
  const chosen = (connections.data ?? []).filter((c) => connIds.includes(c.id));

  const applyTemplate = async (key: string) => {
    const t = TEMPLATES.find((x) => x.key === key);
    if (!t) return;
    if (promptTouched && prompt.trim() && !(await confirm({ title: "Replace the prompt?", description: `The current prompt is replaced with the ${t.name} starter.`, confirmLabel: "Replace", tone: "default" }))) return;
    setTemplate(key);
    setPrompt(t.prompt);
    setPromptTouched(false);
    if (!agent && (!name || TEMPLATES.some((x) => x.name === name))) setName(t.key === "blank" ? "" : t.name);
  };

  const save = useMutation({
    mutationFn: () => (agent ? api.agents.update(agent.id, body) : api.agents.create(body)),
    onSuccess: (a) => {
      qc.invalidateQueries({ queryKey: qk.agents });
      qc.invalidateQueries({ queryKey: qk.providers });
      toast.ok(agent ? "Agent saved" : `Agent ${a.slug} created`, agent ? undefined : "Attach it to a project to run it.");
      if (!agent) navigate({ to: "/agents", search: { edit: a.id }, replace: true });
    },
    onError: (e) => toast.err("Could not save the agent", e),
  });
  const remove = useMutation({
    mutationFn: () => api.agents.remove(agent!.id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.agents });
      toast.ok(`Agent ${agent!.slug} deleted`);
      navigate({ to: "/agents", search: {} });
    },
    onError: (e) => toast.err("Could not delete the agent", e),
  });

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        if (body.name && effectiveSlug && !slugError && identityOk) save.mutate();
      }}
      onKeyDown={(e) => {
        if ((e.metaKey || e.ctrlKey) && e.key === "s") {
          e.preventDefault();
          if (body.name && effectiveSlug && !slugError && identityOk && dirty) save.mutate();
        }
      }}
    >
      <PageHeader
        breadcrumb={
          <>
            <Link to="/agents" search={{}} className="hover:text-fg">Agents</Link>
            <Icon name="chevronRight" size={12} />
            <span className="text-muted">{agent?.slug ?? "new"}</span>
          </>
        }
        title={agent ? agent.name : "New agent"}
        actions={
          <>
            {agent && (
              <Button
                variant="ghost"
                icon="trash"
                onClick={async () => {
                  if (await confirm({ title: `Delete ${agent.slug}?`, description: "It is detached from every project. Tasks that name it will be rejected.", typeToConfirm: agent.slug, confirmLabel: "Delete agent" })) remove.mutate();
                }}
              >
                Delete
              </Button>
            )}
            <Button type="submit" variant="primary" loading={save.isPending} disabled={!dirty || !body.name || !effectiveSlug || !!slugError || !identityOk}>
              {agent ? "Save" : "Create agent"}
            </Button>
          </>
        }
      />
      <div className="grid grid-cols-[minmax(0,1fr)_340px] gap-6">
        <div className="flex min-w-0 flex-col gap-5">
          <Card className="p-4">
            <div className="grid grid-cols-2 gap-3">
              <Field label="Name"><Input autoFocus={!agent} value={name} onChange={(e) => setName(e.target.value)} placeholder="Bug fixer" /></Field>
              <Field label="Slug" error={slugError} hint={!slugError && "Tasks name the agent by this."}>
                <Input mono value={effectiveSlug} invalid={!!slugError} onChange={(e) => { setSlugTouched(true); setSlug(e.target.value.toLowerCase()); }} />
              </Field>
            </div>
            {(runtimes.data?.length ?? 0) > 1 && (
              <Field label="Runtime" className="mt-3">
                <PickList
                  aria-label="Runtime"
                  value={runtime}
                  onChange={(v) => {
                    if (!v) return;
                    setRuntime(v);
                    // A provider the new runtime cannot use falls back to the default.
                    const chosen = providerList.find((p) => p.id === providerId);
                    if (chosen && !runtimeAccepts(runtimes.data?.find((r) => r.name === v), chosen)) setProviderId(null);
                  }}
                  options={(runtimes.data ?? []).map((r) => ({ value: r.name, label: r.label }))}
                />
              </Field>
            )}
            {(providerList.length > 1 || (providerId !== null && providerList.length > 0)) && (
              <Field
                label="Provider"
                className="mt-3"
                warning={providerId === null && defaultProvider && !runtimeAccepts(runtimeSpec, defaultProvider) ? `${runtimeSpec?.label ?? runtime} cannot use the default provider ${defaultProvider.name}. Pick one it accepts.` : undefined}
                hint={providerId === null ? "Follows the default when it changes." : undefined}
              >
                <PickList
                  aria-label="Provider"
                  value={providerId}
                  onChange={setProviderId}
                  noneLabel={`Default (${defaultProvider?.name ?? "none"})`}
                  options={usableProviders.map((p) => ({ value: p.id, label: p.name, meta: p.is_default ? "default" : undefined }))}
                />
              </Field>
            )}
            {modelSupported && (
              <ModelField
                models={models}
                selection={modelSel}
                model={model}
                onSelect={(v) => {
                  if (v === OTHER) {
                    setOtherMode(true);
                    return;
                  }
                  setOtherMode(false);
                  setModel(v === RUNTIME_DEFAULT ? null : v);
                }}
                onOther={setModel}
                testProvider={apiListsModels && effectiveProvider ? { id: effectiveProvider.id, name: effectiveProvider.name } : null}
              />
            )}
          </Card>

          <Card>
            <CardHeader
              title="Identity"
              description="Who the agent is when it commits. Commits also carry Exradius-Task and Exradius-Agent trailers."
              actions={agent && needsEmail(agent) && idEmail.trim() === (agent.identity_email ?? "") ? <Chip tone="warn" dot>set an email</Chip> : undefined}
            />
            <div className="grid grid-cols-2 gap-3 p-4">
              <Field label="Name" htmlFor="identity-name" hint={!idNameTouched ? "Follows the agent name." : undefined}>
                <Input id="identity-name" value={effectiveIdName} onChange={(e) => { setIdNameTouched(true); setIdName(e.target.value); }} placeholder={name || "Bug fixer"} />
              </Field>
              <Field label="Email" htmlFor="identity-email" error={idEmailError} warning={keepsPlaceholder ? "A placeholder from the migration. Set the address commits should carry." : undefined}>
                <Input id="identity-email" type="email" value={idEmail} invalid={!!idEmailError} onChange={(e) => setIdEmail(e.target.value)} spellCheck={false} />
              </Field>
            </div>
          </Card>

          <Card>
            <CardHeader
              title="Prompt"
              description="Describe the agent's role and how it should work. It already knows how to ask questions, report results and use git."
              actions={<span className="text-xs tabular text-subtle">{prompt.length.toLocaleString()} chars</span>}
            />
            <div className="flex flex-wrap gap-1.5 border-b border-border px-4 py-2.5">
              <span className="mr-1 self-center text-xs text-subtle">Starters</span>
              {TEMPLATES.map((t) => (
                <Tooltip key={t.key} content={t.blurb}>
                  <button
                    type="button"
                    onClick={() => applyTemplate(t.key)}
                    className={cn(
                      "h-6 rounded-full border px-2.5 text-xs font-medium transition-colors",
                      template === t.key && !promptTouched ? "border-accent bg-accent-soft text-accent" : "border-border text-muted hover:border-border-strong hover:text-fg",
                    )}
                  >
                    {t.name}
                  </button>
                </Tooltip>
              ))}
            </div>
            <Textarea
              mono
              value={prompt}
              onChange={(e) => { setPrompt(e.target.value); setPromptTouched(true); }}
              spellCheck={false}
              className="min-h-[440px] resize-y rounded-none rounded-b-lg border-0 px-4 py-3 focus:ring-0"
              aria-label="Prompt"
            />
          </Card>

          <ConnectionsChecklist all={connections.data ?? []} selected={connIds} onChange={setConnIds} />
        </div>
        <GuidePanel connections={chosen} />
      </div>
    </form>
  );
}

function ModelField({ models, selection, model, onSelect, onOther, testProvider }: {
  models: ProviderModel[]; selection: string; model: string | null;
  onSelect: (v: string) => void; onOther: (v: string | null) => void;
  /** Set when the API takes a model on the provider test. */
  testProvider: { id: string; name: string } | null;
}) {
  const [testing, setTesting] = useState(false);
  const test = async () => {
    if (!testProvider || !model) return;
    setTesting(true);
    try {
      const t = await runProviderTest(testProvider.id, { model });
      if (!t) toast.err(`${model}: the test is still running`, "Its result is stored on the provider when it ends.");
      else if (testOk(t)) toast.ok(`${model} works with ${testProvider.name}`, testDetail(t.detail) ?? undefined);
      else toast.err(`${model} failed with ${testProvider.name}`, t.error ?? testDetail(t.detail) ?? undefined);
    } catch (e) {
      toast.err("The test did not run", e);
    } finally {
      setTesting(false);
    }
  };
  return (
    <Field label="Model" className="mt-3" hint={selection === RUNTIME_DEFAULT ? "The runtime picks; it follows the runtime's default when that changes." : undefined}>
      <div className="flex flex-col gap-2">
        <PickList
          aria-label="Model"
          value={selection}
          onChange={(v) => v && onSelect(v)}
          options={[
            { value: RUNTIME_DEFAULT, label: "Runtime default" },
            ...models.map((m) => ({ value: m.id, label: m.label || m.id, text: m.label || m.id, description: m.description ?? undefined, meta: m.label ? <Mono className="text-2xs text-subtle">{m.id}</Mono> : undefined })),
            { value: OTHER, label: "Other…", description: "A model id this list does not know" },
          ]}
        />
        {selection === OTHER && (
          <div className="flex gap-1.5">
            <Input
              mono
              autoFocus
              aria-label="Model id"
              value={model ?? ""}
              onChange={(e) => onOther(e.target.value || null)}
              placeholder={models[0]?.id ?? "model-id"}
              spellCheck={false}
              className="min-w-0 flex-1"
            />
          </div>
        )}
        {testProvider && model && (
          <div>
            <Button size="xs" variant="ghost" icon={testing ? undefined : "play"} disabled={testing} onClick={test}>
              {testing && <Spinner size={11} />}Test {modelLabel(model, models)} with {testProvider.name}
            </Button>
          </div>
        )}
      </div>
    </Field>
  );
}

function ConnectionsChecklist({ all, selected, onChange }: { all: Connection[]; selected: string[]; onChange: (ids: string[]) => void }) {
  const navigate = useNavigate();
  return (
    <Card>
      <CardHeader
        title="Connections"
        description="What this agent can reach. Override any of them per project when attaching."
        actions={<Button size="sm" variant="ghost" icon="plus" onClick={() => navigate({ to: "/connections", search: { create: "1" } })}>New connection</Button>}
      />
      {!all.length ? (
        <EmptyState compact icon="connections" title="No connections yet">
          Without connections, the agent has its runtime's own tools plus request_information and submit_result.
        </EmptyState>
      ) : (
        <ul className="divide-y divide-border">
          {all.map((c) => {
            const on = selected.includes(c.id);
            const t = c.last_test;
            return (
              <li key={c.id}>
                <label className={cn("flex cursor-pointer items-start gap-3 px-4 py-3 hover:bg-surface-2/50", on && "bg-accent-soft/30")}>
                  <Checkbox checked={on} onChange={(v) => onChange(v ? [...selected, c.id] : selected.filter((x) => x !== c.id))} aria-label={c.slug} />
                  <div className="-mt-0.5 min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium">{c.name}</span>
                      <Mono className="text-xs text-muted">{c.slug}</Mono>
                      {!t ? <Chip>not tested</Chip> : !t.ok ? <Chip tone="err" dot>failed</Chip> : null}
                    </div>
                    {t?.ok ? (
                      <div className="mt-1.5 flex flex-wrap gap-1">
                        {t.tools.slice(0, 10).map((tool) => <Chip key={tool.name} mono title={tool.description}>{tool.name}</Chip>)}
                        {t.tools.length > 10 && <Chip>+{t.tools.length - 10}</Chip>}
                      </div>
                    ) : t?.error ? (
                      <p className="mt-1 truncate font-mono text-[11.5px] text-err">{t.error}</p>
                    ) : null}
                  </div>
                </label>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

function GuidePanel({ connections }: { connections: Connection[] }) {
  return (
    <aside className="sticky top-6 flex max-h-[calc(100vh-48px)] flex-col gap-4 self-start overflow-y-auto">
      <Card>
        <CardHeader title="What the agent receives" description="The first message of a run: the task as it arrived." />
        <div className="p-3">
          <CodeBlock code={TASK_LAYOUT} wrap />
          <p className="mt-2 text-xs text-muted">It starts in <code className="font-mono">/work</code>, which holds one folder per repository.</p>
        </div>
      </Card>
      <Card>
        <CardHeader title="Connected tools" description="Names as the agent sees them." />
        <div className="flex flex-col gap-3 p-4">
          {!connections.length && <p className="text-xs text-subtle">No connections checked.</p>}
          {connections.map((c) => (
            <div key={c.id}>
              <div className="mb-1 flex items-center gap-2"><Mono className="text-xs font-medium">{c.slug}</Mono>{!c.last_test && <Chip>not tested</Chip>}</div>
              {c.last_test?.ok ? (
                <ul className="flex flex-col gap-0.5">
                  {c.last_test.tools.map((t) => (
                    <li key={t.name} className="truncate font-mono text-[11px] text-muted" title={t.description}>mcp__{c.slug}__{t.name}</li>
                  ))}
                </ul>
              ) : (
                <p className="text-xs text-subtle">{c.last_test ? "The last test failed." : "Test it to list its tools."}</p>
              )}
            </div>
          ))}
        </div>
      </Card>
    </aside>
  );
}
