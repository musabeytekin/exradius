import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { Checkbox, PickList, Table } from "@/components/ui/controls";
import { Icon } from "@/components/ui/icons";
import { Dialog } from "@/components/ui/overlays";
import { Banner, Button, Card, CardHeader, Chip, EmptyState, Field, Input, Mono, PageHeader, Skeleton, SkeletonRows, Textarea } from "@/components/ui/primitives";
import { api, ApiError, refusal } from "@/lib/api";
import { buttonClass } from "@/lib/buttonStyles";
import { useConfirm } from "@/lib/confirm";
import { parseDotenv } from "@/lib/dotenv";
import { absoluteTime, relativeTime } from "@/lib/format";
import { requiredKeys, SECRET_KEY_RE, STORE_NAME_RE, storeNameOf } from "@/lib/projectConfig";
import { qk, useProjects, useStoreChanges, useStoreKeys, useStoreKinds, useStores } from "@/lib/queries";
import { toast } from "@/lib/toast";
import type { Project, SecretStore, SecretStoreKey, StoreChange, StoreKind } from "@/lib/types";
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
              <tr><Table.Th>Store</Table.Th><Table.Th>Kept in</Table.Th><Table.Th>Keys</Table.Th><Table.Th>Allowed projects</Table.Th><Table.Th>Named by</Table.Th><Table.Th className="text-right">Created</Table.Th></tr>
            </Table.Head>
            <tbody>
              {list.map((s) => <StoreRow key={s.id} store={s} usedBy={usedBy(s)} />)}
            </tbody>
          </Table.Root>
        )}
      </Card>
      {open && <CreateStoreDialog taken={list.map((s) => s.name)} projects={projects.data ?? []} onOpenChange={setDialog} />}
    </>
  );
}

function StoreRow({ store, usedBy }: { store: SecretStore; usedBy: string[] }) {
  const navigate = useNavigate();
  const kinds = useStoreKinds();
  const known = store.error ? null : store.key_count ?? store.keys?.length;
  const keys = useStoreKeys(known === undefined || (known === null && !store.error) ? store.id : null);
  const n = keyCount(store, keys.data);
  return (
    <Table.Row onClick={() => navigate({ to: "/stores/$storeId", params: { storeId: store.id } })}>
      <Table.Td><Mono className="font-medium">{store.name}</Mono></Table.Td>
      <Table.Td className="text-xs text-muted">
        {kindLabel(kinds.data, store.kind)}
        {store.where && <Mono className="ml-1.5 text-xs text-subtle">{store.where}</Mono>}
      </Table.Td>
      <Table.Td className="tabular">
        {store.error ? <span title={store.error}><Chip tone="warn" dot>unreachable</Chip></span> : n ?? "—"}
      </Table.Td>
      <Table.Td><SlugList slugs={store.allowed_projects ?? []} /></Table.Td>
      <Table.Td><SlugList slugs={usedBy} flag={(slug) => !(store.allowed_projects ?? []).includes(slug)} /></Table.Td>
      <Table.Td className="text-right text-xs text-subtle">{store.created_at ? relativeTime(store.created_at) : ""}</Table.Td>
    </Table.Row>
  );
}

/** Project slugs; `flag` marks the ones a store does not allow. */
function SlugList({ slugs, flag }: { slugs: string[]; flag?: (slug: string) => boolean }) {
  if (!slugs.length) return <span className="text-xs text-subtle">—</span>;
  return (
    <span className="flex flex-wrap gap-x-1.5 font-mono text-xs text-muted">
      {slugs.map((slug, i) => (
        <span key={slug} className={flag?.(slug) ? "text-err" : undefined} title={flag?.(slug) ? "Names this store but is not allowed to use it" : undefined}>
          {slug}{i < slugs.length - 1 ? "," : ""}
        </span>
      ))}
    </span>
  );
}

/** Where a store's values live: Exradius itself, or an external backend this install can reach. */
function kindLabel(kinds: StoreKind[] | undefined, kind: string | undefined): string {
  return kinds?.find((k) => k.kind === kind)?.label ?? kind ?? "Exradius";
}

