import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { Table } from "@/components/ui/controls";
import { Icon } from "@/components/ui/icons";
import { Dialog } from "@/components/ui/overlays";
import { Banner, Button, Card, CardHeader, EmptyState, Field, Input, Mono, PageHeader, Skeleton, SkeletonRows } from "@/components/ui/primitives";
import { api, ApiError, refusal } from "@/lib/api";
import { buttonClass } from "@/lib/buttonStyles";
import { useConfirm } from "@/lib/confirm";
import { relativeTime } from "@/lib/format";
import { SECRET_KEY_RE, STORE_NAME_RE, storeNameOf } from "@/lib/projectConfig";
import { qk, useProjects, useStoreKeys, useStores } from "@/lib/queries";
import { toast } from "@/lib/toast";
import type { SecretStore, SecretStoreKey } from "@/lib/types";
import { KeyValueSetter } from "./project/SecretsTab";

const notAvailable = (e: unknown) => e instanceof ApiError && (e.status === 404 || e.status === 405);

/** The store's key count, from the list response or its key list. */
function keyCount(s: SecretStore, loaded?: unknown[]): number | null {
  return s.key_count ?? s.keys?.length ?? loaded?.length ?? null;
}

export function StoresPage() {
  const stores = useStores();
  const projects = useProjects();
  const { create } = useSearch({ from: "/app/stores" });
  const navigate = useNavigate();
  const [open, setOpen] = useState(!!create);
  const list = stores.data ?? [];
  const usedBy = (s: SecretStore) => s.used_by ?? (projects.data ?? []).filter((p) => storeNameOf(p) === s.name).map((p) => p.slug);
  const setDialog = (o: boolean) => {
    setOpen(o);
    if (!o && create) navigate({ to: "/stores", search: {}, replace: true });
  };

  return (
    <>
      <PageHeader title="Stores" actions={<Button variant="primary" icon="plus" disabled={stores.isError} onClick={() => setOpen(true)}>New store</Button>} />
      <Card>
        {stores.isLoading ? (
          <SkeletonRows rows={3} />
        ) : stores.isError ? (
          <p className="px-4 py-6 text-center text-sm text-muted">
            {notAvailable(stores.error) ? "This API has no secret stores." : `Could not load stores: ${(stores.error as Error).message}`}
          </p>
        ) : list.length === 0 ? (
          <EmptyState icon="key" title="No stores" action={<Button variant="primary" icon="plus" onClick={() => setOpen(true)}>New store</Button>} />
        ) : (
          <Table.Root>
            <Table.Head>
              <tr><Table.Th>Store</Table.Th><Table.Th>Keys</Table.Th><Table.Th>Used by</Table.Th><Table.Th className="text-right">Created</Table.Th></tr>
            </Table.Head>
            <tbody>
              {list.map((s) => <StoreRow key={s.id} store={s} usedBy={usedBy(s)} />)}
            </tbody>
          </Table.Root>
        )}
      </Card>
      {open && <CreateStoreDialog taken={list.map((s) => s.name)} onOpenChange={setDialog} />}
    </>
  );
}

function StoreRow({ store, usedBy }: { store: SecretStore; usedBy: string[] }) {
  const navigate = useNavigate();
  const known = store.key_count ?? store.keys?.length;
  const keys = useStoreKeys(known === undefined || known === null ? store.id : null);
  const n = keyCount(store, keys.data);
  return (
    <Table.Row onClick={() => navigate({ to: "/stores/$storeId", params: { storeId: store.id } })}>
      <Table.Td><Mono className="font-medium">{store.name}</Mono></Table.Td>
      <Table.Td className="tabular">{n ?? "—"}</Table.Td>
      <Table.Td>{usedBy.length ? <Mono className="text-xs text-muted">{usedBy.join(", ")}</Mono> : <span className="text-xs text-subtle">—</span>}</Table.Td>
      <Table.Td className="text-right text-xs text-subtle">{store.created_at ? relativeTime(store.created_at) : ""}</Table.Td>
    </Table.Row>
  );
}

