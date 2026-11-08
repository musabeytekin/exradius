import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { PickList, Table } from "@/components/ui/controls";
import { Icon } from "@/components/ui/icons";
import { Tooltip } from "@/components/ui/overlays";
import { Banner, Button, Card, CardHeader, Chip, Field, Input, Mono, PageHeader, Skeleton, SkeletonRows, Spinner } from "@/components/ui/primitives";
import { api, ApiError, refusal } from "@/lib/api";
import { useConfirm } from "@/lib/confirm";
import { toast } from "@/lib/toast";
import { absoluteTime, duration, relativeTime } from "@/lib/format";
import { qk, useBridges, useGitCredentials, useIntegration, useIntegrationTokens, useRouting, useStatus } from "@/lib/queries";
import type { Bridge, GitCredential, GitCredentialKind, IntegrationToken, IntegrationTokenCreated, RoutingSettings, RoutingTest } from "@/lib/types";
import { CopyButton } from "@/components/ui/data";
import { useTheme, type ThemePref } from "@/lib/theme";
import { Segmented } from "@/components/ui/controls";

export function SettingsPage() {
  const status = useStatus();
  const integration = useIntegration();
  const { pref, setTheme } = useTheme();
  const s = status.data;
  const i = integration.data;
  const concurrency = s ? [...new Set(s.workers.map((w) => w.concurrency))] : [];

  const installation: [string, string, ReactNode][] = s ? [
    ["Public API URL", "Base of the signed artifact and transcript links in task.result.", <Mono key="u">{s.public_api_url}</Mono>],
    ["Public broker address", "Where integrations connect to publish tasks and read events.", i ? <Mono key="b">{i.broker.host}:{i.broker.port}{i.broker.vhost === "/" ? "" : `/${i.broker.vhost}`}</Mono> : <span key="b" className="text-xs text-subtle">not reported by the API</span>],
    ["Operator", "Turns runs into pods. Seen through its leader Lease.", !s.operator ? <span key="o" className="text-xs text-subtle">not reported by a worker</span> : <Chip key="o" tone={s.operator.alive ? "ok" : "err"} dot title={s.operator.detail ?? undefined}>{s.operator.alive ? `running${s.operator.holder ? ` (${s.operator.holder})` : ""}` : "not running"}</Chip>],
    ["Worker concurrency", "Runs each worker has in flight at once.", concurrency.length ? <Mono key="c">{concurrency.join(", ")}</Mono> : <span key="c" className="text-xs text-subtle">no live worker</span>],
  ] : [];

  return (
    <>
      <PageHeader title="Settings" />
      <div className="flex flex-col gap-6">
        <PasswordCard />
        <GitAccessCard />
        <RoutingCard />
        <IntegrationTokensCard />
        <BridgesCard />
        <Card>
          <CardHeader title="Appearance" description="Stored in this browser." />
          <div className="flex items-center justify-between px-4 py-3">
            <span className="text-sm">Theme</span>
            <Segmented<ThemePref>
              value={pref}
              onChange={setTheme}
              options={[{ value: "system", label: "System", icon: "monitor" }, { value: "light", label: "Light", icon: "sun" }, { value: "dark", label: "Dark", icon: "moon" }]}
            />
          </div>
        </Card>
        {status.isError ? (
          <Card className="px-4 py-6 text-center text-sm text-muted">The status endpoint did not answer: {(status.error as Error).message}</Card>
        ) : !s || integration.isLoading ? (
          <Skeleton className="h-96" />
        ) : (
          <>
            <Card>
              <CardHeader title="Installation" description="Fixed when Exradius was installed." />
              <Table.Root>
                <tbody>
                  {installation.map(([label, what, value]) => (
                    <Table.Row key={label}>
                      <Table.Td className="w-[220px] align-top text-sm font-medium">{label}</Table.Td>
                      <Table.Td className="align-top text-xs text-muted">{what}</Table.Td>
                      <Table.Td className="w-[360px] align-top text-sm">{value}</Table.Td>
                    </Table.Row>
                  ))}
                </tbody>
              </Table.Root>
            </Card>
            <Card>
              <CardHeader title="Live workers" description="Seen in the last 60 seconds." />
              {s.workers.length === 0 ? (
                <div className="p-4"><Banner tone="err" title="No live workers" /></div>
              ) : (
                <Table.Root>
                  <Table.Head>
                    <tr><Table.Th>Host</Table.Th><Table.Th>Version</Table.Th><Table.Th>Slots</Table.Th><Table.Th>Runtimes</Table.Th><Table.Th className="text-right">Last seen</Table.Th></tr>
                  </Table.Head>
                  <tbody>
                    {s.workers.map((wk) => (
                      <Table.Row key={wk.id}>
                        <Table.Td><Mono className="text-xs">{wk.hostname}</Mono></Table.Td>
                        <Table.Td><Mono className="text-xs text-muted">{wk.version ?? "—"}</Mono></Table.Td>
                        <Table.Td className="tabular">{wk.concurrency}</Table.Td>
                        <Table.Td>
                          <span className="flex flex-wrap items-center gap-1">
                            {(wk.runtimes ?? []).map((r) => <Chip key={r.name} mono tone={r.ready ? "neutral" : "err"} dot={!r.ready} title={r.error ?? r.image ?? undefined}>{r.name}{r.version ? ` ${r.version}` : ""}{r.ready ? "" : " not ready"}</Chip>)}
                            {!wk.runtimes?.length && <span className="text-xs text-subtle">none</span>}
                          </span>
                        </Table.Td>
                        <Table.Td className="text-right text-xs text-subtle">{relativeTime(wk.last_seen_at)}</Table.Td>
                      </Table.Row>
                    ))}
                  </tbody>
                </Table.Root>
              )}
            </Card>
          </>
        )}
      </div>
    </>
  );
}

