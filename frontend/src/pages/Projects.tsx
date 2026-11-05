import { useMutation, useQueries, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useState } from "react";
import { ImageField, SetupCommandsEditor } from "@/components/projectFields";
import { Table } from "@/components/ui/controls";
import { Dialog } from "@/components/ui/overlays";
import { Button, Card, Chip, EmptyState, Field, Input, Mono, PageHeader, SkeletonRows } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { qk, useProjects } from "@/lib/queries";
import { SLUG_RE, slugify } from "@/lib/slug";
import { toast } from "@/lib/toast";

export function ProjectsPage() {
  const projects = useProjects();
  const { create } = useSearch({ from: "/app/projects" });
  const navigate = useNavigate();
  const [open, setOpen] = useState(!!create);
  const list = projects.data ?? [];
  const repos = useQueries({ queries: list.map((p) => ({ queryKey: qk.repositories(p.id), queryFn: () => api.repositories.list(p.id) })) });
  const agents = useQueries({ queries: list.map((p) => ({ queryKey: qk.projectAgents(p.id), queryFn: () => api.projectAgents.list(p.id) })) });

  const setDialog = (o: boolean) => {
    setOpen(o);
    if (!o && create) navigate({ to: "/projects", search: {}, replace: true });
  };

  return (
    <>
      <PageHeader
        title="Projects"
        description="A project is the working directory a run starts in: its repositories, environment, secrets and image."
        actions={<Button variant="primary" icon="plus" onClick={() => setOpen(true)}>New project</Button>}
      />
      <Card>
        {projects.isLoading ? (
          <SkeletonRows rows={3} />
        ) : list.length === 0 ? (
          <EmptyState
            icon="projects"
            title="No projects yet"
            action={<Button variant="primary" icon="plus" onClick={() => setOpen(true)}>Create a project</Button>}
          >
            Tasks name a project by slug. Create one, add its repositories, then verify it prepares cleanly.
          </EmptyState>
        ) : (
          <Table.Root>
            <Table.Head>
              <tr>
                <Table.Th>Project</Table.Th>
                <Table.Th>Image</Table.Th>
                <Table.Th>Repositories</Table.Th>
                <Table.Th>Agents</Table.Th>
                <Table.Th className="text-right">Created</Table.Th>
              </tr>
            </Table.Head>
            <tbody>
              {list.map((p, i) => {
                const r = repos[i]?.data;
                const failing = r?.filter((x) => x.last_check && !x.last_check.ok).length ?? 0;
                return (
                  <Table.Row key={p.id} onClick={() => navigate({ to: "/projects/$projectId", params: { projectId: p.id }, search: {} })}>
                    <Table.Td>
                      <div className="font-medium text-fg">{p.name}</div>
                      <Mono className="text-xs text-muted">{p.slug}</Mono>
                    </Table.Td>
                    <Table.Td>{p.image ? <Mono className="text-xs text-muted">{p.image}</Mono> : <span className="text-xs text-subtle">default image</span>}</Table.Td>
                    <Table.Td>
                      <div className="flex items-center gap-2">
                        <span className="tabular">{r?.length ?? "—"}</span>
                        {failing > 0 && <Chip tone="err" dot>{failing} failing</Chip>}
                      </div>
                    </Table.Td>
                    <Table.Td className="tabular">{agents[i]?.data?.length ?? "—"}</Table.Td>
                    <Table.Td className="text-right text-xs text-subtle">{relativeTime(p.created_at)}</Table.Td>
                  </Table.Row>
                );
              })}
            </tbody>
          </Table.Root>
        )}
      </Card>
      {open && <CreateProjectDialog onOpenChange={setDialog} />}
    </>
  );
}

function CreateProjectDialog({ onOpenChange }: { onOpenChange: (o: boolean) => void }) {
  const qc = useQueryClient();
  const navigate = useNavigate();
  const projects = useProjects();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [image, setImage] = useState<string>("");
  const [setup, setSetup] = useState("");
  const effectiveSlug = slugTouched ? slug : slugify(name);
  const taken = projects.data?.some((p) => p.slug === effectiveSlug);
  const slugError = !effectiveSlug ? null : !SLUG_RE.test(effectiveSlug) ? "Lowercase letters, digits, dot, dash and underscore." : taken ? "Another project uses this slug." : null;

  const create = useMutation({
    mutationFn: () =>
      api.projects.create({
        name: name.trim(),
        slug: effectiveSlug,
        image: image.trim() || null,
        setup_commands: setup.split("\n").map((l) => l.trim()).filter(Boolean),
      }),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: qk.projects });
      toast.ok(`Project ${p.slug} created`, "Add its repositories next.");
      onOpenChange(false);
      navigate({ to: "/projects/$projectId", params: { projectId: p.id }, search: { tab: "repositories" } });
    },
    onError: (e) => toast.err("Could not create the project", e),
  });

  return (
    <Dialog
      open
      onOpenChange={onOpenChange}
      title="New project"
      width={560}
      onSubmit={() => name.trim() && effectiveSlug && !slugError && create.mutate()}
      footer={
        <>
          <Button variant="secondary" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" variant="primary" loading={create.isPending} disabled={!name.trim() || !effectiveSlug || !!slugError}>Create project</Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <div className="grid grid-cols-2 gap-3">
          <Field label="Name" htmlFor="p-name">
            <Input id="p-name" autoFocus value={name} onChange={(e) => setName(e.target.value)} placeholder="Billing platform" />
          </Field>
          <Field label="Slug" htmlFor="p-slug" error={slugError} hint={!slugError && "Tasks name the project by this."}>
            <Input
              id="p-slug"
              mono
              value={effectiveSlug}
              invalid={!!slugError}
              onChange={(e) => {
                setSlugTouched(true);
                setSlug(e.target.value.toLowerCase());
              }}
              placeholder="billing-platform"
            />
          </Field>
        </div>
        <ImageField value={image} onChange={setImage} />
        <Field label="Setup commands" optional hint="Run in order in the project directory before each run. A non-zero exit fails the run.">
          <SetupCommandsEditor value={setup} onChange={setSetup} />
        </Field>
      </div>
    </Dialog>
  );
}
