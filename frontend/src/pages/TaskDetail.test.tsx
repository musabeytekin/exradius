import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Run, RunRouting, TaskDetail } from "@/lib/types";
import { AgentCard } from "./TaskDetail";

const T = "2026-11-08T17:10:03Z";
const run = (r: Partial<Run>): Run => ({
  id: "r1", state: "running", container_id: null, runtime_session_id: null, started_at: T, ended_at: null,
  error: null, summary: null, output: null, ...r,
});
const task = (r: Run | null): TaskDetail => ({
  task_id: "BANK-204", project: "bank", agent: "scout", state: "running", received_at: T, run_id: r?.id ?? null,
  instructions: "", context: null, correlation: null, attachments: [], run: r, info_requests: [], artifacts: [], events: [],
});
const routed = (routing: RunRouting | null) => task(run({ routing, agent_snapshot: { slug: "scout", name: "Scout" } }));
const picked = () => screen.getByText("Picked").nextSibling as HTMLElement;

describe("how the agent was picked", () => {
  it("says the task named it", () => {
    render(<AgentCard task={routed({ method: "named", agent: "scout", decided_at: T })} />);
    expect(picked()).toHaveTextContent(/^named by the task$/);
  });

  it("says it is the project's only agent", () => {
    render(<AgentCard task={routed({ method: "only_agent", agent: "scout", decided_at: T })} />);
    expect(picked()).toHaveTextContent(/^the project's only agent$/);
  });

  it("shows a decision with its confidence, a guess mark and the probabilities, highest first", () => {
    render(<AgentCard task={routed({
      method: "decided", agent: "scout", confidence: 0.48, guess: true,
      probabilities: { developer: 0.41, scout: 0.59 }, model: "gpt-6-luna", ms: 840,
      input_tokens: 9120, cached_tokens: 8960, decided_at: T,
    })} />);
    expect(picked()).toHaveTextContent("picked automatically");
    expect(within(picked()).getByLabelText("Confidence")).toHaveTextContent("48%");
    expect(within(picked()).getByText("guess")).toBeInTheDocument();
    const rows = within(within(picked()).getByRole("list", { name: "Probabilities" })).getAllByRole("listitem");
    expect(rows.map((r) => r.textContent)).toEqual(["scout59%", "developer41%"]);
  });

  it("has no guess mark for a confident decision", () => {
    render(<AgentCard task={routed({
      method: "decided", agent: "scout", confidence: 0.97, guess: false, probabilities: { developer: 0.03, scout: 0.97 }, decided_at: T,
    })} />);
    expect(within(picked()).getByLabelText("Confidence")).toHaveTextContent("97%");
    expect(screen.queryByText("guess")).toBeNull();
  });

  it("shows nothing for a run from before routing", () => {
    render(<AgentCard task={routed(null)} />);
    expect(screen.getByText("Scout")).toBeInTheDocument();
    expect(screen.queryByText("Picked")).toBeNull();
  });

  it("shows the pick before the run records its agent", () => {
    render(<AgentCard task={task(run({ state: "queued", started_at: null, routing: { method: "only_agent", agent: "scout", decided_at: T } }))} />);
    expect(picked()).toHaveTextContent("the project's only agent");
    expect(screen.queryByText("Model")).toBeNull();
  });
});
