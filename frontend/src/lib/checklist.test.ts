import { describe, expect, it } from "vitest";
import { checklistProgress, deriveChecklist, type ChecklistInput } from "./checklist";
import type { Project, ProjectAgent, ProviderStatus, Repository, Verification } from "./types";

const project = (id: string, slug = id): Project => ({ id, slug, name: slug, image: null, setup_commands: [], created_at: "2026-11-05T10:00:00Z" });
const repo = (id: string, projectId: string, ok: boolean | null): Repository => ({
  id, project_id: projectId, name: id, url: `https://example.com/${id}.git`, branch: "main", auth_secret_id: null,
  last_check: ok === null ? null : { ok, branches: ["main"], default_branch: "main", error: ok ? null : "auth failed", checked_at: "2026-11-05T10:00:00Z" },
});
const verification = (projectId: string, state: Verification["state"]): Verification => ({
  id: `v-${projectId}-${state}`, project_id: projectId, state, created_at: "2026-11-05T10:00:00Z", started_at: null, ended_at: null, error: null, steps: [],
});
const providerStatus = (last: { ok: boolean; error?: string } | null): ProviderStatus => ({
  configured: true, count: 1,
  default: { id: "pr1", name: "Anthropic", kind: "anthropic", auth_method: "api_key", last_test: last && { ok: last.ok, tested_at: "2026-11-05T10:00:00Z", detail: null, error: last.error ?? null } },
});
const attachment = (projectId: string, agentId: string): ProjectAgent => ({ project_id: projectId, agent_id: agentId, connection_overrides: {}, secret_overrides: {} });

const empty: ChecklistInput = {
  provider: { configured: false, count: 0, default: null },
  projects: [], repositories: {}, verifications: {}, attachments: {}, agentCount: 0, completedTaskCount: 0,
};

const states = (input: ChecklistInput) => Object.fromEntries(deriveChecklist(input).map((i) => [i.id, i.state]));

describe("deriveChecklist", () => {
  it("starts with everything to do on a fresh installation", () => {
    expect(states(empty)).toEqual({
      provider: "todo", project: "todo", repository: "todo", verification: "todo", agent: "todo", task: "todo",
    });
    expect(checklistProgress(deriveChecklist(empty))).toEqual({ done: 0, total: 6, complete: false });
  });

  it("needs the default provider to have passed its test", () => {
    const none = deriveChecklist(empty)[0];
    expect(none.href).toBe("/providers?create=1");
    const untested = deriveChecklist({ ...empty, provider: providerStatus(null) })[0];
    expect(untested.state).toBe("todo");
    expect(untested.detail).toMatch(/not been tested/);
    expect(untested.href).toBe("/providers");
    const failed = deriveChecklist({ ...empty, provider: providerStatus({ ok: false, error: "401 invalid x-api-key" }) })[0];
    expect(failed.state).toBe("todo");
    expect(failed.detail).toContain("401 invalid x-api-key");
    const passed = deriveChecklist({ ...empty, provider: providerStatus({ ok: true }) })[0];
    expect(passed.state).toBe("done");
    expect(passed.title).toBe("A provider is configured and tested");
  });

  it("treats a missing status as not configured rather than crashing", () => {
    expect(states({ ...empty, provider: undefined }).provider).toBe("todo");
  });

  it("makes verification optional when no project has repositories", () => {
    const s = states({ ...empty, projects: [project("p1")], repositories: { p1: [] } });
    expect(s.project).toBe("done");
    expect(s.repository).toBe("todo");
    expect(s.verification).toBe("optional");
    // An optional item counts as done for progress.
    expect(checklistProgress(deriveChecklist({ ...empty, projects: [project("p1")] })).done).toBe(2);
  });

  it("requires a repository whose last check passed, not just any repository", () => {
    const failing = { ...empty, projects: [project("p1")], repositories: { p1: [repo("api", "p1", false), repo("web", "p1", null)] } };
    expect(states(failing).repository).toBe("todo");
    expect(states(failing).verification).toBe("todo");
    const items = deriveChecklist(failing);
    expect(items.find((i) => i.id === "repository")!.href).toBe("/projects/p1?tab=repositories");
    const passing = { ...failing, repositories: { p1: [repo("api", "p1", false), repo("web", "p1", true)] } };
    expect(states(passing).repository).toBe("done");
    expect(deriveChecklist(passing).find((i) => i.id === "repository")!.detail).toContain("web");
  });

  it("reads only the latest verification of each project", () => {
    const base = { ...empty, projects: [project("p1")], repositories: { p1: [repo("api", "p1", true)] } };
    expect(states({ ...base, verifications: { p1: [verification("p1", "failed"), verification("p1", "passed")] } }).verification).toBe("todo");
    expect(states({ ...base, verifications: { p1: [verification("p1", "passed"), verification("p1", "failed")] } }).verification).toBe("done");
    const failed = deriveChecklist({ ...base, verifications: { p1: [verification("p1", "failed")] } }).find((i) => i.id === "verification")!;
    expect(failed.detail).toMatch(/failed/);
    expect(failed.href).toBe("/projects/p1?tab=verify");
  });

  it("is satisfied by any project, not only the first", () => {
    const s = states({
      ...empty,
      projects: [project("p1"), project("p2")],
      repositories: { p1: [], p2: [repo("api", "p2", true)] },
      verifications: { p2: [verification("p2", "passed")] },
      attachments: { p1: [], p2: [attachment("p2", "a1")] },
      agentCount: 1,
    });
    expect(s).toMatchObject({ repository: "done", verification: "done", agent: "done" });
  });

  it("separates creating an agent from attaching one", () => {
    const noAgent = deriveChecklist({ ...empty, projects: [project("p1")] }).find((i) => i.id === "agent")!;
    expect(noAgent.href).toBe("/agents?create=1");
    const unattached = deriveChecklist({ ...empty, projects: [project("p1")], agentCount: 2 }).find((i) => i.id === "agent")!;
    expect(unattached.state).toBe("todo");
    expect(unattached.href).toBe("/projects/p1?tab=agents");
  });

  it("completes when a task has completed", () => {
    const all: ChecklistInput = {
      provider: providerStatus({ ok: true }),
      projects: [project("p1")],
      repositories: { p1: [repo("api", "p1", true)] },
      verifications: { p1: [verification("p1", "passed")] },
      attachments: { p1: [attachment("p1", "a1")] },
      agentCount: 1,
      completedTaskCount: 1,
    };
    expect(checklistProgress(deriveChecklist(all))).toEqual({ done: 6, total: 6, complete: true });
    expect(checklistProgress(deriveChecklist({ ...all, completedTaskCount: 0 })).complete).toBe(false);
  });
});