function CreateStoreDialog({ taken, onOpenChange }: { taken: string[]; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [serverError, setServerError] = useState<string | null>(null);
  const n = name.trim();
  const error = !n ? null : !STORE_NAME_RE.test(n) ? "Lowercase letters, digits and dashes, starting with a letter." : taken.includes(n) ? "A store with this name exists." : serverError;
  const create = useMutation({
    mutationFn: () => api.stores.create(n),
    onSuccess: (s) => {
      qc.invalidateQueries({ queryKey: qk.stores });
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok(`Store ${s?.name ?? n} created`);
      onOpenChange(false);
      if (s?.id) navigate({ to: "/stores/$storeId", params: { storeId: s.id } });
    },
    onError: (e) => { const m = refusal(e); if (m) setServerError(m); else toast.err("Could not create the store", e); },
  });
  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title="New store"
      onSubmit={() => n && !error && create.mutate()}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={create.isPending} disabled={!n || !!error}>Create store</Button>
        </>
      }
    >
      <Field label="Name" htmlFor="store-name" error={error}>
        <Input id="store-name" mono autoFocus value={name} invalid={!!error} onChange={(e) => { setName(e.target.value.toLowerCase()); setServerError(null); }} placeholder="bank-platform" spellCheck={false} />
      </Field>
    </Dialog>
  );
}

export function StoreDetailPage() {
  const { storeId } = useParams({ from: "/app/stores/$storeId" });
  const navigate = useNavigate();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const stores = useStores();
  const projects = useProjects();
  const store = stores.data?.find((s) => s.id === storeId);
  const keys = useStoreKeys(storeId);
  const [refused, setRefused] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);

  const remove = useMutation({
    mutationFn: () => api.stores.remove(storeId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.stores });
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok(`Store ${store?.name} deleted`);
      navigate({ to: "/stores", search: {} });
    },
    onError: (e) => { const m = refusal(e); if (m) setRefused(m); else toast.err("Could not delete the store", e); },
  });

  const removeKey = useMutation({
    mutationFn: (key: string) => api.stores.removeKey(storeId, key),
    onSuccess: (_, key) => {
      qc.invalidateQueries({ queryKey: qk.storeKeys(storeId) });
      qc.invalidateQueries({ queryKey: qk.stores });
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok(`${key} deleted`);
    },
    onError: (e) => toast.err("Could not delete the key", e),
  });

  if (stores.isLoading) return <Skeleton className="h-64" />;
  if (!store) {
    return (
      <EmptyState icon="key" title="Store not found" action={<Link to="/stores" search={{}} className={buttonClass("secondary")}>All stores</Link>}>
        {stores.isError ? (stores.error as Error).message : "It may have been deleted."}
      </EmptyState>
    );
  }
  const usedBy = (projects.data ?? []).filter((p) => storeNameOf(p) === store.name);
  const list: SecretStoreKey[] = (keys.data ?? []).map((k) => (typeof k === "string" ? { key: k } : k));
  const names = list.map((k) => k.key);

  return (
    <>
      <PageHeader
        breadcrumb={<><Link to="/stores" search={{}} className="hover:text-fg">Stores</Link><Icon name="chevronRight" size={12} /><span className="text-muted">{store.name}</span></>}
        title={<span className="font-mono">{store.name}</span>}
        meta={
          usedBy.length > 0 && (
            <span className="flex items-center gap-1.5 text-xs text-subtle">
              Named by
              {usedBy.map((p, i) => (
                <span key={p.id}>
                  <Link to="/projects/$projectId" params={{ projectId: p.id }} search={{ tab: "secrets" }} className="font-mono text-muted hover:text-fg hover:underline">{p.slug}</Link>
                  {i < usedBy.length - 1 ? "," : ""}
                </span>
              ))}
            </span>
          )
        }
        actions={
          <>
            <Button variant="primary" icon="plus" onClick={() => setAdding(true)}>Add key</Button>
            <Button
              variant="secondary"
              icon="trash"
              loading={remove.isPending}
              onClick={async () => {
                setRefused(null);
                const ok = await confirm({
                  title: `Delete store ${store.name}?`,
                  description: "Every value in it is deleted.",
                  typeToConfirm: store.name,
                  confirmLabel: "Delete store",
                });
                if (ok) remove.mutate();
              }}
            >
              Delete store
            </Button>
          </>
        }
      />
      {refused && <Banner tone="err" title="The store was not deleted" className="mb-4"><span className="font-mono text-xs">{refused}</span></Banner>}
      <Card>
        <CardHeader title="Keys" description="Values are write-only." />
        {keys.isLoading ? (
          <SkeletonRows rows={3} />
        ) : keys.isError ? (
          <p className="px-4 py-6 text-center text-sm text-err">Could not load keys: {(keys.error as Error).message}</p>
        ) : list.length === 0 ? (
          <EmptyState compact icon="key" title="No keys" action={<Button size="sm" variant="secondary" icon="plus" onClick={() => setAdding(true)}>Add key</Button>} />
        ) : (
          <Table.Root>
            <Table.Head>
              <tr><Table.Th>Key</Table.Th><Table.Th>Updated</Table.Th><Table.Th className="w-[420px]">Value</Table.Th><Table.Th className="w-12" /></tr>
            </Table.Head>
            <tbody>
              {list.map((k) => (
                <Table.Row key={k.key}>
                  <Table.Td><Mono className="font-medium">{k.key}</Mono></Table.Td>
                  <Table.Td className="text-xs text-subtle">{k.updated_at ? relativeTime(k.updated_at) : ""}</Table.Td>
                  <Table.Td><KeyValueSetter store={store} keyName={k.key} present /></Table.Td>
                  <Table.Td className="text-right">
                    <Button
                      size="sm"
                      variant="ghost"
                      square
                      icon="trash"
                      aria-label={`Delete ${k.key}`}
                      onClick={async () => {
                        const ok = await confirm({ title: `Delete ${k.key}?`, description: `Projects that reference it lose it on their next run.`, confirmLabel: "Delete key" });
                        if (ok) removeKey.mutate(k.key);
                      }}
                    />
                  </Table.Td>
                </Table.Row>
              ))}
            </tbody>
          </Table.Root>
        )}
      </Card>
      {adding && <AddKeyDialog store={store} taken={names} onOpenChange={setAdding} />}
    </>
  );
}