/** The kind picker and its settings fields (where an external store's values are). */
function KindFields({ kinds, kind, onKind, settings, onSettings }: {
  kinds: StoreKind[]; kind: string; onKind: (k: string) => void;
  settings: Record<string, string>; onSettings: (s: Record<string, string>) => void;
}) {
  const current = kinds.find((k) => k.kind === kind);
  return (
    <>
      <Field label="Values kept in" htmlFor="store-kind">
        <PickList
          id="store-kind"
          value={kind}
          onChange={(v) => v && onKind(v)}
          options={kinds.map((k) => ({
            value: k.kind, label: k.label, disabled: !k.available,
            description: k.available ? (k.external ? "Read from there; set and delete values there." : "Set here, encrypted in Exradius's database.") : k.reason,
          }))}
        />
      </Field>
      {current?.fields.map((f) => (
        <Field key={f.name} label={f.label} htmlFor={`store-${f.name}`} hint={f.description || undefined} optional={!f.required}>
          <Input
            id={`store-${f.name}`}
            mono
            spellCheck={false}
            value={settings[f.name] ?? ""}
            placeholder={f.default != null ? String(f.default) : undefined}
            onChange={(e) => onSettings({ ...settings, [f.name]: e.target.value })}
          />
        </Field>
      ))}
    </>
  );
}

/** Settings as the API takes them: blank fields left out (their default applies). */
function filledSettings(settings: Record<string, string>): Record<string, string> {
  return Object.fromEntries(Object.entries(settings).map(([k, v]) => [k, v.trim()]).filter(([, v]) => v));
}

