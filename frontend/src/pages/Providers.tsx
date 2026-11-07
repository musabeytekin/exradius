import { useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useEffect, useRef, useState } from "react";
import { ProviderDialog } from "@/components/ProviderForm";
import { Table } from "@/components/ui/controls";
import { Dialog, Menu, Tooltip } from "@/components/ui/overlays";
import { Banner, Button, Card, Chip, EmptyState, Mono, PageHeader, SkeletonRows, Spinner } from "@/components/ui/primitives";
import { api, ApiError } from "@/lib/api";
import { useConfirm } from "@/lib/confirm";
import { relativeTime } from "@/lib/format";
import { describeProvider, providerKinds, testDetail } from "@/lib/providers";
import { runProviderTest, testOk } from "@/lib/providerTest";
import { qk, useProviders, useRuntimes } from "@/lib/queries";
import { toast } from "@/lib/toast";
import type { Provider, ProviderIn, ProviderTest } from "@/lib/types";

/** `null` while a synchronous test runs, the test id while the worker runs one. */
type Testing = Record<string, string | null>;

function without(m: Testing, id: string): Testing {
  const next = { ...m };
  delete next[id];
  return next;
}


export function ProvidersPage() {
  const providers = useProviders();
  const runtimes = useRuntimes();
  const { edit, create } = useSearch({ from: "/app/providers" });
  const navigate = useNavigate();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [testing, setTesting] = useState<Testing>({});
  const [blocked, setBlocked] = useState<{ provider: Provider; detail: string } | null>(null);
  const alive = useRef(true);
  useEffect(() => () => { alive.current = false; }, []);

  const kinds = providerKinds(runtimes.data);
  const list = providers.data ?? [];
  const editing = edit ? list.find((p) => p.id === edit) ?? null : null;
  const dialogOpen = !!create || !!editing;
  const open = (search: { edit?: string; create?: string }) => navigate({ to: "/providers", search, replace: true });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: qk.providers });
    qc.invalidateQueries({ queryKey: qk.status });
  };
  /** Agents in project configs that name this provider, as the API counts them. */
  const usersOf = (p: Provider): number => p.agent_count ?? 0;

  const finish = (p: Provider, r: ProviderTest) => {
    setTesting((m) => without(m, p.id));
    refresh();
    const detail = testDetail(r.detail);
    if (testOk(r)) toast.ok(`${p.name}: test passed`, detail ?? undefined);
    else toast.err(`${p.name}: test failed`, r.error ?? detail ?? undefined);
  };

  const runTest = async (p: Provider) => {
    setTesting((m) => ({ ...m, [p.id]: null }));
    try {
      const t = await runProviderTest(p.id, { alive: () => alive.current, onQueued: (tid) => setTesting((m) => ({ ...m, [p.id]: tid })) });
      if (t) return finish(p, t);
      if (alive.current) {
        setTesting((m) => without(m, p.id));
        toast.err(`${p.name}: the test is still running`, "Its result is stored on the provider when it ends.");
      }
    } catch (e) {
      setTesting((m) => without(m, p.id));
      toast.err("The test did not run", e);
    }
  };

  const save = async (body: ProviderIn) => {
    const before = editing;
    const p = before ? await api.providers.update(before.id, body) : await api.providers.create(body);
    refresh();
    toast.ok(before ? `${p.name} saved` : `${p.name} created`, !before && p.is_default ? "It is the default provider." : undefined);
    open({});
    const changed = !before || !!body.credential || body.kind !== before.kind || body.auth_method !== before.auth_method ||
      JSON.stringify(body.settings) !== JSON.stringify(before.settings ?? {});
    if (changed) void runTest(p);
  };

  const makeDefault = async (p: Provider) => {
    try {
      await api.providers.makeDefault(p.id);
      refresh();
      toast.ok(`${p.name} is the default`, "Agents without their own provider use it from the next run.");
    } catch (e) {
      toast.err("Could not change the default", e);
    }
  };

  const remove = async (p: Provider) => {
    const users = usersOf(p);
    const ok = await confirm({
      title: `Delete ${p.name}?`,
      description: users
        ? `${users} agent${users === 1 ? "" : "s"} in project configs ${users === 1 ? "names" : "name"} it.`
        : p.is_default && list.length === 1
          ? "It is the only provider. Tasks are rejected until you add another."
          : "Its credential is deleted.",
      confirmLabel: "Delete provider",
    });
    if (!ok) return;
    try {
      await api.providers.remove(p.id);
      refresh();
      toast.ok(`${p.name} deleted`);
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) setBlocked({ provider: p, detail: e.message });
      else toast.err("Could not delete the provider", e);
    }
  };

  const noKinds = runtimes.isSuccess && kinds.length === 0;

  return (
    <>
      <PageHeader
        title="Providers"
        description="Model credentials. A run gets its agent's provider, or the default one; the credential is injected into the run container when it starts."
        actions={<Button variant="primary" icon="plus" disabled={noKinds} onClick={() => open({ create: "1" })}>New provider</Button>}
      />
      {providers.isError ? (
        <Banner tone="err" title="Providers did not load">{(providers.error as Error).message}</Banner>
      ) : providers.isLoading ? (
        <Card><SkeletonRows rows={2} /></Card>
      ) : !list.length ? (
        <Card>
          <EmptyState
            icon="brain"
            title="No provider yet"
            action={<Button variant="primary" icon="plus" disabled={noKinds} onClick={() => open({ create: "1" })}>Add a provider</Button>}
          >
            Tasks are rejected until one exists. The first provider becomes the default.
          </EmptyState>
        </Card>
      ) : (
        <Card>
          <Table.Root>
            <Table.Head>
              <tr>
                <Table.Th>Provider</Table.Th>
                <Table.Th>Authentication</Table.Th>
                <Table.Th>Last test</Table.Th>
                <Table.Th>Agents</Table.Th>
                <Table.Th />
              </tr>
            </Table.Head>
            <tbody>
              {list.map((p) => {
                const d = describeProvider(kinds, p);
                const t = p.last_test;
                const busy = p.id in testing;
                const detail = t ? (t.ok ? testDetail(t.detail) : t.error ?? testDetail(t.detail)) : null;
                return (
                  <Table.Row key={p.id} onClick={() => open({ edit: p.id })}>
                    <Table.Td>
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{p.name}</span>
                        {p.is_default && <Chip tone="accent">default</Chip>}
                      </div>
                      <Mono className="text-xs text-muted">{p.slug}</Mono>
                    </Table.Td>
                    <Table.Td>
                      <div className="text-sm">{d.kindLabel}</div>
                      <div className="text-xs text-muted">{d.methodLabel}</div>
                    </Table.Td>
                    <Table.Td className="max-w-[340px]">
                      {busy ? (
                        <Chip tone="info"><Spinner size={10} />{testing[p.id] ? "testing in a run container" : "testing"}</Chip>
                      ) : !t ? (
                        <Chip tone="warn" dot>not tested</Chip>
                      ) : (
                        <div className="flex min-w-0 flex-col gap-1">
                          <span className="flex items-center gap-2">
                            <Chip tone={t.ok ? "ok" : "err"} dot>{t.ok ? "passed" : "failed"}</Chip>
                            <span className="text-xs text-subtle">{relativeTime(t.tested_at)}</span>
                          </span>
                          {detail && (
                            <Tooltip content={detail}>
                              <span className={t.ok ? "truncate font-mono text-[11.5px] text-muted" : "truncate font-mono text-[11.5px] text-err"}>{detail}</span>
                            </Tooltip>
                          )}
                        </div>
                      )}
                    </Table.Td>
                    <Table.Td>
                      <span className="text-xs text-subtle">{p.agent_count ? `${p.agent_count} agent${p.agent_count === 1 ? "" : "s"}` : p.agent_count === 0 ? "none" : "—"}</span>
                    </Table.Td>
                    <Table.Td className="w-[1%] whitespace-nowrap text-right">
                      <span className="inline-flex gap-0.5" onClick={(e) => e.stopPropagation()}>
                        <Button size="xs" variant="ghost" icon="play" loading={busy} onClick={() => runTest(p)}>Test</Button>
                        <Menu.Root>
                          <Menu.Trigger asChild><Button size="xs" variant="ghost" square icon="settings" aria-label={`${p.slug} actions`} /></Menu.Trigger>
                          <Menu.Content>
                            <Menu.Item icon="edit" onSelect={() => open({ edit: p.id })}>Edit</Menu.Item>
                            {!p.is_default && <Menu.Item icon="check" onSelect={() => makeDefault(p)}>Make default</Menu.Item>}
                            <Menu.Separator />
                            <Menu.Item danger icon="trash" onSelect={() => remove(p)}>Delete</Menu.Item>
                          </Menu.Content>
                        </Menu.Root>
                      </span>
                    </Table.Td>
                  </Table.Row>
                );
              })}
            </tbody>
          </Table.Root>
        </Card>
      )}
      {noKinds && (
        <Banner tone="warn" title="No provider kinds offered" className="mt-4">
          No runtime on this installation declares a provider it accepts, so none can be added.
        </Banner>
      )}

      {dialogOpen && (
        <ProviderDialog
          key={editing?.id ?? "new"}
          open
          onOpenChange={(o) => !o && open({})}
          kinds={kinds}
          provider={editing}
          takenSlugs={list.map((p) => p.slug)}
          onSave={save}
        />
      )}

      <Dialog
        open={!!blocked}
        onOpenChange={(o) => !o && setBlocked(null)}
        title={blocked ? `${blocked.provider.name} was not deleted` : ""}
        footer={<Button variant="primary" onClick={() => setBlocked(null)}>OK</Button>}
      >
        {blocked && <BlockedExplanation provider={blocked.provider} detail={blocked.detail} users={usersOf(blocked.provider)} others={list.filter((p) => p.id !== blocked.provider.id)} onMakeDefault={async (p) => { await makeDefault(p); setBlocked(null); }} />}
      </Dialog>
    </>
  );
}

function BlockedExplanation({ provider, detail, users, others, onMakeDefault }: {
  provider: Provider; detail: string; users: number; others: Provider[]; onMakeDefault: (p: Provider) => void;
}) {
  const inUse = users > 0;
  return (
    <div className="flex flex-col gap-3 text-sm">
      {inUse && <p className="text-muted">Agents in project configs still name it.</p>}
      {provider.is_default && others.length > 0 && (
        <div>
          <p className="text-muted">It is the default. Make another provider the default first.</p>
          <div className="mt-2 flex flex-wrap gap-2">
            {others.map((p) => <Button key={p.id} size="sm" variant="secondary" icon="check" onClick={() => onMakeDefault(p)}>Make {p.name} default</Button>)}
          </div>
        </div>
      )}
      {!inUse && !(provider.is_default && others.length > 0) && <p className="text-muted">The API refused it.</p>}
      <p className="font-mono text-xs text-subtle">{detail}</p>
    </div>
  );
}