function PasswordCard() {
  const me = useQuery({ queryKey: qk.me, queryFn: api.auth.me });
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<{ field: "current" | "next"; message: string } | null>(null);
  const change = useMutation({
    // Values are read from state, not passed as mutation variables, so they are not kept in the cache.
    mutationFn: () => api.auth.changePassword(current, next),
    onSuccess: () => {
      setCurrent(""); setNext(""); setRepeat(""); setError(null);
      toast.ok("Password changed", "Other sessions stay signed in until they expire.");
    },
    onError: (e) => {
      const status = e instanceof ApiError ? e.status : 0;
      // 400: the current password is wrong; 422: the new one is rejected.
      setError({ field: status === 400 || status === 401 || status === 403 ? "current" : "next", message: e.message });
    },
  });
  const mismatch = !!repeat && repeat !== next;
  const same = !!next && next === current;
  const short = !!next && next.length < 8;
  const ready = !!current && !!next && repeat === next && !same && !short;

  return (
    <Card>
      <CardHeader title="Password" description={<>Signed in as <Mono>{me.data?.username ?? "…"}</Mono>.</>} />
      <form
        className="grid grid-cols-[1fr_1fr_1fr_auto] items-start gap-3 px-4 py-4"
        onSubmit={(e) => { e.preventDefault(); if (ready) change.mutate(); }}
      >
        <Field label="Current password" error={error?.field === "current" ? error.message : undefined}>
          <Input type="password" autoComplete="current-password" value={current} invalid={error?.field === "current"} onChange={(e) => { setCurrent(e.target.value); setError(null); }} />
        </Field>
        <Field label="New password" hint="At least 8 characters." error={error?.field === "next" ? error.message : same ? "Same as the current one." : undefined}>
          <Input type="password" autoComplete="new-password" value={next} invalid={error?.field === "next" || same} onChange={(e) => { setNext(e.target.value); setError(null); }} />
        </Field>
        <Field label="Repeat new password" error={mismatch ? "Does not match." : undefined}>
          <Input type="password" autoComplete="new-password" value={repeat} invalid={mismatch} onChange={(e) => setRepeat(e.target.value)} />
        </Field>
        <Button type="submit" variant="primary" className="mt-[22px]" loading={change.isPending} disabled={!ready}>Change password</Button>
      </form>
    </Card>
  );
}

const notAvailable = (e: unknown) => e instanceof ApiError && (e.status === 404 || e.status === 405);

