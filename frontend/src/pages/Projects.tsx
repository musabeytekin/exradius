import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { FreshnessChip } from "@/components/FreshnessChip";
import { HealthChip } from "@/components/HealthChip";
import { Table } from "@/components/ui/controls";
import { Dialog } from "@/components/ui/overlays";
import { Banner, Button, Card, EmptyState, Field, Input, Mono, PageHeader, SkeletonRows } from "@/components/ui/primitives";
import { api, refusal } from "@/lib/api";
import { commitOf, projectHealth, projectName, shortCommit, storeNameOf } from "@/lib/projectConfig";
import { qk, useProjects } from "@/lib/queries";
import { toast } from "@/lib/toast";
import { useStoreKeyIndex } from "@/lib/useStoreKeyIndex";

export function ProjectsPage() {
  const projects = useProjects();
  const storeKeys = useStoreKeyIndex();
  const { create } = useSearch({ from: "/app/projects" });
  const navigate = useNavigate();
  const [open, setOpen] = useState(!!create);
  const list = projects.data ?? [];

  const setDialog = (o: boolean) => {
    setOpen(o);
    if (!o && create) navigate({ to: "/projects", search: {}, replace: true });
  };

  return (
    <>
      <PageHeader
        title="Projects"
        actions={<Button variant="primary" icon="plus" onClick={() => setOpen(true)}>Add project</Button>}
      />
      <Card>
        {projects.isLoading ? (
          <SkeletonRows rows={3} />
        ) : projects.isError ? (
          <p className="px-4 py-6 text-center text-sm text-err">Could not load projects: {(projects.error as Error).message}</p>
        ) : list.length === 0 ? (
          <EmptyState icon="projects" title="No projects" action={<Button variant="primary" icon="plus" onClick={() => setOpen(true)}>Add project</Button>} />
        ) : (
          <Table.Root>
            <Table.Head>
              <tr>
                <Table.Th>Project</Table.Th>
                <Table.Th>Config repository</Table.Th>
                <Table.Th>Commit</Table.Th>
                <Table.Th>Verified</Table.Th>
                <Table.Th className="text-right">Health</Table.Th>
              </tr>
            </Table.Head>
            <tbody>
              {list.map((p) => {
                const commit = commitOf(p);
                return (
                  <Table.Row key={p.id} onClick={() => navigate({ to: "/projects/$projectId", params: { projectId: p.id }, search: {} })}>
                    <Table.Td>
                      <Mono className="font-medium">{p.slug}</Mono>
                      <div className="text-xs text-muted">{projectName(p)}</div>
                    </Table.Td>
                    <Table.Td className="max-w-[480px]">
                      <Mono className="block truncate text-xs text-muted" title={p.config_url}>{p.config_url || "—"}</Mono>
                      <span className="text-xs text-subtle">{p.config_branch ?? "remote default"}{p.config_path ? ` · ${p.config_path}` : ""}</span>
                    </Table.Td>
                    <Table.Td>{commit ? <Mono className="text-xs" title={commit}>{shortCommit(commit)}</Mono> : <span className="text-xs text-subtle">—</span>}</Table.Td>
                    <Table.Td><FreshnessChip project={p} /></Table.Td>
                    <Table.Td className="text-right"><HealthChip info={projectHealth(p, storeKeys(storeNameOf(p)))} /></Table.Td>
                  </Table.Row>
                );
              })}
            </tbody>
          </Table.Root>
        )}
      </Card>
      {open && <AddProjectDialog onOpenChange={setDialog} />}
    </>
  );
}

function AddProjectDialog({ onOpenChange }: { onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const [url, setUrl] = useState("");
  const [branch, setBranch] = useState("");
  const [path, setPath] = useState("");
  const [error, setError] = useState<string | null>(null);

  const create = useMutation({
    mutationFn: () => api.projects.create({ config_url: url.trim(), config_branch: branch.trim() || null, config_path: path.trim() || null }),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok(`Project ${p.slug} added`);
      onOpenChange(false);
      navigate({ to: "/projects/$projectId", params: { projectId: p.id }, search: { tab: "config" } });
    },
    onError: (e) => {
      const m = refusal(e);
      if (m) setError(m);
      else toast.err("Could not add the project", e);
    },
  });

  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title="Add project"
      width={560}
      onSubmit={() => url.trim() && create.mutate()}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={create.isPending} disabled={!url.trim()}>Add project</Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="Config repository URL" htmlFor="p-url">
          <Input id="p-url" mono autoFocus value={url} onChange={(e) => { setUrl(e.target.value); setError(null); }} placeholder="https://github.com/acme/bank-config.git" spellCheck={false} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Branch" htmlFor="p-branch" optional>
            <Input id="p-branch" mono value={branch} onChange={(e) => { setBranch(e.target.value); setError(null); }} placeholder="remote default" spellCheck={false} />
          </Field>
          <Field label="Path" htmlFor="p-path" optional>
            <Input id="p-path" mono value={path} onChange={(e) => { setPath(e.target.value); setError(null); }} placeholder="exradius.yaml" spellCheck={false} />
          </Field>
        </div>
        {error && <Banner tone="err" title="The config could not be read"><span className="whitespace-pre-wrap font-mono text-xs">{error}</span></Banner>}
      </div>
    </Dialog>
  );
}