function AddKeyDialog({ store, taken, onOpenChange }: { store: SecretStore; taken: string[]; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const [key, setKey] = useState("");
  const [value, setValue] = useState("");
  const k = key.trim();
  const error = !k ? null : !SECRET_KEY_RE.test(k) ? "Letters, digits, dot, dash and underscore." : null;
  const replacing = taken.includes(k);
  const save = useMutation({
    mutationFn: () => api.stores.setKey(store.id, k, value),
    onSuccess: () => {
      setValue("");
      qc.invalidateQueries({ queryKey: qk.storeKeys(store.id) });
      qc.invalidateQueries({ queryKey: qk.stores });
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok(`${k} ${replacing ? "replaced" : "set"} in ${store.name}`);
      onOpenChange(false);
    },
    onError: (e) => toast.err(`Could not set ${k}`, e),
  });
  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title={<>Add key to <span className="font-mono">{store.name}</span></>}
      onSubmit={() => k && value && !error && save.mutate()}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={save.isPending} disabled={!k || !value || !!error}>{replacing ? "Replace" : "Save"}</Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="Key" htmlFor="key-name" error={error} warning={!error && replacing ? "Exists: saving replaces its value." : undefined}>
          <Input id="key-name" mono autoFocus value={key} invalid={!!error} onChange={(e) => setKey(e.target.value)} placeholder="DB_PASSWORD" spellCheck={false} />
        </Field>
        <Field label="Value" htmlFor="key-value">
          <Input id="key-value" type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} />
        </Field>
      </div>
    </Dialog>
  );
}
