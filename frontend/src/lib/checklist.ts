// The setup checklist (onboarding §3.1), derived from state and never stored.

import type { Project, ProjectAgent, ProviderStatus, Repository, Verification } from "./types";

export type ChecklistState = "done" | "todo" | "optional";

export interface ChecklistItem {
  id: "provider" | "project" | "repository" | "verification" | "agent" | "task";
  title: string;
  state: ChecklistState;
  detail: string;
  /** Where to go to complete it. */
  href: string;
  action: string;
}

export interface ChecklistInput {
  /** `provider` from GET /api/system/status. */
  provider: ProviderStatus | undefined;
  projects: Project[];
  repositories: Record<string, Repository[]>;
  /** Newest first, per project id. */
  verifications: Record<string, Verification[]>;
  attachments: Record<string, ProjectAgent[]>;
  agentCount: number;
  completedTaskCount: number;
}

export function deriveChecklist(input: ChecklistInput): ChecklistItem[] {
  const { provider: prov, projects, repositories, verifications, attachments, agentCount, completedTaskCount } = input;
  const first = projects[0];
  const projectHref = (p: Project | undefined, tab?: string) =>
    p ? `/projects/${p.id}${tab ? `?tab=${tab}` : ""}` : "/projects";

  const def = prov?.default ?? null;
  const tested = def?.last_test ?? null;
  const provider: ChecklistItem = {
    id: "provider",
    title: "A provider is configured and tested",
    state: tested?.ok ? "done" : "todo",
    detail: !prov?.configured || !def
      ? "Add the model credential runs use. The first provider becomes the default."
      : !tested
        ? `${def.name} is the default but has not been tested.`
        : tested.ok
          ? `${def.name} is the default and passed its test.`
          : `The last test of ${def.name} failed${tested.error ? `: ${tested.error}` : "."}`,
    href: !prov?.configured || !def ? "/providers?create=1" : "/providers",
    action: !prov?.configured || !def ? "Add a provider" : "Test it",
  };

  const project: ChecklistItem = {
    id: "project",
    title: "A project exists",
    state: projects.length > 0 ? "done" : "todo",
    detail: projects.length > 0
      ? `${projects.length} project${projects.length === 1 ? "" : "s"}: ${projects.slice(0, 3).map((p) => p.slug).join(", ")}${projects.length > 3 ? "…" : ""}`
      : "A project groups repositories, environment and secrets.",
    href: "/projects?create=1",
    action: "Create a project",
  };

  const withRepos = projects.filter((p) => (repositories[p.id] ?? []).length > 0);
  const checked = projects.find((p) => (repositories[p.id] ?? []).some((r) => r.last_check?.ok));
  const repository: ChecklistItem = {
    id: "repository",
    title: "A repository passed a check",
    state: checked ? "done" : "todo",
    detail: checked
      ? `${checked.slug}: ${(repositories[checked.id] ?? []).filter((r) => r.last_check?.ok).map((r) => r.name).join(", ")}`
      : withRepos.length > 0
        ? "Run the check to confirm the URL, branch and credential."
        : "Add a repository and check it with git ls-remote.",
    href: projectHref(withRepos[0] ?? first, "repositories"),
    action: withRepos.length > 0 ? "Check a repository" : "Add a repository",
  };

  const passed = projects.find((p) => (verifications[p.id] ?? [])[0]?.state === "passed");
  const failed = projects.find((p) => (verifications[p.id] ?? [])[0]?.state === "failed");
  const verification: ChecklistItem = {
    id: "verification",
    title: "The project passed verification",
    state: passed ? "done" : withRepos.length === 0 && projects.length > 0 ? "optional" : "todo",
    detail: passed
      ? `${passed.slug} prepared cleanly in a throwaway directory.`
      : withRepos.length === 0 && projects.length > 0
        ? "Optional: no project has repositories to prepare."
        : failed
          ? `The last verification of ${failed.slug} failed.`
          : "A dry run: mirrors, clones and setup commands, without an agent.",
    href: projectHref(failed ?? withRepos[0] ?? first, "verify"),
    action: "Verify",
  };

  const attachedTo = projects.find((p) => (attachments[p.id] ?? []).length > 0);
  const agent: ChecklistItem = {
    id: "agent",
    title: "An agent is attached to a project",
    state: attachedTo ? "done" : "todo",
    detail: attachedTo
      ? `${attachedTo.slug} has ${(attachments[attachedTo.id] ?? []).length} agent${(attachments[attachedTo.id] ?? []).length === 1 ? "" : "s"}.`
      : agentCount > 0
        ? "Attach an agent to a project so tasks can name both."
        : "Create an agent from a starter prompt, then attach it.",
    href: agentCount > 0 ? projectHref(first, "agents") : "/agents?create=1",
    action: agentCount > 0 ? "Attach an agent" : "Create an agent",
  };

  const task: ChecklistItem = {
    id: "task",
    title: "A first task completed",
    state: completedTaskCount > 0 ? "done" : "todo",
    detail: completedTaskCount > 0 ? "Exradius has run end to end." : "Send a task from the panel and follow it live.",
    href: "/tasks?send=1",
    action: "Send a task",
  };

  return [provider, project, repository, verification, agent, task];
}

export function checklistProgress(items: ChecklistItem[]) {
  const done = items.filter((i) => i.state !== "todo").length;
  return { done, total: items.length, complete: done === items.length };
}
