import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { secretLabel } from "@/lib/catalog";
import { useConfirm } from "@/lib/confirm";
import { qk, useConnections, useGlobalSecrets, useProjectSecrets } from "@/lib/queries";
import { findRefs, buildSecretIndex } from "@/lib/secretRefs";
import { toast } from "@/lib/toast";
import type { Secret } from "@/lib/types";
import { CreateSecretDialog } from "./secrets";
import { Table } from "./ui/controls";
import { Icon } from "./ui/icons";
import { Dialog } from "./ui/overlays";
import { Button, Card, CardHeader, Chip, EmptyState, Field, Mono, SkeletonRows, Textarea } from "./ui/primitives";

/** Write-only secrets of one scope: keys, where they are referenced, replace and delete. */
export function SecretsTable({
  projectId, projectSlug, title, description, createKey, openCreate = false, emptyHint,
}: {
  projectId: string | null; projectSlug?: string; title: string; description: ReactNode; createKey?: string; openCreate?: boolean; emptyHint: ReactNode;
}) {
  const projectSecrets = useProjectSecrets(projectId);
  const globalSecrets = useGlobalSecrets();
  const secrets = projectId ? projectSecrets : globalSecrets;
  const connections = useConnections();
  const qc = useQueryClient();
  const confirm = useConfirm();
  const [creating, setCreating] = useState(openCreate);
  const [replacing, setReplacing] = useState<Secret | null>(null);

  // Which connections reference each name (by ${secret:NAME}).
  const refIndex = buildSecretIndex([]);
  const usedBy = new Map<string, string[]>();
  for (const c of connections.data ?? []) {
    for (const v of [...Object.values(c.env), ...Object.values(c.headers)]) {
      for (const r of findRefs(v, refIndex)) {
        if (r.name) usedBy.set(r.name, [...new Set([...(usedBy.get(r.name) ?? []), c.slug])]);
      }
    }
  }

  const remove = useMutation({
    mutationFn: (s: Secret) => api.secrets.remove(projectId, s.id),
    onSuccess: (_, s) => {
      qc.invalidateQueries({ queryKey: projectId ? qk.projectSecrets(projectId) : qk.globalSecrets });
      qc.invalidateQueries({ queryKey: qk.status });
      toast.ok(`${secretLabel(s.key) ?? `Secret ${s.key}`} deleted`);
    },
    onError: (e) => toast.err("Could not delete the secret", e),
  });

  return (
    <Card>
      <CardHeader
        title={title}
        description={description}
        actions={<Button size="sm" variant="primary" icon="plus" onClick={() => setCreating(true)}>Add secret</Button>}
      />
      {secrets.isLoading ? (
        <SkeletonRows rows={2} />
      ) : !secrets.data?.length ? (
        <EmptyState icon="key" title="No secrets" action={<Button variant="secondary" icon="plus" onClick={() => setCreating(true)}>Add a secret</Button>}>
          {emptyHint}
        </EmptyState>
      ) : (
        <Table.Root>
          <Table.Head>
            <tr>
              <Table.Th>Name</Table.Th>
              <Table.Th>Value</Table.Th>
              <Table.Th>Referenced by</Table.Th>
              <Table.Th />
            </tr>
          </Table.Head>
          <tbody>
            {secrets.data.map((s) => (
              <Table.Row key={s.id}>
                <Table.Td>
                  {secretLabel(s.key) ? (
                    <div className="flex flex-col">
                      <span className="font-medium">{secretLabel(s.key)}</span>
                      <Mono className="text-[11px] text-subtle">{s.key}</Mono>
                    </div>
                  ) : (
                    <Mono className="font-medium">{s.key}</Mono>
                  )}
                </Table.Td>
                <Table.Td>
                  <span className="inline-flex items-center gap-1.5 text-xs text-subtle">
                    <Icon name="lock" size={12} />
                    <span className="tracking-[0.2em]">••••••••</span>
                    <span>write-only</span>
                  </span>
                </Table.Td>
                <Table.Td>
                  <div className="flex flex-wrap gap-1">
                    {(usedBy.get(s.key) ?? []).map((c) => <Chip key={c} mono>{c}</Chip>)}
                    {!usedBy.get(s.key) && (
                      <span className="text-xs text-subtle">{projectId ? "exported to runs" : "—"}</span>
                    )}
                  </div>
                </Table.Td>
                <Table.Td className="w-[1%] whitespace-nowrap text-right">
                  <Button size="sm" variant="ghost" icon="refresh" onClick={() => setReplacing(s)}>Replace value</Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    square
                    icon="trash"
                    aria-label={`Delete ${secretLabel(s.key) ?? s.key}`}
                    onClick={async () => {
                      const users = usedBy.get(s.key);
                      const ok = await confirm({
                        title: `Delete ${secretLabel(s.key) ?? s.key}?`,
                        description: users?.length
                          ? `Connections ${users.join(", ")} reference it and will fail to start where nothing else resolves the name. The value cannot be recovered.`
                          : "The value cannot be recovered.",
                        confirmLabel: "Delete secret",
                      });
                      if (ok) remove.mutate(s);
                    }}
                  />
                </Table.Td>
              </Table.Row>
            ))}
          </tbody>
        </Table.Root>
      )}
      <CreateSecretDialog open={creating} onOpenChange={setCreating} projectId={projectId} projectSlug={projectSlug} defaultKey={createKey} />
      {replacing && <ReplaceDialog secret={replacing} projectId={projectId} onClose={() => setReplacing(null)} />}
    </Card>
  );
}

function ReplaceDialog({ secret, projectId, onClose }: { secret: Secret; projectId: string | null; onClose: () => void }) {
  const [value, setValue] = useState("");
  const replace = useMutation({
    mutationFn: () => api.secrets.replace(projectId, secret.id, value),
    onSuccess: () => {
      toast.ok(`${secretLabel(secret.key) ?? secret.key} replaced`, "Runs started from now on use the new value.");
      onClose();
    },
    onError: (e) => toast.err("Could not replace the value", e),
  });
  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={secretLabel(secret.key) ? `Replace the ${secretLabel(secret.key)}` : <>Replace <span className="font-mono">{secret.key}</span></>}
      description="The current value is overwritten. It is never shown, so there is nothing to compare against."
      onSubmit={() => value && replace.mutate()}
      footer={
        <>
          <Button variant="secondary" onClick={onClose}>Cancel</Button>
          <Button type="submit" variant="primary" disabled={!value} loading={replace.isPending}>Replace value</Button>
        </>
      }
    >
      <Field label="New value">
        <Textarea
          autoFocus
          mono
          rows={3}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoComplete="off"
          spellCheck={false}
          className="[-webkit-text-security:disc]"
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              e.currentTarget.form?.requestSubmit();
            }
          }}
        />
      </Field>
    </Dialog>
  );
}
