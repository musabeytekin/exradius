import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { KeyValueEditor } from "@/components/KeyValueEditor";
import { newRow, rowsFrom, rowsTo, type Row } from "@/lib/rows";
import { useSecretIndex } from "@/lib/useSecretIndex";
import { Checkbox, PickList, Segmented, SuggestInput } from "@/components/ui/controls";
import { CodeBlock } from "@/components/ui/data";
import { Icon } from "@/components/ui/icons";
import { Dialog, Menu, Sheet, Tooltip } from "@/components/ui/overlays";
import { Banner, Button, Card, Chip, EmptyState, Field, Input, Mono, PageHeader, SkeletonRows, Spinner, Textarea } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { CATALOG, COMMAND_SUGGESTIONS, fieldValue, parseMcpServers, type CatalogEntry, type ImportedServer } from "@/lib/catalog";
import { cn } from "@/lib/cn";
import { useConfirm } from "@/lib/confirm";
import { pretty, relativeTime } from "@/lib/format";
import { qk, useAgents, useConnections, useProjects } from "@/lib/queries";
import { summarize, validateFields } from "@/lib/secretRefs";
import { SLUG_RE, slugify } from "@/lib/slug";
import { toast } from "@/lib/toast";
import type { Connection, ConnectionIn, ConnectionTestResult, Transport } from "@/lib/types";

const NOTICE = "Connections that reach production systems should use read-only credentials.";