function GitAccessCard() {
  const creds = useGitCredentials();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [adding, setAdding] = useState(false);
  const remove = useMutation({
    mutationFn: (c: GitCredential) => api.gitCredentials.remove(c.id),
    onSuccess: (_, c) => {
      qc.invalidateQueries({ queryKey: qk.gitCredentials });
      toast.ok(`Credential for ${c.host} deleted`);
    },
    onError: (e) => toast.err("Could not delete the credential", e),
  });
  const list = creds.data ?? [];
  return (
    <Card>
      <CardHeader
        title="Config repository access"
        description="One credential per git host, used to read config repositories. Values are write-only."
        actions={!creds.isError && !adding && <Button size="sm" variant="secondary" icon="plus" onClick={() => setAdding(true)}>Add host</Button>}
      />
      {creds.isLoading ? (
        <SkeletonRows rows={2} />
      ) : creds.isError ? (
        <p className="px-4 py-4 text-sm text-muted">
          {notAvailable(creds.error) ? "This API has no git credentials." : `Could not load credentials: ${(creds.error as Error).message}`}
        </p>
      ) : (
        <>
          {list.length > 0 && (
            <Table.Root>
              <Table.Head>
                <tr><Table.Th>Host</Table.Th><Table.Th>Kind</Table.Th><Table.Th>Added</Table.Th><Table.Th className="w-12" /></tr>
              </Table.Head>
              <tbody>
                {list.map((c) => (
                  <Table.Row key={c.id}>
                    <Table.Td><Mono className="font-medium">{c.host}</Mono></Table.Td>
                    <Table.Td><Chip>{c.kind === "basic" ? "username and password" : "token"}</Chip></Table.Td>
                    <Table.Td className="text-xs text-subtle">{c.created_at ? relativeTime(c.created_at) : ""}</Table.Td>
                    <Table.Td className="text-right">
                      <Button
                        size="sm"
                        variant="ghost"
                        square
                        icon="trash"
                        aria-label={`Delete the credential for ${c.host}`}
                        onClick={async () => {
                          const ok = await confirm({ title: `Delete the credential for ${c.host}?`, description: "Config repositories on this host are read anonymously afterwards.", confirmLabel: "Delete" });
                          if (ok) remove.mutate(c);
                        }}
                      />
                    </Table.Td>
                  </Table.Row>
                ))}
              </tbody>
            </Table.Root>
          )}
          {list.length === 0 && !adding && <p className="px-4 py-4 text-sm text-subtle">No hosts. Config repositories are read anonymously.</p>}
          {adding && <AddGitCredential taken={list.map((c) => c.host)} onDone={() => setAdding(false)} />}
        </>
      )}
    </Card>
  );
}

const HOST_RE = /^[A-Za-z0-9.-]+(:\d+)?$/;

function AddGitCredential({ taken, onDone }: { taken: string[]; onDone: () => void }) {
  const qc = useQueryClient();
  const [host, setHost] = useState("");
  const [kind, setKind] = useState<GitCredentialKind>("token");
  const [token, setToken] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [serverError, setServerError] = useState<string | null>(null);
  const h = host.trim().toLowerCase();
  const hostError = !h ? null : !HOST_RE.test(h) ? "A host name such as github.com, without scheme or path." : taken.includes(h) ? "This host has a credential. Delete it first to replace it." : null;
  const value = kind === "token" ? token : username && password ? `${username}:${password}` : "";
  const add = useMutation({
    // Values are read from state, not passed as variables, so they are not kept in the mutation cache.
    mutationFn: () => api.gitCredentials.create({ host: h, kind, value }),
    onSuccess: () => {
      setToken(""); setPassword("");
      qc.invalidateQueries({ queryKey: qk.gitCredentials });
      toast.ok(`Credential for ${h} saved`);
      onDone();
    },
    onError: (e) => { const m = refusal(e); if (m) setServerError(m); else toast.err("Could not save the credential", e); },
  });
  const ready = !!h && !hostError && !!value;
  return (
    <form
      className="flex flex-col gap-3 border-t border-border px-4 py-4 first:border-t-0"
      onSubmit={(e) => { e.preventDefault(); if (ready) add.mutate(); }}
    >
      <div className="grid grid-cols-[1fr_auto] items-start gap-3">
        <Field label="Host" error={hostError ?? serverError}>
          <Input mono autoFocus value={host} invalid={!!hostError} onChange={(e) => { setHost(e.target.value); setServerError(null); }} placeholder="github.com" spellCheck={false} />
        </Field>
        <Field label="Kind">
          <Segmented<GitCredentialKind>
            value={kind}
            onChange={(k) => { setKind(k); setServerError(null); }}
            options={[{ value: "token", label: "Token" }, { value: "basic", label: "Username and password" }]}
          />
        </Field>
      </div>
      {kind === "token" ? (
        <Field label="Token">
          <Input type="password" autoComplete="off" value={token} onChange={(e) => setToken(e.target.value)} />
        </Field>
      ) : (
        <div className="grid grid-cols-2 gap-3">
          <Field label="Username">
            <Input autoComplete="off" value={username} onChange={(e) => setUsername(e.target.value)} spellCheck={false} />
          </Field>
          <Field label="Password">
            <Input type="password" autoComplete="off" value={password} onChange={(e) => setPassword(e.target.value)} />
          </Field>
        </div>
      )}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={onDone}>Cancel</Button>
        <Button type="submit" variant="primary" loading={add.isPending} disabled={!ready}>Save</Button>
      </div>
    </form>
  );
}

