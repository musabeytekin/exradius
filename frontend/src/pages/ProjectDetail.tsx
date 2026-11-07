import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { FreshnessChip } from "@/components/FreshnessChip";
import { HealthChip } from "@/components/HealthChip";
import { Tabs, Table } from "@/components/ui/controls";
import { StateChip } from "@/components/ui/data";
import { Icon } from "@/components/ui/icons";
import { Dialog, Menu, Tooltip } from "@/components/ui/overlays";
import { Button, Card, EmptyState, Field, Input, Mono, PageHeader, Skeleton, SkeletonRows } from "@/components/ui/primitives";
import { api, ApiError } from "@/lib/api";
import { buttonClass } from "@/lib/buttonStyles";
import { useConfirm } from "@/lib/confirm";
import { absoluteTime, relativeTime } from "@/lib/format";
import { agentSlugsOf, commitOf, errorsOf, keyName, projectHealth, projectName, shortCommit, storeNameOf } from "@/lib/projectConfig";
import { qk, useProject, useStoreKeys, useStores } from "@/lib/queries";
import { toast } from "@/lib/toast";
import type { Project } from "@/lib/types";
import type { ProjectTab } from "@/router";
import { ConfigTab } from "./project/ConfigTab";
import { SecretsTab } from "./project/SecretsTab";
import { VerifyTab } from "./project/VerifyTab";

export function ProjectDetailPage() {
  const { projectId } = useParams({ from: "/app/projects/$projectId" });
  const { tab = "config" } = useSearch({ from: "/app/projects/$projectId" });
  const navigate = useNavigate();
  const project = useProject(projectId);
  const latest = useQuery({ queryKey: qk.verifications(projectId), queryFn: () => api.projects.verifications(projectId) });
  const [editing, setEditing] = useState(false);
  const confirm = useConfirm();
  const qc = useQueryClient();

  // Presence of keys against the live store, so a value set in the Secrets tab shows at once.
  const p = project.data;
  const stores = useStores();
  const storeName = p ? storeNameOf(p) : null;
  const store = storeName ? stores.data?.find((s) => s.name === storeName) : undefined;
  const keys = useStoreKeys(store?.id);
  const storeKeys = !storeName || !stores.isSuccess ? undefined : !store ? null : keys.data?.map(keyName);

  const remove = useMutation({
    mutationFn: () => api.projects.remove(projectId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok(`Project ${p?.slug} deleted`);
      navigate({ to: "/projects", search: {} });
    },
    onError: (e) => toast.err("Could not delete the project", e),
  });

  if (project.isError) {
    const gone = project.error instanceof ApiError && project.error.status === 404;
    return (
      <EmptyState
        icon="projects"
        title={gone ? "Project not found" : "Could not load the project"}
        action={<Link to="/projects" search={{}} className={buttonClass("secondary")}>All projects</Link>}
      >
        {gone ? "It may have been deleted." : (project.error as Error).message}
      </EmptyState>
    );
  }
  const health = p ? projectHealth(p, storeKeys) : null;
  const agents = agentSlugsOf(p);
  const verifyState = latest.data?.[0]?.state ?? p?.verification?.state;
  const commit = p ? commitOf(p) : null;

  return (
    <>
      <PageHeader
        breadcrumb={
          <>
            <Link to="/projects" search={{}} className="hover:text-fg">Projects</Link>
            <Icon name="chevronRight" size={12} />
            <span className="text-muted">{p?.slug ?? "…"}</span>
          </>
        }
        title={p ? projectName(p) : <Skeleton className="h-6 w-56" />}
        meta={
          p && (
            <>
              <Mono className="rounded bg-surface-2 px-1.5 py-0.5 text-xs text-muted">{p.slug}</Mono>
              {health && <HealthChip info={health} />}
              <FreshnessChip project={p} />
              {p.config_url && <span className="flex min-w-0 items-center gap-1 text-xs text-subtle">
                <Icon name="branch" size={12} />
                <Mono className="max-w-[420px] truncate text-xs text-subtle" title={p.config_url}>{p.config_url}</Mono>
                {p.config_branch && <Mono className="text-xs text-subtle">@{p.config_branch}</Mono>}
                {commit && <Mono className="text-xs text-muted" title={commit}>{shortCommit(commit)}</Mono>}
              </span>}
            </>
          )
        }
        actions={
          p && (
            <>
              <Button
                variant="secondary"
                icon="send"
                disabled={!agents.length}
                title={agents.length ? undefined : "The config defines no agents"}
                onClick={() => navigate({ to: "/tasks", search: { send: "1", project: p.slug } })}
              >
                Send a task
              </Button>
              <Menu.Root>
                <Menu.Trigger asChild>
                  <Button variant="secondary" square icon="settings" aria-label="Project actions" />
                </Menu.Trigger>
                <Menu.Content>
                  <Menu.Item icon="edit" onSelect={() => setEditing(true)}>Branch and path</Menu.Item>
                  <Menu.Separator />
                  <Menu.Item
                    danger
                    icon="trash"
                    onSelect={async () => {
                      const ok = await confirm({
                        title: `Delete project ${p.slug}?`,
                        description: "Exradius forgets the config repository. The repository itself and the secret store are not touched.",
                        typeToConfirm: p.slug,
                        confirmLabel: "Delete project",
                      });
                      if (ok) remove.mutate();
                    }}
                  >
                    Delete project
                  </Menu.Item>
                </Menu.Content>
              </Menu.Root>
            </>
          )
        }
      />
      <Tabs.Root value={tab} onValueChange={(t) => navigate({ to: "/projects/$projectId", params: { projectId }, search: { tab: t as ProjectTab }, replace: true })}>
        <Tabs.List className="mb-5">
          <Tabs.Trigger value="config" attention={!!p && (errorsOf(p).length > 0 || health?.health === "unreadable")}>Config</Tabs.Trigger>
          <Tabs.Trigger value="secrets" attention={health?.health === "missing_secrets"}>Secrets</Tabs.Trigger>
          <Tabs.Trigger value="verify" attention={verifyState === "failed"}>Verify</Tabs.Trigger>
          <Tabs.Trigger value="tasks">Tasks</Tabs.Trigger>
        </Tabs.List>
        {p ? (
          <>
            <Tabs.Content value="config"><ConfigTab project={p} /></Tabs.Content>
            <Tabs.Content value="secrets"><SecretsTab project={p} /></Tabs.Content>
            <Tabs.Content value="verify"><VerifyTab project={p} /></Tabs.Content>
            <Tabs.Content value="tasks"><ProjectTasks project={p} /></Tabs.Content>
          </>
        ) : (
          <Skeleton className="h-64" />
        )}
      </Tabs.Root>
      {editing && p && <SourceDialog project={p} onOpenChange={setEditing} />}
    </>
  );
}