export function ConnectionsPage() {
  const connections = useConnections();
  const agents = useAgents();
  const { edit, create } = useSearch({ from: "/app/connections" });
  const navigate = useNavigate();
  const [importing, setImporting] = useState(false);
  const [testing, setTesting] = useState<Set<string>>(new Set());
  const qc = useQueryClient();
  const confirm = useConfirm();
  const editing = edit ? connections.data?.find((c) => c.id === edit) : undefined;
  const open = (search: { edit?: string; create?: string }) => navigate({ to: "/connections", search, replace: true });

  const test = async (c: Connection) => {
    setTesting((s) => new Set(s).add(c.id));
    try {
      const r = await api.connections.test(c.id);
      if (r.ok) toast.ok(`${c.slug}: ${r.tools.length} tools`);
      else toast.err(`${c.slug}: test failed`, r.error);
    } catch (e) {
      toast.err("The test did not run", e);
    } finally {
      setTesting((s) => {
        const n = new Set(s);
        n.delete(c.id);
        return n;
      });
      qc.invalidateQueries({ queryKey: qk.connections });
    }
  };

  const remove = useMutation({
    mutationFn: (c: Connection) => api.connections.remove(c.id),
    onSuccess: (_, c) => {
      qc.invalidateQueries({ queryKey: qk.connections });
      qc.invalidateQueries({ queryKey: qk.agents });
      toast.ok(`Connection ${c.slug} deleted`);
    },
    onError: (e) => toast.err("Could not delete the connection", e),
  });

  const usedBy = (c: Connection) => (agents.data ?? []).filter((a) => a.connection_ids.includes(c.id));

  return (
    <>
      <PageHeader
        title="Connections"
        description="MCP servers that agents use. An agent can do exactly what its connections allow, plus the built-in request_information and submit_result."
        actions={
          <>
            <Button variant="secondary" icon="import" onClick={() => setImporting(true)}>Import JSON</Button>
            <Button variant="primary" icon="plus" onClick={() => open({ create: "1" })}>Add connection</Button>
          </>
        }
      />
      {connections.isLoading ? (
        <Card><SkeletonRows rows={3} /></Card>
      ) : !connections.data?.length ? (
        <Card>
          <EmptyState
            icon="connections"
            title="No connections"
            action={
              <>
                <Button variant="primary" icon="plus" onClick={() => open({ create: "1" })}>Add from the catalog</Button>
                <Button variant="secondary" icon="import" onClick={() => setImporting(true)}>Paste an mcpServers block</Button>
              </>
            }
          >
            Pick GitHub, Postgres, Sentry and others from the catalog, or paste the JSON block a server's README gives you.
          </EmptyState>
        </Card>
      ) : (
        <div className="grid grid-cols-2 gap-4 2xl:grid-cols-3">
          {connections.data.map((c) => {
            const t = c.last_test;
            const users = usedBy(c);
            return (
              <Card key={c.id} className="flex flex-col transition-colors hover:border-border-strong">
                <button type="button" onClick={() => open({ edit: c.id })} className="flex flex-1 flex-col gap-3 p-4 text-left">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <div className="font-medium text-fg">{c.name}</div>
                      <Mono className="text-xs text-muted">{c.slug}</Mono>
                    </div>
                    <Chip>{c.transport}</Chip>
                  </div>
                  <Mono className="line-clamp-2 break-all rounded-md bg-code px-2 py-1.5 text-[11.5px] text-muted">
                    {c.transport === "stdio" ? [c.command, ...c.args].join(" ") : c.url}
                  </Mono>
                  {t?.ok && t.tools.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {t.tools.slice(0, 6).map((tool) => <Chip key={tool.name} mono>{tool.name}</Chip>)}
                      {t.tools.length > 6 && <Chip>+{t.tools.length - 6}</Chip>}
                    </div>
                  )}
                  {t && !t.ok && <p className="line-clamp-2 font-mono text-[11.5px] text-err">{t.error}</p>}
                </button>
                <div className="flex items-center gap-2 border-t border-border px-4 py-2">
                  {testing.has(c.id) ? (
                    <Chip tone="info"><Spinner size={10} />testing</Chip>
                  ) : !t ? (
                    <Chip>not tested</Chip>
                  ) : (
                    <Tooltip content={`Tested ${relativeTime(t.tested_at)}`}>
                      <span><Chip tone={t.ok ? "ok" : "err"} dot>{t.ok ? `${t.tools.length} tools` : "failed"}</Chip></span>
                    </Tooltip>
                  )}
                  <span className="truncate text-xs text-subtle">{users.length ? `used by ${users.map((a) => a.slug).join(", ")}` : "not used"}</span>
                  <div className="ml-auto flex gap-0.5">
                    <Button size="xs" variant="ghost" icon="play" loading={testing.has(c.id)} onClick={() => test(c)}>Test</Button>
                    <Menu.Root>
                      <Menu.Trigger asChild><Button size="xs" variant="ghost" square icon="settings" aria-label={`${c.slug} actions`} /></Menu.Trigger>
                      <Menu.Content>
                        <Menu.Item icon="edit" onSelect={() => open({ edit: c.id })}>Edit</Menu.Item>
                        <Menu.Separator />
                        <Menu.Item
                          danger
                          icon="trash"
                          onSelect={async () => {
                            const ok = await confirm({
                              title: `Delete ${c.slug}?`,
                              description: users.length ? `Agents ${users.map((a) => a.slug).join(", ")} lose this connection, and its overrides in projects stop applying.` : "No agent uses it.",
                              confirmLabel: "Delete connection",
                            });
                            if (ok) remove.mutate(c);
                          }}
                        >
                          Delete
                        </Menu.Item>
                      </Menu.Content>
                    </Menu.Root>
                  </div>
                </div>
              </Card>
            );
          })}
        </div>
      )}
      {(create || editing) && (
        <ConnectionSheet
          key={editing?.id ?? "new"}
          connection={editing ?? null}
          existing={connections.data ?? []}
          onClose={() => open({})}
        />
      )}
      {importing && <ImportDialog existing={connections.data ?? []} onClose={() => setImporting(false)} />}
    </>
  );
}

// --- Form -----------------------------------------------------------------------------------

interface Draft {
  catalog: string | null;
  name: string;
  slug: string;
  slugTouched: boolean;
  transport: Transport;
  command: string;
  args: string;
  url: string;
  env: Row[];
  headers: Row[];
}

function draftFrom(c: Connection | null): Draft {
  return {
    catalog: null,
    name: c?.name ?? "",
    slug: c?.slug ?? "",
    slugTouched: !!c,
    transport: (c?.transport as Transport) ?? "stdio",
    command: c?.command ?? "",
    args: (c?.args ?? []).join("\n"),
    url: c?.url ?? "",
    env: rowsFrom(c?.env ?? {}),
    headers: rowsFrom(c?.headers ?? {}),
  };
}