export function RoutingCard() {
  const routing = useRouting();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [replacing, setReplacing] = useState(false);
  const test = useMutation({
    mutationFn: () => api.routing.test(),
    onSuccess: (t) => qc.setQueryData<RoutingSettings>(qk.routing, (r) => (r ? { ...r, last_test: t } : r)),
    onError: (e) => toast.err("The test did not run", e),
  });
  const remove = useMutation({
    mutationFn: () => api.routing.removeKey(),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.routing });
      toast.ok("OpenAI API key deleted");
    },
    onError: (e) => toast.err("Could not delete the key", e),
  });
  const r = routing.data;
  return (
    <Card>
      <CardHeader
        title="Agent routing"
        actions={r?.key_set && <Button size="sm" variant="secondary" icon="play" loading={test.isPending} onClick={() => test.mutate()}>Test</Button>}
      />
      {routing.isLoading ? (
        <SkeletonRows rows={2} />
      ) : routing.isError || !r ? (
        <p className="px-4 py-4 text-sm text-muted">
          {notAvailable(routing.error) ? "This API has no agent routing." : `Could not load agent routing: ${(routing.error as Error)?.message}`}
        </p>
      ) : (
        <dl className="grid grid-cols-[minmax(110px,auto)_1fr] gap-x-6 gap-y-2 px-4 py-3 text-sm">
          <dt className="text-muted">Model</dt>
          <dd><Mono>{r.model}</Mono></dd>
          <dt className="pt-1 text-muted">OpenAI API key</dt>
          <dd className="flex min-h-8 flex-wrap items-center gap-2">
            {r.key_set && !replacing ? (
              <>
                <Chip tone="ok" dot>set</Chip>
                {r.key_set_at && <Tooltip content={absoluteTime(r.key_set_at)}><span className="text-xs text-subtle">{relativeTime(r.key_set_at)}</span></Tooltip>}
                <Button size="xs" variant="ghost" onClick={() => setReplacing(true)}>Replace</Button>
                <Button
                  size="xs"
                  variant="ghost"
                  square
                  icon="trash"
                  aria-label="Delete the OpenAI API key"
                  loading={remove.isPending}
                  onClick={async () => {
                    const ok = await confirm({ title: "Delete the OpenAI API key?", description: "Tasks that name no agent are rejected for projects with more than one agent.", confirmLabel: "Delete" });
                    if (ok) remove.mutate();
                  }}
                />
              </>
            ) : (
              <>
                {!r.key_set && <Chip tone="warn" dot>not set</Chip>}
                <RoutingKeyForm
                  replacing={r.key_set}
                  onCancel={r.key_set ? () => setReplacing(false) : undefined}
                  onSaved={(next) => {
                    qc.setQueryData(qk.routing, next);
                    setReplacing(false);
                    toast.ok("OpenAI API key saved");
                    test.mutate();
                  }}
                />
              </>
            )}
          </dd>
          {r.key_set && (
            <>
              <dt className="text-muted">Last test</dt>
              <dd>{test.isPending ? <Chip tone="info"><Spinner size={10} />testing</Chip> : <RoutingTestResult t={r.last_test} />}</dd>
            </>
          )}
        </dl>
      )}
    </Card>
  );
}