function ProjectTasks({ project }: { project: Project }) {
  const navigate = useNavigate();
  const filters = { project: project.slug, limit: 50 };
  const tasks = useQuery({ queryKey: qk.tasks(filters), queryFn: () => api.tasks.list(filters), refetchInterval: 5000 });
  return (
    <Card>
      {tasks.isLoading ? (
        <SkeletonRows rows={3} />
      ) : tasks.isError ? (
        <p className="px-4 py-6 text-center text-sm text-err">Could not load tasks: {(tasks.error as Error).message}</p>
      ) : !tasks.data?.length ? (
        <EmptyState compact icon="tasks" title="No tasks for this project" />
      ) : (
        <>
          <Table.Root>
            <Table.Head>
              <tr><Table.Th>Task</Table.Th><Table.Th>Agent</Table.Th><Table.Th>State</Table.Th><Table.Th className="text-right">Received</Table.Th></tr>
            </Table.Head>
            <tbody>
              {tasks.data.map((t) => (
                <Table.Row key={t.task_id} onClick={() => navigate({ to: "/tasks/$taskId", params: { taskId: t.task_id } })}>
                  <Table.Td><Mono className="font-medium">{t.task_id}</Mono></Table.Td>
                  <Table.Td><Mono className="text-xs text-muted">{t.agent}</Mono></Table.Td>
                  <Table.Td><StateChip state={t.state} /></Table.Td>
                  <Table.Td className="text-right">
                    <Tooltip content={absoluteTime(t.received_at)}><span className="text-xs tabular text-subtle">{relativeTime(t.received_at)}</span></Tooltip>
                  </Table.Td>
                </Table.Row>
              ))}
            </tbody>
          </Table.Root>
          <div className="flex justify-end border-t border-border px-4 py-2">
            <Link to="/tasks" search={{ project: project.slug }} className={buttonClass("ghost", "sm")}>All tasks of {project.slug}<Icon name="arrowRight" size={13} /></Link>
          </div>
        </>
      )}
    </Card>
  );
}

function SourceDialog({ project, onOpenChange }: { project: Project; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const [branch, setBranch] = useState(project.config_branch ?? "");
  const [path, setPath] = useState(project.config_path ?? "");
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: () => api.projects.update(project.id, { config_branch: branch.trim() || null, config_path: path.trim() || null }),
    onSuccess: (p) => {
      if (p && typeof p === "object" && "id" in p) qc.setQueryData(qk.project(project.id), p);
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok("Config source updated");
      onOpenChange(false);
    },
    onError: (e) => setError((e as Error).message),
  });
  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title="Branch and path"
      description={<Mono className="text-xs text-muted">{project.config_url}</Mono>}
      onSubmit={() => save.mutate()}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={save.isPending}>Save</Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="Branch" optional>
          <Input mono autoFocus value={branch} onChange={(e) => { setBranch(e.target.value); setError(null); }} placeholder="remote default" spellCheck={false} />
        </Field>
        <Field label="Path" optional error={error}>
          <Input mono value={path} onChange={(e) => { setPath(e.target.value); setError(null); }} placeholder="exradius.yaml" spellCheck={false} />
        </Field>
      </div>
    </Dialog>
  );
}