function draftFromCatalog(e: CatalogEntry): Draft {
  return {
    catalog: e.key,
    name: e.name,
    slug: e.slug,
    slugTouched: false,
    transport: e.transport,
    command: e.command ?? "",
    args: (e.args ?? []).join("\n"),
    url: e.url ?? "",
    env: (e.env ?? []).map((f) => newRow(f.key, fieldValue(f))),
    headers: (e.headers ?? []).map((f) => newRow(f.key, fieldValue(f))),
  };
}

function bodyOf(d: Draft): ConnectionIn {
  return {
    name: d.name.trim(),
    slug: d.slugTouched ? d.slug : slugify(d.name),
    transport: d.transport,
    command: d.transport === "stdio" ? d.command.trim() || null : null,
    args: d.transport === "stdio" ? d.args.split("\n").map((a) => a.trim()).filter(Boolean) : [],
    url: d.transport === "http" ? d.url.trim() || null : null,
    env: rowsTo(d.env),
    headers: d.transport === "http" ? rowsTo(d.headers) : {},
  };
}

function ConnectionSheet({ connection, existing, onClose }: { connection: Connection | null; existing: Connection[]; onClose: () => void }) {
  const qc = useQueryClient();
  const projects = useProjects();
  const { index, globals } = useSecretIndex();
  const [saved, setSaved] = useState<Connection | null>(connection);
  const [draft, setDraft] = useState<Draft>(() => draftFrom(connection));
  const [picking, setPicking] = useState(!connection);
  const [testProject, setTestProject] = useState<string | null>(null);
  const [result, setResult] = useState<ConnectionTestResult | null>(null);
  const [busy, setBusy] = useState<null | "save" | "test">(null);
  const set = (patch: Partial<Draft>) => setDraft((d) => ({ ...d, ...patch }));

  const body = bodyOf(draft);
  const slugTaken = existing.some((c) => c.slug === body.slug && c.id !== saved?.id);
  const slugError = !body.slug ? null : body.slug === "exradius" ? "exradius is reserved for the built-in server." : !SLUG_RE.test(body.slug) ? "Lowercase letters, digits, dot, dash and underscore." : slugTaken ? "Another connection uses this slug." : null;
  const validations = useMemo(() => validateFields({ ...body.env, ...body.headers }, index), [body.env, body.headers, index]);
  const refs = summarize(validations);
  const catalogEntry = CATALOG.find((e) => e.key === draft.catalog);
  const createKeys = Object.fromEntries([...(catalogEntry?.env ?? []), ...(catalogEntry?.headers ?? [])].filter((f) => f.secret).map((f) => [f.key, f.secret!]));
  const missingTarget = draft.transport === "stdio" ? !body.command : !body.url;
  const canSave = !!body.name && !!body.slug && !slugError && !missingTarget && refs.invalid.length === 0;

  const insertNames = [
    ...globals.map((s) => ({ name: s.key, scope: "global" as const })),
    ...[...index.project.entries()].filter(([n]) => !index.global.has(n)).map(([name, ps]) => ({ name, scope: "project" as const, where: ps.join(", ") })),
  ];

  const saveAndTest = async () => {
    setBusy("save");
    setResult(null);
    try {
      const c = saved ? await api.connections.update(saved.id, body) : await api.connections.create(body);
      setSaved(c);
      qc.invalidateQueries({ queryKey: qk.connections });
      setBusy("test");
      const r = await api.connections.test(c.id, testProject ? { project_id: testProject } : undefined);
      setResult(r);
      qc.invalidateQueries({ queryKey: qk.connections });
    } catch (e) {
      toast.err("Could not save the connection", e);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Sheet
      open
      onOpenChange={(o) => !o && onClose()}
      title={connection ? `Edit ${connection.slug}` : "Add a connection"}
      description={saved && !connection ? `Saved as ${saved.slug}.` : undefined}
      width={780}
      onSubmit={() => canSave && saveAndTest()}
      footer={
        <>
          {saved && result?.ok && <span className="mr-auto flex items-center gap-1.5 text-xs text-ok"><Icon name="check" size={13} />Saved and tested</span>}
          <Button variant="secondary" onClick={onClose}>{saved ? "Close" : "Cancel"}</Button>
          <Button type="submit" variant="primary" loading={!!busy} disabled={!canSave || picking}>
            {busy === "test" ? "Testing…" : saved ? "Save and test" : "Create and test"}
          </Button>
        </>
      }
    >
      {picking ? (
        <div className="px-6 py-5">
          <p className="mb-4 text-sm text-muted">Start from a common server. Picking one fills the command and lists the secrets it needs.</p>
          <div className="grid grid-cols-3 gap-2">
            {CATALOG.map((e) => (
              <button
                key={e.key}
                type="button"
                onClick={() => {
                  setDraft(draftFromCatalog(e));
                  setPicking(false);
                }}
                className="flex flex-col gap-1 rounded-lg border border-border bg-surface p-3 text-left transition-colors hover:border-accent hover:bg-accent-soft/40"
              >
                <div className="flex items-center justify-between">
                  <span className="text-sm font-medium">{e.name}</span>
                  <span className="text-2xs text-subtle">{e.transport}</span>
                </div>
                <span className="line-clamp-2 text-xs text-muted">{e.description}</span>
                {e.requires && (
                  <span className="mt-1 inline-flex items-center gap-1 text-[10.5px] text-warn" title={`The default project image has no ${e.requires}. Use a project image that has it on PATH.`}>
                    <Icon name="alert" size={10} />needs {e.requires} in the image
                  </span>
                )}
                {[...(e.env ?? []), ...(e.headers ?? [])].some((f) => f.secret) && (
                  <span className="mt-1 flex flex-wrap gap-1">
                    {[...(e.env ?? []), ...(e.headers ?? [])].filter((f) => f.secret).map((f) => (
                      <span key={f.secret} className="inline-flex items-center gap-1 font-mono text-[10.5px] text-subtle"><Icon name="key" size={10} />{f.secret}</span>
                    ))}
                  </span>
                )}
              </button>
            ))}
            <button
              type="button"
              onClick={() => setPicking(false)}
              className="flex flex-col items-start gap-1 rounded-lg border border-dashed border-border-strong p-3 text-left transition-colors hover:border-accent"
            >
              <span className="text-sm font-medium">Custom</span>
              <span className="text-xs text-muted">Any stdio command or HTTP endpoint.</span>
            </button>
          </div>
        </div>
      ) : (
        <div className="flex flex-col gap-5 px-6 py-5">
          {catalogEntry && (
            <div className="flex items-center gap-2 text-xs text-muted">
              <Icon name="sparkle" size={13} className="text-accent" />
              From the catalog: <span className="font-medium text-fg">{catalogEntry.name}</span>
              {!connection && <button type="button" className="text-accent hover:underline" onClick={() => setPicking(true)}>Change</button>}
            </div>
          )}
          {!connection && !catalogEntry && (
            <button type="button" onClick={() => setPicking(true)} className="flex items-center gap-1 self-start text-xs text-accent hover:underline">
              <Icon name="chevronLeft" size={12} /> Back to the catalog
            </button>
          )}
          <div className="grid grid-cols-2 gap-3">
            <Field label="Name"><Input autoFocus={!connection} value={draft.name} onChange={(e) => set({ name: e.target.value })} placeholder="GitHub (read-only)" /></Field>
            <Field label="Slug" error={slugError} hint={!slugError && "Becomes the MCP server name: tools appear as mcp__<slug>__<tool>."}>
              <Input mono value={body.slug} invalid={!!slugError} onChange={(e) => set({ slug: e.target.value.toLowerCase(), slugTouched: true })} />
            </Field>
          </div>
          <Field label="Transport">
            <Segmented
              value={draft.transport}
              onChange={(t) => set({ transport: t })}
              options={[
                { value: "stdio", label: "stdio", icon: "terminal" },
                { value: "http", label: "HTTP", icon: "link" },
              ]}
            />
          </Field>
          {draft.transport === "stdio" ? (
            <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-3">
              <Field label="Command">
                <SuggestInput mono value={draft.command} onChange={(command) => set({ command })} suggestions={COMMAND_SUGGESTIONS.map((c) => ({ value: c }))} placeholder="npx" />
              </Field>
              <Field label="Arguments" hint="One per line. Started inside the run container, in /work.">
                <Textarea mono rows={Math.max(3, draft.args.split("\n").length)} value={draft.args} onChange={(e) => set({ args: e.target.value })} placeholder={"-y\n@modelcontextprotocol/server-github"} spellCheck={false} />
              </Field>
            </div>
          ) : (
            <Field label="URL"><Input mono value={draft.url} onChange={(e) => set({ url: e.target.value })} placeholder="https://mcp.example.com/mcp" spellCheck={false} /></Field>
          )}
          <Field label="Environment" hint={<>Use <code className="font-mono">{"${secret:NAME}"}</code> for credentials. The key button inserts one.</>}>
            <KeyValueEditor rows={draft.env} onChange={(env) => set({ env })} index={index} names={insertNames} keyPlaceholder="VARIABLE" valuePlaceholder="value or ${secret:NAME}" createKeys={createKeys} />
          </Field>
          {draft.transport === "http" && (
            <Field label="Headers">
              <KeyValueEditor rows={draft.headers} onChange={(headers) => set({ headers })} index={index} names={insertNames} keyPlaceholder="Authorization" valuePlaceholder="Bearer ${secret:TOKEN}" createKeys={createKeys} addLabel="Add header" />
            </Field>
          )}
          {(refs.unknown.length > 0 || refs.projectOnly.length > 0) && (
            <Banner tone="warn" title="Some references do not resolve globally">
              {refs.unknown.length > 0 && <div>Unknown: <span className="font-mono">{refs.unknown.join(", ")}</span>. Create them, or the connection fails to start.</div>}
              {refs.projectOnly.length > 0 && <div>Project-only: <span className="font-mono">{refs.projectOnly.join(", ")}</span>. They resolve only in projects that define them; attaching checks again.</div>}
            </Banner>
          )}
          <Banner tone="neutral" icon="shield">{connection?.notice ?? NOTICE}</Banner>

          <div className="flex flex-col gap-3 rounded-lg border border-border p-3">
            <div className="flex items-center gap-3">
              <span className="text-sm font-medium">Test</span>
              <span className="text-xs text-muted">Saving starts the server in the API container and lists its tools.</span>
              <div className="ml-auto w-56">
                <PickList
                  size="sm"
                  value={testProject}
                  onChange={setTestProject}
                  noneLabel="With global secrets"
                  options={(projects.data ?? []).map((p) => ({ value: p.id, label: `With ${p.slug} secrets`, text: `With ${p.slug} secrets` }))}
                />
              </div>
            </div>
            {busy === "test" && <div className="flex items-center gap-2 text-sm text-muted"><Spinner />Starting {body.slug} and listing its tools (up to 60 s)…</div>}
            {result && !busy && <TestResult result={result} />}
            {!result && !busy && saved?.last_test && (
              <div className="text-xs text-subtle">
                Last test {relativeTime(saved.last_test.tested_at)}: {saved.last_test.ok ? `${saved.last_test.tools.length} tools` : saved.last_test.error}
              </div>
            )}
          </div>
          <details className="group">
            <summary className="flex cursor-pointer list-none items-center gap-1.5 text-xs font-medium text-muted hover:text-fg">
              <Icon name="chevronRight" size={12} className="transition-transform group-open:rotate-90" />
              Request body
            </summary>
            <CodeBlock className="mt-2" lang="json" code={pretty(body)} />
          </details>
        </div>
      )}
    </Sheet>
  );
}

function TestResult({ result }: { result: ConnectionTestResult }) {
  if (!result.ok) {
    return (
      <Banner tone="err" title="The server did not start">
        <pre className="mt-1 whitespace-pre-wrap break-words font-mono text-xs">{result.error}</pre>
      </Banner>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2 text-sm text-ok"><Icon name="check" size={14} />Started. {result.tools.length} tools available.</div>
      <ul className="max-h-64 overflow-y-auto rounded-md border border-border">
        {result.tools.map((t) => (
          <li key={t.name} className="flex gap-3 border-b border-border px-3 py-1.5 text-xs last:border-b-0">
            <Mono className="shrink-0 text-xs">{t.name}</Mono>
            <span className="truncate text-muted">{t.description}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

// --- Import ---------------------------------------------------------------------------------

function ImportDialog({ existing, onClose }: { existing: Connection[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const [skip, setSkip] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const parsed = useMemo(() => (text.trim() ? parseMcpServers(text) : null), [text]);
  const servers = parsed?.servers ?? [];
  const conflict = (s: ImportedServer) => existing.some((c) => c.slug === s.slug) || s.slug === "exradius";
  const chosen = servers.filter((s) => !skip.has(s.slug) && !conflict(s));

  const run = async () => {
    setBusy(true);
    const created: string[] = [];
    for (const s of chosen) {
      try {
        const c = await api.connections.create(s);
        created.push(c.slug);
        api.connections.test(c.id).finally(() => qc.invalidateQueries({ queryKey: qk.connections }));
      } catch (e) {
        toast.err(`Could not import ${s.slug}`, e);
      }
    }
    qc.invalidateQueries({ queryKey: qk.connections });
    setBusy(false);
    if (created.length) {
      toast.ok(`Imported ${created.length} connection${created.length === 1 ? "" : "s"}`, "Each is being tested now.");
      onClose();
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title="Import an mcpServers block"
      description="Paste the JSON a server's README gives for Claude Code, Cursor or Claude Desktop. Each entry becomes a connection."
      width={680}
      onSubmit={() => chosen.length && run()}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" loading={busy} disabled={!chosen.length}>
            Import {chosen.length || ""} connection{chosen.length === 1 ? "" : "s"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Textarea
          autoFocus
          mono
          rows={9}
          value={text}
          onChange={(e) => setText(e.target.value)}
          invalid={!!parsed?.error}
          spellCheck={false}
          placeholder={`{\n  "mcpServers": {\n    "sentry": {\n      "command": "npx",\n      "args": ["-y", "@sentry/mcp-server"],\n      "env": { "SENTRY_ACCESS_TOKEN": "\${secret:SENTRY_ACCESS_TOKEN}" }\n    }\n  }\n}`}
        />
        {parsed?.error && <p className="text-xs text-err">{parsed.error}</p>}
        {servers.length > 0 && (
          <ul className="flex flex-col divide-y divide-border rounded-lg border border-border">
            {servers.map((s) => {
              const clash = conflict(s);
              const literalSecrets = Object.entries({ ...s.env, ...s.headers }).filter(([, v]) => v && !v.includes("${secret:") && /token|key|secret|password|auth/i.test(v + " ")).length;
              return (
                <li key={s.slug} className={cn("flex items-center gap-3 px-3 py-2", clash && "opacity-60")}>
                  <Checkbox
                    checked={!skip.has(s.slug) && !clash}
                    disabled={clash}
                    aria-label={`Import ${s.slug}`}
                    onChange={(v) => setSkip((sk) => { const n = new Set(sk); if (v) n.delete(s.slug); else n.add(s.slug); return n; })}
                  />
                  <Mono className="font-medium">{s.slug}</Mono>
                  <Chip>{s.transport}</Chip>
                  <Mono className="min-w-0 flex-1 truncate text-xs text-muted">{s.transport === "stdio" ? [s.command, ...s.args].join(" ") : s.url}</Mono>
                  {clash ? <Chip tone="warn">slug exists</Chip> : literalSecrets > 0 ? (
                    <Tooltip content="Values that look like credentials are stored as plain text. Edit the connection afterwards to use ${secret:NAME}.">
                      <span><Chip tone="warn">literal credentials?</Chip></span>
                    </Tooltip>
                  ) : null}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </Dialog>
  );
}