function RoutingTestResult({ t }: { t: RoutingTest | null }) {
  if (!t) return <Chip tone="warn" dot>not tested</Chip>;
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <span className="flex items-center gap-2">
        <Chip tone={t.ok ? "ok" : "err"} dot>{t.ok ? "passed" : "failed"}</Chip>
        {t.ms != null && <span className="text-xs tabular text-muted">{duration(t.ms)}</span>}
        <Tooltip content={absoluteTime(t.at)}><span className="text-xs text-subtle">{relativeTime(t.at)}</span></Tooltip>
      </span>
      <span className={t.ok ? "break-words font-mono text-[11.5px] text-muted" : "break-words font-mono text-[11.5px] text-err"}>{t.detail}</span>
    </div>
  );
}

function RoutingKeyForm({ replacing, onCancel, onSaved }: { replacing: boolean; onCancel?: () => void; onSaved: (r: RoutingSettings) => void }) {
  const [key, setKey] = useState("");
  const [serverError, setServerError] = useState<string | null>(null);
  const save = useMutation({
    // The key is read from state, not passed as a variable, so it is not kept in the mutation cache.
    mutationFn: () => api.routing.setKey(key.trim()),
    onSuccess: (r) => { setKey(""); onSaved(r); },
    onError: (e) => { const m = refusal(e); if (m) setServerError(m); else toast.err("Could not save the key", e); },
  });
  const ready = !!key.trim();
  return (
    <form className="flex min-w-0 flex-1 flex-col gap-1" onSubmit={(e) => { e.preventDefault(); if (ready) save.mutate(); }}>
      <div className="flex items-center gap-2">
        <Input
          type="password"
          autoComplete="off"
          autoFocus={replacing}
          aria-label={replacing ? "New OpenAI API key" : "OpenAI API key"}
          invalid={!!serverError}
          value={key}
          onChange={(e) => { setKey(e.target.value); setServerError(null); }}
          className="w-80"
        />
        <Button type="submit" variant="primary" size="sm" loading={save.isPending} disabled={!ready}>Save</Button>
        {onCancel && <Button variant="ghost" size="sm" onClick={onCancel}>Cancel</Button>}
      </div>
      {serverError && <p className="flex items-start gap-1 text-xs text-err"><Icon name="alert" size={13} className="mt-0.5 shrink-0" />{serverError}</p>}
    </form>
  );
}

export function IntegrationTokensCard() {
  const tokens = useIntegrationTokens();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [adding, setAdding] = useState(false);
  const [created, setCreated] = useState<IntegrationTokenCreated | null>(null);
  const revoke = useMutation({
    mutationFn: (t: IntegrationToken) => api.integrationTokens.revoke(t.id),
    onSuccess: (_, t) => {
      qc.invalidateQueries({ queryKey: qk.integrationTokens });
      qc.invalidateQueries({ queryKey: qk.bridges });
      setCreated((c) => (c?.id === t.id ? null : c));
      toast.ok(`Token ${t.name} revoked`);
    },
    onError: (e) => toast.err("Could not revoke the token", e),
  });
  const list = tokens.data ?? [];
  return (
    <Card>
      <CardHeader
        title="Integration tokens"
        description={<>Bearer tokens for <Mono>/api/integration/v1</Mono>.</>}
        actions={!tokens.isError && !adding && !created && <Button size="sm" variant="secondary" icon="plus" onClick={() => setAdding(true)}>New token</Button>}
      />
      {tokens.isLoading ? (
        <SkeletonRows rows={2} />
      ) : tokens.isError ? (
        <p className="px-4 py-4 text-sm text-muted">
          {notAvailable(tokens.error) ? "This API has no integration tokens." : `Could not load tokens: ${(tokens.error as Error).message}`}
        </p>
      ) : (
        <>
          {created && (
            <div className="border-b border-border px-4 py-3">
              <Banner
                tone="ok"
                title={<>Token <Mono>{created.name}</Mono>, shown once</>}
                action={<Button size="sm" variant="ghost" onClick={() => setCreated(null)}>Done</Button>}
              >
                <span className="mt-1 flex min-w-0 items-center gap-1">
                  <Mono className="break-all text-fg">{created.token}</Mono>
                  <CopyButton text={created.token} label="" />
                </span>
              </Banner>
            </div>
          )}
          {list.length > 0 && (
            <Table.Root>
              <Table.Head>
                <tr><Table.Th>Name</Table.Th><Table.Th>Bridge</Table.Th><Table.Th>Created</Table.Th><Table.Th>Last used</Table.Th><Table.Th className="w-24" /></tr>
              </Table.Head>
              <tbody>
                {list.map((t) => (
                  <Table.Row key={t.id}>
                    <Table.Td><Mono className={t.revoked_at ? "text-subtle line-through" : "font-medium"}>{t.name}</Mono></Table.Td>
                    <Table.Td>{t.bridge ? <Mono className="text-xs text-muted">{t.bridge}</Mono> : <span className="text-xs text-subtle">—</span>}</Table.Td>
                    <Table.Td className="text-xs text-subtle">{relativeTime(t.created_at)}</Table.Td>
                    <Table.Td className="text-xs text-subtle">{t.last_used_at ? relativeTime(t.last_used_at) : "never"}</Table.Td>
                    <Table.Td className="text-right">
                      {t.revoked_at ? <Chip>revoked</Chip> : (
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-label={`Revoke the token ${t.name}`}
                          onClick={async () => {
                            const ok = await confirm({ title: `Revoke the token ${t.name}?`, description: "Requests with it are refused from now on.", confirmLabel: "Revoke" });
                            if (ok) revoke.mutate(t);
                          }}
                        >
                          Revoke
                        </Button>
                      )}
                    </Table.Td>
                  </Table.Row>
                ))}
              </tbody>
            </Table.Root>
          )}
          {list.length === 0 && !adding && !created && <p className="px-4 py-4 text-sm text-subtle">No tokens.</p>}
          {adding && (
            <NewIntegrationToken
              taken={list.map((t) => t.name)}
              onCancel={() => setAdding(false)}
              onCreated={(t) => { setAdding(false); setCreated(t); }}
            />
          )}
        </>
      )}
    </Card>
  );
}

