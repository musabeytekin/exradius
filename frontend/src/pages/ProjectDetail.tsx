import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate, useParams, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { Tabs } from "@/components/ui/controls";
import { Icon } from "@/components/ui/icons";
import { Dialog, Menu } from "@/components/ui/overlays";
import { Button, Chip, EmptyState, Field, Input, Mono, PageHeader, Skeleton } from "@/components/ui/primitives";
import { api, ApiError } from "@/lib/api";
import { buttonClass } from "@/lib/buttonStyles";
import { useConfirm } from "@/lib/confirm";
import { qk, useEnvVars, useProject, useProjectAgents, useProjectSecrets, useRepositories, useServices } from "@/lib/queries";
import { SLUG_RE } from "@/lib/slug";
import { toast } from "@/lib/toast";
import type { Project } from "@/lib/types";
import type { ProjectTab } from "@/router";
import { AgentsTab } from "./project/AgentsTab";
import { EnvironmentTab } from "./project/EnvironmentTab";
import { NotesTab } from "./project/NotesTab";
import { RepositoriesTab } from "./project/RepositoriesTab";
import { SecretsTab } from "./project/SecretsTab";
import { ServicesTab } from "./project/ServicesTab";
import { VerifyTab } from "./project/VerifyTab";

export function ProjectDetailPage() {
  const { projectId } = useParams({ from: "/app/projects/$projectId" });
  const { tab = "repositories" } = useSearch({ from: "/app/projects/$projectId" });
  const navigate = useNavigate();
  const project = useProject(projectId);
  const repos = useRepositories(projectId);
  const envVars = useEnvVars(projectId);
  const secrets = useProjectSecrets(projectId);
  const agents = useProjectAgents(projectId);
  const services = useServices(projectId);
  const latest = useQuery({ queryKey: qk.verifications(projectId), queryFn: () => api.projects.verifications(projectId) });
  const [editing, setEditing] = useState(false);
  const confirm = useConfirm();
  const qc = useQueryClient();

  const remove = useMutation({
    mutationFn: () => api.projects.remove(projectId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok(`Project ${project.data?.slug} deleted`);
      navigate({ to: "/projects", search: {} });
    },
    onError: (e) => toast.err("Could not delete the project", e instanceof ApiError && e.status === 409 ? "Tasks still reference it." : e),
  });

  if (project.isError) {
    return (
      <EmptyState
        icon="projects"
        title="Project not found"
        action={<Link to="/projects" search={{}} className={buttonClass("secondary")}>All projects</Link>}
      >
        It may have been deleted.
      </EmptyState>
    );
  }
  const p = project.data;
  const failingRepos = repos.data?.filter((r) => r.last_check && !r.last_check.ok).length ?? 0;
  const verifyState = latest.data?.[0]?.state;

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
        title={p ? p.name : <Skeleton className="h-6 w-56" />}
        meta={
          p && (
            <>
              <Mono className="rounded bg-surface-2 px-1.5 py-0.5 text-xs text-muted">{p.slug}</Mono>
              <span className="flex items-center gap-1 text-xs text-subtle">
                <Icon name="layers" size={12} />
                <Mono className="text-xs text-subtle">{p.image ?? "default image"}</Mono>
              </span>
              {verifyState === "passed" && <Chip tone="ok" dot>verified</Chip>}
              {verifyState === "failed" && <Chip tone="err" dot>verification failed</Chip>}
            </>
          )
        }
        actions={
          p && (
            <>
              <Button
                variant="secondary"
                icon="send"
                disabled={!agents.data?.length}
                title={agents.data?.length ? undefined : "Attach an agent first"}
                onClick={() => navigate({ to: "/tasks", search: { send: "1", project: p.slug } })}
              >
                Send a task
              </Button>
              <Menu.Root>
                <Menu.Trigger asChild>
                  <Button variant="secondary" square icon="settings" aria-label="Project actions" />
                </Menu.Trigger>
                <Menu.Content>
                  <Menu.Item icon="edit" onSelect={() => setEditing(true)}>Rename</Menu.Item>
                  <Menu.Item icon="tasks" onSelect={() => navigate({ to: "/tasks", search: { project: p.slug } })}>Tasks of this project</Menu.Item>
                  <Menu.Separator />
                  <Menu.Item
                    danger
                    icon="trash"
                    onSelect={async () => {
                      const ok = await confirm({
                        title: `Delete project ${p.slug}?`,
                        description: "Its repositories, environment, secrets and agent attachments are deleted too. Mirrors on the worker stay until cleaned up.",
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
          <Tabs.Trigger value="repositories" count={repos.data?.length} attention={failingRepos > 0}>Repositories</Tabs.Trigger>
          <Tabs.Trigger value="environment" count={envVars.data?.length}>Environment</Tabs.Trigger>
          <Tabs.Trigger value="services" count={services.data?.length}>Services</Tabs.Trigger>
          <Tabs.Trigger value="secrets" count={secrets.data?.length}>Secrets</Tabs.Trigger>
          <Tabs.Trigger value="agents" count={agents.data?.length}>Agents</Tabs.Trigger>
          <Tabs.Trigger value="notes">Notes</Tabs.Trigger>
          <Tabs.Trigger value="verify" attention={verifyState === "failed"}>Verify</Tabs.Trigger>
        </Tabs.List>
        {p ? (
          <>
            <Tabs.Content value="repositories"><RepositoriesTab project={p} /></Tabs.Content>
            <Tabs.Content value="environment"><EnvironmentTab project={p} /></Tabs.Content>
            <Tabs.Content value="services"><ServicesTab project={p} /></Tabs.Content>
            <Tabs.Content value="secrets"><SecretsTab project={p} /></Tabs.Content>
            <Tabs.Content value="agents"><AgentsTab project={p} /></Tabs.Content>
            <Tabs.Content value="notes"><NotesTab key={p.id} project={p} /></Tabs.Content>
            <Tabs.Content value="verify"><VerifyTab project={p} /></Tabs.Content>
          </>
        ) : (
          <Skeleton className="h-64" />
        )}
      </Tabs.Root>
      {editing && p && <RenameDialog project={p} onOpenChange={setEditing} />}
    </>
  );
}

function RenameDialog({ project, onOpenChange }: { project: Project; onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const [name, setName] = useState(project.name);
  const [slug, setSlug] = useState(project.slug);
  const projects = useQuery({ queryKey: qk.projects, queryFn: api.projects.list });
  const taken = projects.data?.some((p) => p.slug === slug && p.id !== project.id);
  const slugError = !SLUG_RE.test(slug) ? "Lowercase letters, digits, dot, dash and underscore." : taken ? "Another project uses this slug." : null;
  const save = useMutation({
    mutationFn: () => api.projects.update(project.id, { name, slug }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok("Project updated");
      onOpenChange(false);
    },
    onError: (e) => toast.err("Could not update the project", e),
  });
  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title="Rename project"
      onSubmit={() => !slugError && name.trim() && save.mutate()}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={save.isPending} disabled={!!slugError || !name.trim()}>Save</Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="Name"><Input autoFocus value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field
          label="Slug"
          error={slugError}
          warning={!slugError && slug !== project.slug ? "Integrations that publish tasks with the old slug will be rejected." : undefined}
        >
          <Input mono value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase())} invalid={!!slugError} />
        </Field>
      </div>
    </Dialog>
  );
}
