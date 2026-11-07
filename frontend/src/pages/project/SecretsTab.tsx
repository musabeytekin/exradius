import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { Table } from "@/components/ui/controls";
import { Banner, Button, Card, CardHeader, Chip, Field, Input, Mono, Skeleton } from "@/components/ui/primitives";
import { api, ApiError } from "@/lib/api";
import { buttonClass } from "@/lib/buttonStyles";
import { keyName, requiredKeys, storeNameOf } from "@/lib/projectConfig";
import { qk, useStoreKeys, useStores } from "@/lib/queries";
import { toast } from "@/lib/toast";
import type { Project, SecretStore } from "@/lib/types";

const notAvailable = (e: unknown) => e instanceof ApiError && (e.status === 404 || e.status === 405);

export function SecretsTab({ project }: { project: Project }) {
  const storeName = storeNameOf(project);
  const stores = useStores();
  const store = storeName ? stores.data?.find((s) => s.name === storeName) : undefined;
  const keys = useStoreKeys(store?.id);

  if (!storeName) {
    const req = requiredKeys(project);
    return (
      <Card className="px-4 py-6 text-sm text-muted">
        The file names no secret store.
        {req.length > 0 && <> It references <Mono>{req.map((k) => k.key).join(", ")}</Mono>.</>}
      </Card>
    );
  }
  if (stores.isLoading) return <Skeleton className="h-48" />;
  if (stores.isError) {
    return (
      <Banner tone={notAvailable(stores.error) ? "neutral" : "err"} title="Secret stores could not be loaded">
        {notAvailable(stores.error) ? "This API has no secret stores." : (stores.error as Error).message}
      </Banner>
    );
  }
  if (!store) return <MissingStore project={project} name={storeName} />;

  const storeKeys = keys.data?.map(keyName);
  const req = requiredKeys(project, storeKeys);
  const missing = req.filter((k) => !k.present).length;
  return (
    <Card>
      <CardHeader
        title={<span className="flex items-center gap-2">Store <Mono>{store.name}</Mono></span>}
        description={req.length ? (missing ? `${missing} of ${req.length} required keys missing` : `All ${req.length} required keys present`) : "The file references no secrets."}
        actions={<Link to="/stores/$storeId" params={{ storeId: store.id }} className={buttonClass("ghost", "sm")}>Open store</Link>}
      />
      {keys.isLoading ? (
        <div className="p-4"><Skeleton className="h-24" /></div>
      ) : req.length === 0 ? null : (
        <>
          {missing > 0 && <MissingKeys store={store} keys={req.filter((k) => !k.present).map((k) => k.key)} projectId={project.id} />}
          {missing < req.length && (
            <Table.Root>
              <Table.Head>
                <tr><Table.Th>Key</Table.Th><Table.Th>State</Table.Th><Table.Th className="w-[420px]">Value</Table.Th></tr>
              </Table.Head>
              <tbody>
                {req.filter((k) => k.present).map((k) => (
                  <Table.Row key={k.key}>
                    <Table.Td><Mono className="font-medium">{k.key}</Mono></Table.Td>
                    <Table.Td><Chip tone="ok" dot>present</Chip></Table.Td>
                    <Table.Td><KeyValueSetter store={store} keyName={k.key} present projectId={project.id} /></Table.Td>
                  </Table.Row>
                ))}
              </tbody>
            </Table.Root>
          )}
        </>
      )}
    </Card>
  );
}

function MissingStore({ project, name }: { project: Project; name: string }) {
  const qc = useQueryClient();
  const req = requiredKeys(project, null);
  const create = useMutation({
    mutationFn: () => api.stores.create(name),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.stores });
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok(`Store ${name} created`);
    },
    onError: (e) => toast.err("Could not create the store", e),
  });
  return (
    <Card>
      <div className="flex items-center gap-4 px-4 py-4">
        <div className="min-w-0 flex-1 text-sm">
          <div className="font-medium text-fg">Store <Mono>{name}</Mono> does not exist</div>
          {req.length > 0 && <div className="mt-0.5 text-xs text-muted">Required: <Mono className="text-xs text-muted">{req.map((k) => k.key).join(", ")}</Mono></div>}
        </div>
        <Button variant="primary" icon="plus" loading={create.isPending} onClick={() => create.mutate()}>Create store {name}</Button>
      </div>
    </Card>
  );
}