const BRIDGE_RE = /^[a-z][a-z0-9-]*$/;

/** The bridge to preselect: the one configured in projects that has no token yet, or the only one. */
function suggestedBridge(bridges: Bridge[]): string | null {
  const unbound = bridges.filter((b) => b.projects.length > 0 && b.tokens === 0);
  if (unbound.length === 1) return unbound[0].bridge;
  return bridges.length === 1 ? bridges[0].bridge : null;
}

function NewIntegrationToken({ taken, onCancel, onCreated }: {
  taken: string[]; onCancel: () => void; onCreated: (t: IntegrationTokenCreated) => void;
}) {
  const qc = useQueryClient();
  const bridges = useBridges();
  const known = bridges.data ?? [];
  const [picked, setPicked] = useState<string | null>(null);
  const [typing, setTyping] = useState(false);
  const [typed, setTyped] = useState("");
  const [name, setName] = useState("");
  const [serverError, setServerError] = useState<string | null>(null);
  // Typed when no bridge is known yet, or when the admin asks for another one.
  const free = typing || bridges.isError || (bridges.isSuccess && known.length === 0);
  const bridge = free ? typed.trim() : picked ?? suggestedBridge(known) ?? "";
  const bridgeError = bridge && (!BRIDGE_RE.test(bridge) || bridge.length > 100) ? "Lowercase letters, digits and dashes, starting with a letter." : null;
  const n = name.trim();
  const effective = n || bridge;
  const nameError = effective && taken.includes(effective) ? "A token with this name exists." : null;
  const create = useMutation({
    mutationFn: () => api.integrationTokens.create({ bridge, name: n || null }),
    onSuccess: (t) => {
      qc.invalidateQueries({ queryKey: qk.integrationTokens });
      qc.invalidateQueries({ queryKey: qk.bridges });
      onCreated(t);
    },
    onError: (e) => { const m = refusal(e); if (m) setServerError(m); else toast.err("Could not create the token", e); },
  });
  const ready = !!bridge && !bridgeError && n.length <= 100 && !nameError;
  return (
    <form
      className="flex items-start gap-3 border-t border-border px-4 py-4 first:border-t-0"
      onSubmit={(e) => { e.preventDefault(); if (ready) create.mutate(); }}
    >
      <Field label="Bridge" htmlFor="token-bridge" className="flex-1" error={bridgeError}>
        {free ? (
          <Input
            id="token-bridge"
            mono
            autoFocus
            value={typed}
            maxLength={100}
            invalid={!!bridgeError}
            onChange={(e) => { setTyped(e.target.value); setServerError(null); }}
            placeholder="jira"
            spellCheck={false}
            trailing={typing && known.length > 0 && (
              <Button size="xs" variant="ghost" square icon="list" aria-label="Known bridges" onClick={() => { setTyping(false); setServerError(null); }} />
            )}
          />
        ) : (
          <PickList
            id="token-bridge"
            mono
            value={bridge || null}
            placeholder={bridges.isLoading ? "…" : "Choose a bridge"}
            disabled={bridges.isLoading}
            onChange={(v) => { setPicked(v); setServerError(null); }}
            options={known.map((b) => ({
              value: b.bridge,
              label: b.bridge,
              description: b.projects.length ? b.projects.join(", ") : undefined,
              meta: b.tokens ? `${b.tokens} token${b.tokens === 1 ? "" : "s"}` : undefined,
            }))}
            actions={[{ key: "new", label: "Another bridge", onSelect: () => { setTyping(true); setTyped(""); setServerError(null); } }]}
          />
        )}
      </Field>
      <Field label="Name" htmlFor="token-name" optional className="flex-1" error={nameError ?? serverError}>
        <Input id="token-name" mono value={name} maxLength={100} invalid={!!nameError} onChange={(e) => { setName(e.target.value); setServerError(null); }} placeholder={bridge} spellCheck={false} />
      </Field>
      <div className="mt-[22px] flex gap-2">
        <Button variant="ghost" onClick={onCancel}>Cancel</Button>
        <Button type="submit" variant="primary" loading={create.isPending} disabled={!ready}>Create</Button>
      </div>
    </form>
  );
}

