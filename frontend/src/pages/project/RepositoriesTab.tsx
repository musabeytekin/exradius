import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { SecretPicker } from "@/components/secrets";
import { PickList, Table } from "@/components/ui/controls";
import { Icon } from "@/components/ui/icons";
import { Dialog, Tooltip } from "@/components/ui/overlays";
import { Banner, Button, Card, CardHeader, Chip, EmptyState, Field, Input, Mono, SkeletonRows, Spinner } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { useConfirm } from "@/lib/confirm";
import { relativeTime } from "@/lib/format";
import { qk, useGlobalSecrets, useProjectSecrets, useRepositories } from "@/lib/queries";
import { FOLDER_RE, repoNameFromUrl } from "@/lib/slug";
import { toast } from "@/lib/toast";
import type { Project, Repository, RepositoryCheck } from "@/lib/types";

export function RepositoriesTab({ project }: { project: Project }) {
  const repos = useRepositories(project.id);
  const projectSecrets = useProjectSecrets(project.id);
  const globals = useGlobalSecrets();
  const [editing, setEditing] = useState<Repository | "new" | null>(null);
  const [checking, setChecking] = useState<Set<string>>(new Set());
  const qc = useQueryClient();
  const confirm = useConfirm();
  const secretName = (id: string | null) =>
    id ? [...(projectSecrets.data ?? []), ...(globals.data ?? [])].find((s) => s.id === id)?.key ?? "unknown secret" : null;

  const runCheck = async (r: Repository) => {
    setChecking((s) => new Set(s).add(r.id));
    try {
      const res = await api.repositories.check(project.id, r.id);
      if (res.ok) toast.ok(`${r.name}: reachable`, `${res.branches.length} branches; ${r.branch} found.`);
      else toast.err(`${r.name}: check failed`, res.error);
    } catch (e) {
      toast.err("The check did not run", e);
    } finally {
      setChecking((s) => {
        const n = new Set(s);
        n.delete(r.id);
        return n;
      });
      qc.invalidateQueries({ queryKey: qk.repositories(project.id) });
    }
  };

  const remove = useMutation({
    mutationFn: (r: Repository) => api.repositories.remove(project.id, r.id),
    onSuccess: (_, r) => {
      qc.invalidateQueries({ queryKey: qk.repositories(project.id) });
      toast.ok(`Repository ${r.name} removed`);
    },
    onError: (e) => toast.err("Could not remove the repository", e),
  });

  return (
    <Card>
      <CardHeader
        title="Repositories"
        description={<>Each is cloned into its own folder of the run directory, from a mirror refreshed with <code className="font-mono">git fetch</code>.</>}
        actions={<Button size="sm" variant="primary" icon="plus" onClick={() => setEditing("new")}>Add repository</Button>}
      />
      {repos.isLoading ? (
        <SkeletonRows rows={2} />
      ) : !repos.data?.length ? (
        <EmptyState
          icon="branch"
          title="No repositories"
          action={<Button variant="secondary" icon="plus" onClick={() => setEditing("new")}>Add a repository</Button>}
        >
          A project can run without repositories, but most agents need code. Each repository is checked with <code className="font-mono">git ls-remote</code> when you save it.
        </EmptyState>
      ) : (
        <Table.Root>
          <Table.Head>
            <tr>
              <Table.Th>Folder</Table.Th>
              <Table.Th>URL</Table.Th>
              <Table.Th>Branch</Table.Th>
              <Table.Th>Credential</Table.Th>
              <Table.Th>Check</Table.Th>
              <Table.Th />
            </tr>
          </Table.Head>
          <tbody>
            {repos.data.map((r) => (
              <Table.Row key={r.id}>
                <Table.Td><Mono className="font-medium">{r.name}/</Mono></Table.Td>
                <Table.Td className="max-w-[320px]"><Mono className="block truncate text-xs text-muted" title={r.url}>{r.url}</Mono></Table.Td>
                <Table.Td>
                  <span className="inline-flex items-center gap-1 font-mono text-xs"><Icon name="branch" size={12} className="text-subtle" />{r.branch}</span>
                </Table.Td>
                <Table.Td>
                  {r.auth_secret_id ? (
                    <span className="inline-flex items-center gap-1 font-mono text-xs text-muted"><Icon name="lock" size={12} />{secretName(r.auth_secret_id)}</span>
                  ) : (
                    <span className="text-xs text-subtle">public</span>
                  )}
                </Table.Td>
                <Table.Td><CheckChip check={r.last_check} running={checking.has(r.id)} /></Table.Td>
                <Table.Td className="w-[1%] whitespace-nowrap text-right">
                  <div className="flex justify-end gap-1">
                    <Button size="sm" variant="ghost" icon="refresh" loading={checking.has(r.id)} onClick={() => runCheck(r)}>Check</Button>
                    <Button size="sm" variant="ghost" square icon="edit" aria-label={`Edit ${r.name}`} onClick={() => setEditing(r)} />
                    <Button
                      size="sm"
                      variant="ghost"
                      square
                      icon="trash"
                      aria-label={`Remove ${r.name}`}
                      onClick={async () => {
                        if (await confirm({ title: `Remove ${r.name}?`, description: "Runs will no longer clone it. The mirror on the worker stays.", confirmLabel: "Remove" })) remove.mutate(r);
                      }}
                    />
                  </div>
                </Table.Td>
              </Table.Row>
            ))}
          </tbody>
        </Table.Root>
      )}
      {editing && <RepositoryDialog project={project} repo={editing === "new" ? null : editing} existing={repos.data ?? []} onClose={() => setEditing(null)} />}
    </Card>
  );
}