function MissingKeys({ store, keys, projectId }: { store: Pick<SecretStore, "id" | "name">; keys: string[]; projectId: string }) {
  const qc = useQueryClient();
  const save = useMutation({
    mutationFn: (values: Record<string, string>) => api.stores.setKeys(store.id, values),
    onSuccess: (_, values) => {
      qc.invalidateQueries({ queryKey: qk.storeKeys(store.id) });
      qc.invalidateQueries({ queryKey: qk.stores });
      qc.invalidateQueries({ queryKey: qk.projects });
      qc.invalidateQueries({ queryKey: qk.project(projectId) });
      const n = Object.keys(values).length;
      toast.ok(`${n} ${n === 1 ? "key" : "keys"} set in ${store.name}`);
    },
    onError: (e) => toast.err("Could not set the keys", e),
  });
  // Values go in as variables but the mutation is reset once it settles, so they are not kept in its cache.
  return <MissingKeysForm keys={keys} saving={save.isPending} onSave={(values) => save.mutateAsync(values).then(() => true, () => false).finally(() => save.reset())} />;
}

/** One write-only input per missing key, saved together. Unfilled inputs are left out. */
export function MissingKeysForm({ keys, saving, onSave }: {
  keys: string[]; saving?: boolean; onSave: (values: Record<string, string>) => Promise<boolean> | void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const filled = Object.fromEntries(keys.filter((k) => values[k]).map((k) => [k, values[k]]));
  const count = Object.keys(filled).length;
  return (
    <form
      className="flex flex-col gap-3 border-b border-border px-4 py-4"
      onSubmit={async (e) => {
        e.preventDefault();
        if (count && (await onSave(filled)) !== false) setValues({});
      }}
    >
      <div className="grid gap-3 sm:grid-cols-2">
        {keys.map((k) => (
          <Field key={k} label={<Mono className="text-xs">{k}</Mono>} htmlFor={`missing-${k}`}>
            <Input
              id={`missing-${k}`}
              type="password"
              autoComplete="off"
              value={values[k] ?? ""}
              onChange={(e) => setValues((v) => ({ ...v, [k]: e.target.value }))}
            />
          </Field>
        ))}
      </div>
      <div className="flex justify-end">
        <Button type="submit" size="sm" variant="primary" loading={saving} disabled={!count}>Save all</Button>
      </div>
    </form>
  );
}

/** A write-only value control: the value is sent once and never shown again. */
export function KeyValueSetter({ store, keyName: key, present, projectId, onDone }: {
  store: Pick<SecretStore, "id" | "name">; keyName: string; present: boolean; projectId?: string; onDone?: () => void;
}) {
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState("");
  const save = useMutation({
    // Read from state, not passed as a variable, so the value is not kept in the mutation cache.
    mutationFn: () => api.stores.setKey(store.id, key, value),
    onSuccess: () => {
      setValue("");
      setEditing(false);
      qc.invalidateQueries({ queryKey: qk.storeKeys(store.id) });
      qc.invalidateQueries({ queryKey: qk.stores });
      qc.invalidateQueries({ queryKey: qk.projects });
      if (projectId) qc.invalidateQueries({ queryKey: qk.project(projectId) });
      toast.ok(`${key} ${present ? "replaced" : "set"} in ${store.name}`);
      onDone?.();
    },
    onError: (e) => toast.err(`Could not set ${key}`, e),
  });
  if (!editing) {
    return (
      <Button size="sm" variant={present ? "ghost" : "secondary"} icon={present ? "edit" : "plus"} onClick={() => setEditing(true)}>
        {present ? "Replace" : "Set value"}
      </Button>
    );
  }
  return (
    <form
      className="flex items-center gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        if (value) save.mutate();
      }}
    >
      <Input
        type="password"
        autoComplete="off"
        autoFocus
        aria-label={`Value for ${key}`}
        value={value}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => e.key === "Escape" && (setEditing(false), setValue(""))}
        className="flex-1"
      />
      <Button size="sm" type="submit" variant="primary" loading={save.isPending} disabled={!value}>Save</Button>
      <Button size="sm" variant="ghost" onClick={() => { setEditing(false); setValue(""); }}>Cancel</Button>
    </form>
  );
}