export function BridgesCard() {
  const bridges = useBridges();
  const list = bridges.data ?? [];
  return (
    <Card>
      <CardHeader title="Bridges" />
      {bridges.isLoading ? (
        <SkeletonRows rows={2} />
      ) : bridges.isError ? (
        <p className="px-4 py-4 text-sm text-muted">
          {notAvailable(bridges.error) ? "This API has no bridges." : `Could not load bridges: ${(bridges.error as Error).message}`}
        </p>
      ) : list.length === 0 ? (
        <p className="px-4 py-4 text-sm text-subtle">No bridges.</p>
      ) : (
        <ul className="divide-y divide-border">
          {list.map((b) => <BridgeRow key={b.bridge} b={b} />)}
        </ul>
      )}
    </Card>
  );
}

function BridgeRow({ b }: { b: Bridge }) {
  return (
    <li className="px-4 py-3" aria-label={b.bridge}>
      <div className="flex items-start gap-4">
        <Mono className="w-36 shrink-0 pt-0.5 font-medium">{b.bridge}</Mono>
        <dl className="grid min-w-0 flex-1 grid-cols-[auto_1fr] gap-x-5 gap-y-1 text-xs">
          <dt className="text-muted">Projects</dt>
          <dd className="flex flex-wrap gap-1">{b.projects.length ? b.projects.map((p) => <Chip key={p} mono>{p}</Chip>) : <span className="text-subtle">none</span>}</dd>
          <dt className="text-muted">Tokens</dt>
          <dd className="tabular">{b.tokens}</dd>
          <dt className="text-muted">Last report</dt>
          <dd className="flex flex-wrap items-center gap-2">
            {!b.reported_at ? (
              <span className="text-subtle">no reports</span>
            ) : b.reporting ? (
              <Tooltip content={absoluteTime(b.reported_at)}><span>{relativeTime(b.reported_at)}</span></Tooltip>
            ) : (
              <Chip tone="warn" dot>not reporting since {absoluteTime(b.reported_at)}</Chip>
            )}
            {b.software && <Mono className="break-all text-xs text-muted">{b.software}</Mono>}
          </dd>
        </dl>
      </div>
      {b.problems.length > 0 && (
        <ul aria-label={`Problems reported by ${b.bridge}`} className="mt-2 flex flex-col gap-1 pl-40">
          {b.problems.map((p, i) => (
            <li key={i} className="flex items-start gap-2 text-xs">
              <Icon name="alert" size={13} className="mt-0.5 shrink-0 text-warn" />
              <Mono className="shrink-0 text-xs">{p.project ?? b.bridge}</Mono>
              <span className="min-w-0 whitespace-pre-wrap break-words text-fg">{p.message}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}