function CheckChip({ check, running }: { check: RepositoryCheck | null; running: boolean }) {
  if (running) return <Chip tone="info"><Spinner size={10} />checking</Chip>;
  if (!check) return <Chip tone="neutral">not checked</Chip>;
  return (
    <Tooltip content={check.ok ? `${check.branches.length} branches seen` : check.error}>
      <span className="inline-flex items-center gap-2">
        <Chip tone={check.ok ? "ok" : "err"} dot>{check.ok ? "reachable" : "failed"}</Chip>
        <span className="text-xs text-subtle">{relativeTime(check.checked_at)}</span>
      </span>
    </Tooltip>
  );
}

/**
 * Add or edit a repository. Saving runs the check; the check's branch list becomes the
 * branch pick list, with the remote's default preselected when the branch is missing.
 */
function RepositoryDialog({ project, repo, existing, onClose }: { project: Project; repo: Repository | null; existing: Repository[]; onClose: () => void }) {
  const qc = useQueryClient();
  const [saved, setSaved] = useState<Repository | null>(repo);
  const [url, setUrl] = useState(repo?.url ?? "");
  const [name, setName] = useState(repo?.name ?? "");
  const [nameTouched, setNameTouched] = useState(!!repo);
  const [secretId, setSecretId] = useState<string | null>(repo?.auth_secret_id ?? null);
  const [branch, setBranch] = useState(repo?.branch ?? "main");
  const [check, setCheck] = useState<RepositoryCheck | null>(repo?.last_check ?? null);
  const [busy, setBusy] = useState<null | "save" | "check">(null);

  const folder = nameTouched ? name : repoNameFromUrl(url);
  const taken = existing.some((r) => r.name === folder && r.id !== saved?.id);
  const folderError = !folder ? null : !FOLDER_RE.test(folder) ? "Letters, digits, dot, dash and underscore." : taken ? "Another repository uses this folder." : null;
  const branches = check?.branches ?? [];
  const sourceChanged = !!saved && (url !== saved.url || secretId !== saved.auth_secret_id);
  const dirty = !saved || url !== saved.url || folder !== saved.name || secretId !== saved.auth_secret_id || branch !== saved.branch;
  // A check made against another URL or credential does not describe this form.
  const visibleCheck = sourceChanged ? null : check;

  const saveAndCheck = async () => {
    setBusy("save");
    try {
      const body = { name: folder, url: url.trim(), branch: branch.trim() || "main", auth_secret_id: secretId };
      const r = saved ? await api.repositories.update(project.id, saved.id, body) : await api.repositories.create(project.id, body);
      setSaved(r);
      qc.invalidateQueries({ queryKey: qk.repositories(project.id) });
      setBusy("check");
      const res = await api.repositories.check(project.id, r.id);
      setCheck(res);
      qc.invalidateQueries({ queryKey: qk.repositories(project.id) });
      if (res.ok) {
        toast.ok(`${r.name} saved and reachable`);
        onClose();
      } else if (res.branches.length && !res.branches.includes(body.branch)) {
        // Offer the remote's branches; preselect the default.
        setBranch(res.default_branch ?? res.branches[0]);
      }
    } catch (e) {
      toast.err("Could not save the repository", e);
    } finally {
      setBusy(null);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(o) => !o && onClose()}
      title={repo ? `Edit ${repo.name}` : "Add a repository"}
      description="Saving checks the URL, branch and credential with git ls-remote."
      width={600}
      onSubmit={() => url.trim() && folder && !folderError && saveAndCheck()}
      footer={
        <>
          {saved && !dirty && visibleCheck && !visibleCheck.ok && (
            <span className="mr-auto text-xs text-muted">Saved. Fix the problem above, or close and come back later.</span>
          )}
          <Button variant="secondary" onClick={onClose}>{saved && !dirty ? "Close" : "Cancel"}</Button>
          <Button type="submit" variant="primary" loading={!!busy} disabled={!url.trim() || !folder || !!folderError || (!!saved && !dirty && !!visibleCheck?.ok)}>
            {busy === "check" ? "Checking…" : saved && !dirty ? "Check again" : "Save and check"}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-4">
        <Field label="URL" htmlFor="repo-url" hint="HTTPS. Private repositories need a credential below.">
          <Input id="repo-url" autoFocus={!repo} mono value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://github.com/acme/billing-api.git" spellCheck={false} />
        </Field>
        <div className="grid grid-cols-2 gap-3">
          <Field label="Folder" htmlFor="repo-name" error={folderError} hint={!folderError && "Where it is cloned in the run directory."}>
            <Input
              id="repo-name"
              mono
              value={folder}
              invalid={!!folderError}
              onChange={(e) => {
                setNameTouched(true);
                setName(e.target.value);
              }}
              placeholder="billing-api"
            />
          </Field>
          <Field
            label="Branch"
            htmlFor="repo-branch"
            hint={branches.length && !sourceChanged ? `${branches.length} branches on the remote` : "Run the check to list branches."}
          >
            {branches.length > 0 && !sourceChanged ? (
              <PickList
                id="repo-branch"
                mono
                value={branch}
                onChange={(v) => v && setBranch(v)}
                options={[
                  ...(branches.includes(branch) ? [] : [{ value: branch, label: branch, description: "not on the remote" }]),
                  ...branches.map((b) => ({ value: b, label: b, meta: b === check?.default_branch ? "default" : undefined })),
                ]}
              />
            ) : (
              <Input id="repo-branch" mono value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="main" />
            )}
          </Field>
        </div>
        <Field label="Credential" hint="A token with read access. Used only to fetch the mirror; never written into the clone.">
          <SecretPicker
            projectId={project.id}
            projectSlug={project.slug}
            value={secretId}
            onChange={setSecretId}
            defaultCreateKey={/github\.com/.test(url) ? "GITHUB_TOKEN" : /gitlab/.test(url) ? "GITLAB_TOKEN" : "GIT_TOKEN"}
          />
        </Field>
        {visibleCheck && !busy && (
          visibleCheck.ok ? (
            <Banner tone="ok" icon="check" title="Reachable">
              Branch <code className="font-mono">{branch}</code> found among {visibleCheck.branches.length} branches. Checked {relativeTime(visibleCheck.checked_at)}.
            </Banner>
          ) : (
            <Banner tone="err" title="The check failed">
              <span className="break-words font-mono text-xs">{visibleCheck.error}</span>
              {visibleCheck.branches.length > 0 && !visibleCheck.branches.includes(saved?.branch ?? "") && (
                <div className="mt-1 text-xs">Pick a branch from the list above and save again.</div>
              )}
            </Banner>
          )
        )}
      </div>
    </Dialog>
  );
}