function CreateStoreDialog({ taken, projects, onOpenChange }: { taken: string[]; projects: Project[]; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const kinds = useStoreKinds();
  const [name, setName] = useState("");
  const [allowed, setAllowed] = useState<string[]>([]);
  const [kind, setKind] = useState("internal");
  const [settings, setSettings] = useState<Record<string, string>>({});
  const kindSettings = filledSettings(settings);
  const missingField = kinds.data?.find((k) => k.kind === kind)?.fields.some((f) => f.required && !kindSettings[f.name]);
  const [serverError, setServerError] = useState<string | null>(null);
  const n = name.trim();
  const error = !n ? null : !STORE_NAME_RE.test(n) ? "Lowercase letters, digits and dashes, starting with a letter." : taken.includes(n) ? "A store with this name exists." : serverError;
  const create = useMutation({
    mutationFn: () => api.stores.create({ name: n, kind, settings: kindSettings, allowed_projects: allowed }),
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
      onSubmit={() => n && !error && !missingField && create.mutate()}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={create.isPending} disabled={!n || !!error || missingField}>Create store</Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="Name" htmlFor="store-name" error={error}>
          <Input id="store-name" mono autoFocus value={name} invalid={!!error} onChange={(e) => { setName(e.target.value.toLowerCase()); setServerError(null); }} placeholder="bank-platform" spellCheck={false} />
        </Field>
        {(kinds.data?.length ?? 0) > 1 && (
          <KindFields kinds={kinds.data!} kind={kind} onKind={(k) => { setKind(k); setSettings({}); }} settings={settings} onSettings={setSettings} />
        )}
        <Field label="Allowed projects" hint="Only these projects may use the store's values. None by default.">
          <ProjectChecklist projects={projects} store={n} value={allowed} onChange={setAllowed} />
        </Field>
      </div>
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
  const kinds = useStoreKinds();
  const store = stores.data?.find((s) => s.id === storeId);
  // Not asked again while the store's backend is known to be out of reach.
  const keys = useStoreKeys(store?.error ? null : storeId);
  const [refused, setRefused] = useState<string | null>(null);
  const [adding, setAdding] = useState(false);
  const [pasting, setPasting] = useState(false);
  const [editing, setEditing] = useState(false);

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
  const external = !!store.external;
  const label = kindLabel(kinds.data, store.kind);
  // The store's error, or the key list's own (an external backend that stopped answering).
  const unreachable = store.error ?? (external && keys.isError ? (keys.error as Error).message : null);
  const retry = () => {
    qc.invalidateQueries({ queryKey: qk.stores });
    qc.invalidateQueries({ queryKey: qk.projects });
  };

  return (
    <>
      <PageHeader
        breadcrumb={<><Link to="/stores" search={{}} className="hover:text-fg">Stores</Link><Icon name="chevronRight" size={12} /><span className="text-muted">{store.name}</span></>}
        title={<span className="font-mono">{store.name}</span>}
        meta={
          <>
          <span className="flex items-center gap-1.5 text-xs text-subtle">
            Kept in <span className="text-muted">{label}</span>
            {store.where && <Mono className="text-xs text-muted">{store.where}</Mono>}
          </span>
          {usedBy.length > 0 && (
            <span className="flex items-center gap-1.5 text-xs text-subtle">
              Named by
              {usedBy.map((p, i) => (
                <span key={p.id}>
                  <Link to="/projects/$projectId" params={{ projectId: p.id }} search={{ tab: "secrets" }} className="font-mono text-muted hover:text-fg hover:underline">{p.slug}</Link>
                  {i < usedBy.length - 1 ? "," : ""}
                </span>
              ))}
            </span>
          )}
          </>
        }
        actions={
          <>
            {!external && <Button variant="primary" icon="plus" onClick={() => setAdding(true)}>Add key</Button>}
            {!external && <Button variant="secondary" icon="upload" onClick={() => setPasting(true)}>Paste .env</Button>}
            {(kinds.data?.length ?? 0) > 1 && <Button variant="secondary" icon="settings" onClick={() => setEditing(true)}>Settings</Button>}
            <Button
              variant="secondary"
              icon="trash"
              loading={remove.isPending}
              onClick={async () => {
                setRefused(null);
                const ok = await confirm({
                  title: `Delete store ${store.name}?`,
                  description: external ? `Exradius forgets it; the values in ${label} are not touched.` : "Every value in it is deleted.",
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
      {unreachable && (
        <Banner tone="err" title={`${label} cannot be reached`} className="mb-4" action={<Button size="sm" variant="secondary" icon="refresh" onClick={retry}>Try again</Button>}>
          <span className="font-mono text-xs">{unreachable}</span>
          <div className="mt-1 text-xs">Tasks of the projects using this store are refused with this reason until it answers again. Its keys are not missing: they are unknown.</div>
        </Banner>
      )}
      <AllowedProjects store={store} projects={projects.data ?? []} storeKeys={unreachable || keys.isLoading ? undefined : names} />
      <Card className="mt-4">
        <CardHeader
          title="Keys"
          description={external ? <>Read from {label}{store.where ? <> at <Mono className="text-xs">{store.where}</Mono></> : null}. Set and delete values there; Exradius never stores them.</> : "Values are write-only."}
        />
        {unreachable ? (
          <p className="px-4 py-6 text-center text-sm text-muted">Key names are unknown while {label} cannot be reached.</p>
        ) : keys.isLoading ? (
          <SkeletonRows rows={3} />
        ) : keys.isError ? (
          <p className="px-4 py-6 text-center text-sm text-err">Could not load keys: {(keys.error as Error).message}</p>
        ) : list.length === 0 ? (
          external
            ? <EmptyState compact icon="key" title="No keys">{label} holds no field at this path.</EmptyState>
            : <EmptyState compact icon="key" title="No keys" action={<Button size="sm" variant="secondary" icon="plus" onClick={() => setAdding(true)}>Add key</Button>} />
        ) : external ? (
          <Table.Root>
            <tbody>
              {list.map((k) => (
                <Table.Row key={k.key}><Table.Td><Mono className="font-medium">{k.key}</Mono></Table.Td></Table.Row>
              ))}
            </tbody>
          </Table.Root>
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
      <ChangeLog storeId={store.id} />
      {editing && kinds.data && <StoreSettingsDialog store={store} kinds={kinds.data} keyCount={external ? 0 : names.length} onOpenChange={setEditing} />}
      {adding && <AddKeyDialog store={store} taken={names} onOpenChange={setAdding} />}
      {pasting && <PasteEnvDialog store={store} onOpenChange={setPasting} />}
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

function PasteEnvDialog({ store, onOpenChange }: { store: SecretStore; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const [text, setText] = useState("");
  const parsed = parseDotenv(text);
  const keys = Object.keys(parsed.values ?? {});
  const save = useMutation({
    mutationFn: () => api.stores.setKeys(store.id, parsed.values!),
    onSuccess: () => {
      setText("");
      qc.invalidateQueries({ queryKey: qk.storeKeys(store.id) });
      qc.invalidateQueries({ queryKey: qk.stores });
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok(`${keys.length} ${keys.length === 1 ? "key" : "keys"} set in ${store.name}`);
      onOpenChange(false);
    },
    onError: (e) => toast.err("Could not set the keys", e),
  });
  return (
    <Dialog
      open
      width={600}
      onOpenChange={onOpenChange}
      title={<>Paste .env into <span className="font-mono">{store.name}</span></>}
      description="Keys that already exist in this store are replaced."
      onSubmit={() => keys.length && save.mutate()}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={save.isPending} disabled={!keys.length}>
            {keys.length ? `Save ${keys.length} ${keys.length === 1 ? "key" : "keys"}` : "Save"}
          </Button>
        </>
      }
    >
      <Field
        label="KEY=value lines"
        htmlFor="dotenv"
        error={parsed.error}
        hint={keys.length ? <>Sets <Mono className="text-xs">{keys.join(", ")}</Mono></> : undefined}
      >
        <Textarea
          id="dotenv"
          mono
          autoFocus
          rows={10}
          spellCheck={false}
          autoComplete="off"
          invalid={!!parsed.error}
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder={"DB_PASSWORD=...\nAPI_TOKEN=..."}
        />
      </Field>
    </Dialog>
  );
}

/** Checkboxes for known projects; those whose config names `store` say so. */
function ProjectChecklist({ projects, store, value, onChange }: {
  projects: Project[]; store: string; value: string[]; onChange: (v: string[]) => void;
}) {
  if (!projects.length) return <p className="text-xs text-subtle">No projects yet.</p>;
  return (
    <div className="flex max-h-48 flex-col gap-1.5 overflow-y-auto">
      {projects.map((p) => (
        <label key={p.id} className="flex items-center gap-2 text-sm">
          <Checkbox
            checked={value.includes(p.slug)}
            onChange={(on) => onChange(on ? [...value, p.slug] : value.filter((x) => x !== p.slug))}
            aria-label={`Allow ${p.slug}`}
          />
          <Mono className="text-[12.5px]">{p.slug}</Mono>
          {store && storeNameOf(p) === store && <span className="text-xs text-subtle">names this store</span>}
        </label>
      ))}
    </div>
  );
}

/** The projects allowed to use the store (config-repo.md §7), and those that name it without being allowed. */
function AllowedProjects({ store, projects, storeKeys }: { store: SecretStore; projects: Project[]; storeKeys: string[] | undefined }) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const allowed = store.allowed_projects ?? [];
  const [adding, setAdding] = useState<string | null>(null);
  const update = useMutation({
    mutationFn: (next: string[]) => api.stores.update(store.id, { allowed_projects: next }),
    onSuccess: () => {
      setAdding(null);
      qc.invalidateQueries({ queryKey: qk.stores });
      qc.invalidateQueries({ queryKey: qk.projects });
    },
    onError: (e) => toast.err("Could not change the allowed projects", e),
  });
  const bySlug = new Map(projects.map((p) => [p.slug, p]));
  const naming = projects.filter((p) => storeNameOf(p) === store.name && !allowed.includes(p.slug));
  const offered = projects.filter((p) => !allowed.includes(p.slug));
  const allow = async (slug: string) => {
    const ok = await confirm({
      title: `Allow ${slug} to use ${store.name}?`,
      description: "Its config can then put any key of this store into its runs' environment.",
      confirmLabel: "Allow",
    });
    if (ok) update.mutate([...allowed, slug]);
  };
  return (
    <Card>
      <CardHeader
        title="Allowed projects"
        description="Only these projects get this store's values. A config naming the store from another project is not runnable."
      />
      {naming.length > 0 && (
        <div className="flex flex-col gap-2 border-b border-border px-4 py-3">
          {naming.map((p) => (
            <div key={p.id} className="flex items-center gap-3 text-sm">
              <Chip tone="err" dot>not allowed</Chip>
              <span className="min-w-0 flex-1 text-muted"><Mono>{p.slug}</Mono> names this store; its tasks are refused.</span>
              <Button size="sm" variant="secondary" loading={update.isPending} onClick={() => allow(p.slug)}>Allow {p.slug}</Button>
            </div>
          ))}
        </div>
      )}
      {allowed.length === 0 ? (
        <p className="px-4 py-3 text-sm text-subtle">No project may use this store.</p>
      ) : (
        <Table.Root>
          <tbody>
            {allowed.map((slug) => {
              const p = bySlug.get(slug);
              return (
                <Table.Row key={slug}>
                  <Table.Td>
                    {p ? (
                      <Link to="/projects/$projectId" params={{ projectId: p.id }} search={{ tab: "secrets" }} className="font-mono text-[12.5px] hover:underline">{slug}</Link>
                    ) : <Mono className="text-[12.5px]">{slug}</Mono>}
                  </Table.Td>
                  <Table.Td className="text-xs text-subtle">{p && storeNameOf(p) === store.name ? "names this store" : "does not name this store"}</Table.Td>
                  <Table.Td className="text-xs">{p && storeNameOf(p) === store.name ? <ProjectKeys project={p} storeKeys={storeKeys} /> : null}</Table.Td>
                  <Table.Td className="w-12 text-right">
                    <Button
                      size="sm"
                      variant="ghost"
                      square
                      icon="x"
                      aria-label={`Remove ${slug}`}
                      onClick={async () => {
                        const ok = await confirm({
                          title: `Remove ${slug} from ${store.name}?`,
                          description: "Its tasks are refused from then on while its config names this store. Runs already started keep what they resolved.",
                          confirmLabel: "Remove",
                        });
                        if (ok) update.mutate(allowed.filter((x) => x !== slug));
                      }}
                    />
                  </Table.Td>
                </Table.Row>
              );
            })}
          </tbody>
        </Table.Root>
      )}
      {offered.length > 0 && (
        <div className="flex items-center gap-2 border-t border-border px-4 py-3">
          <PickList
            size="sm"
            mono
            className="max-w-xs"
            aria-label="Project to allow"
            placeholder="Allow a project…"
            value={adding}
            onChange={setAdding}
            options={offered.map((p) => ({ value: p.slug, label: p.slug, description: storeNameOf(p) === store.name ? "names this store" : undefined }))}
          />
          <Button size="sm" variant="secondary" disabled={!adding} loading={update.isPending} onClick={() => adding && allow(adding)}>Allow</Button>
        </div>
      )}
    </Card>
  );
}

/** What a project naming the store still lacks in it: unknown while the store cannot be read. */
function ProjectKeys({ project, storeKeys }: { project: Project; storeKeys: string[] | undefined }) {
  if (storeKeys === undefined) return <span className="text-subtle">keys unknown</span>;
  const required = requiredKeys(project, storeKeys);
  if (!required.length) return <span className="text-subtle">references no key</span>;
  const missing = required.filter((k) => !k.present).map((k) => k.key);
  if (!missing.length) return <span className="text-ok">all {required.length} keys present</span>;
  return <span className="text-warn">missing <Mono className="text-xs">{missing.join(", ")}</Mono></span>;
}

/** Change where the store's values are kept: its kind and settings. Projects keep naming it. */
function StoreSettingsDialog({ store, kinds, keyCount, onOpenChange }: {
  store: SecretStore; kinds: StoreKind[]; keyCount: number; onOpenChange: (o: boolean) => void;
}) {
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [kind, setKind] = useState(store.kind ?? "internal");
  const [settings, setSettings] = useState<Record<string, string>>(
    Object.fromEntries(Object.entries(store.settings ?? {}).map(([k, v]) => [k, String(v ?? "")])),
  );
  const [serverError, setServerError] = useState<string | null>(null);
  const filled = filledSettings(settings);
  const missingField = kinds.find((k) => k.kind === kind)?.fields.some((f) => f.required && !filled[f.name]);
  const leaving = store.kind === "internal" && kind !== "internal" && keyCount > 0;
  const save = useMutation({
    mutationFn: () => api.stores.update(store.id, { kind, settings: filled }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.stores });
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok(`Store ${store.name} saved`);
      onOpenChange(false);
    },
    onError: (e) => { const m = refusal(e); if (m) setServerError(m); else toast.err("Could not save the store", e); },
  });
  const submit = async () => {
    if (missingField) return;
    if (leaving) {
      const ok = await confirm({
        title: `Move ${store.name} to ${kindLabel(kinds, kind)}?`,
        description: `Its ${keyCount} ${keyCount === 1 ? "value" : "values"} kept in Exradius are deleted; ${kindLabel(kinds, kind)} becomes the only source. Projects keep naming the store.`,
        typeToConfirm: store.name,
        confirmLabel: "Move store",
      });
      if (!ok) return;
    }
    save.mutate();
  };
  return (
    <Dialog
      open
      width={520}
      onOpenChange={onOpenChange}
      title={<>Settings of <span className="font-mono">{store.name}</span></>}
      description="Where the store's values are kept. Projects name the store, never where it is: changing this needs no commit in their config."
      onSubmit={submit}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={save.isPending} disabled={missingField}>Save</Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <KindFields kinds={kinds} kind={kind} onKind={(k) => { setKind(k); setSettings({}); setServerError(null); }} settings={settings} onSettings={(v) => { setSettings(v); setServerError(null); }} />
        {leaving && <Banner tone="warn" title={`Its ${keyCount} ${keyCount === 1 ? "value" : "values"} in Exradius will be deleted`} />}
        {serverError && <Banner tone="err" title="Not saved"><span className="font-mono text-xs">{serverError}</span></Banner>}
      </div>
    </Dialog>
  );
}

function describeChange(c: StoreChange) {
  const key = c.key ? <Mono className="text-[12.5px]">{c.key}</Mono> : null;
  const project = c.project ? <Mono className="text-[12.5px]">{c.project}</Mono> : null;
  switch (c.action) {
    case "store_created": return <>created the store{c.detail?.kind && c.detail.kind !== "internal" ? <> ({String(c.detail.kind)})</> : null}</>;
    case "store_deleted": return <>deleted the store</>;
    case "key_set": return <>set {key}</>;
    case "key_deleted": return <>deleted {key}</>;
    case "project_allowed": return <>allowed {project}</>;
    case "project_removed": return <>removed {project}{c.detail?.reason ? <> ({String(c.detail.reason)})</> : null}</>;
    case "settings_changed": {
      const after = (c.detail?.after ?? {}) as { kind?: string; settings?: Record<string, unknown> };
      const before = (c.detail?.before ?? {}) as { kind?: string };
      const where = Object.entries(after.settings ?? {}).map(([k, v]) => `${k}=${String(v)}`).join(" ");
      return <>{before.kind !== after.kind ? <>moved the store from {before.kind} to {after.kind}</> : <>changed the settings</>}{where && <> (<Mono className="text-xs">{where}</Mono>)</>}</>;
    }
    default: return <>{c.action}</>;
  }
}

/** The store's append-only change log: who did what, never a value. */
function ChangeLog({ storeId }: { storeId: string }) {
  const changes = useStoreChanges(storeId);
  const list = changes.data ?? [];
  return (
    <Card className="mt-4">
      <CardHeader title="Changes" description="Every change to this store, newest first. Values are never recorded." />
      {changes.isLoading ? (
        <SkeletonRows rows={3} />
      ) : changes.isError ? (
        <p className="px-4 py-6 text-center text-sm text-err">Could not load the changes: {(changes.error as Error).message}</p>
      ) : list.length === 0 ? (
        <p className="px-4 py-3 text-sm text-subtle">No changes recorded.</p>
      ) : (
        <Table.Root>
          <Table.Head>
            <tr><Table.Th>When</Table.Th><Table.Th>Who</Table.Th><Table.Th>What</Table.Th></tr>
          </Table.Head>
          <tbody>
            {list.map((c) => (
              <Table.Row key={c.id}>
                <Table.Td className="whitespace-nowrap text-xs text-subtle"><span title={absoluteTime(c.at)}>{relativeTime(c.at)}</span></Table.Td>
                <Table.Td className="text-xs text-muted">{c.actor}</Table.Td>
                <Table.Td className="text-sm">{describeChange(c)}</Table.Td>
              </Table.Row>
            ))}
          </tbody>
        </Table.Root>
      )}
    </Card>
  );
}
