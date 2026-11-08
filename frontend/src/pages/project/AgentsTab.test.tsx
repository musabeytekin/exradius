import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import type { AgentProfile } from "@/lib/types";
import { AgentProfilesView } from "./AgentsTab";

const wrap = (ui: ReactNode) => render(<TooltipPrimitive.Provider>{ui}</TooltipPrimitive.Provider>);
const card = (name: string) => screen.getByRole("heading", { name: new RegExp(`^${name}`) }).closest(".rounded-lg") as HTMLElement;

const LONG_ROLE = Array.from({ length: 12 }, (_, i) => `Rule ${i + 1}: keep changes small.`).join("\n");
const PROFILES: AgentProfile[] = [
  {
    slug: "developer", name: "Developer", role: LONG_ROLE,
    connections: [
      {
        name: "github", kind: "catalog github", tools_listed_at: new Date(Date.now() - 3600_000).toISOString(),
        tools: [
          { name: "get_issue", description: "Get an issue.", annotations: { readOnlyHint: true, openWorldHint: true } },
          { name: "delete_branch", description: "Delete a branch.", annotations: { readOnlyHint: false, destructiveHint: true, title: "Delete branch" } },
          { name: "create_issue", description: null },
        ],
      },
      { name: "tracker", kind: "custom http", tools: null, tools_note: "not listed since this connection changed" },
    ],
  },
  { slug: "scout", name: "scout", role: "You investigate and report.", connections: [{ name: "docs", kind: "custom stdio", tools: null, tools_note: "not listed yet" }] },
];

describe("agent profiles", () => {
  it("shows each agent with its role, connections and tools", async () => {
    wrap(<AgentProfilesView profiles={PROFILES} />);
    const dev = card("Developer");
    expect(within(dev).getByText("developer")).toBeInTheDocument();

    const github = within(dev).getByRole("list", { name: "Tools of github" });
    const getIssue = within(github).getByText("get_issue").closest("li") as HTMLElement;
    expect(within(getIssue).getByText("read-only")).toBeInTheDocument();
    expect(within(getIssue).getByText("open world")).toBeInTheDocument();
    expect(within(getIssue).queryByText("destructive")).toBeNull();
    expect(within(getIssue).getByText("Get an issue.")).toBeInTheDocument();
    const del = within(github).getByText("delete_branch").closest("li") as HTMLElement;
    expect(within(del).getByText("destructive")).toBeInTheDocument();
    expect(within(del).getByText("Delete branch")).toBeInTheDocument();
    expect(within(dev).getByText("catalog github")).toBeInTheDocument();
    expect(within(dev).getByText("listed 1h ago")).toBeInTheDocument();
    expect(within(dev).getByText("custom http")).toBeInTheDocument();
    expect(within(dev).getByText("not listed since this connection changed")).toBeInTheDocument();

    // A long role is clamped until opened; a short one is shown whole.
    const role = within(dev).getByLabelText("Role");
    expect(role.textContent).toBe(LONG_ROLE);
    expect(role).toHaveClass("max-h-40");
    await userEvent.setup().click(within(dev).getByRole("button", { name: "Show all" }));
    expect(role).not.toHaveClass("max-h-40");

    const scout = card("scout");
    expect(within(scout).getByLabelText("Role")).toHaveTextContent("You investigate and report.");
    expect(within(scout).queryByRole("button", { name: "Show all" })).toBeNull();
    expect(within(scout).getByText("not listed yet")).toBeInTheDocument();
  });

  it("collapses a long tool list", async () => {
    const tools = Array.from({ length: 14 }, (_, i) => ({ name: `tool_${i}` }));
    wrap(<AgentProfilesView profiles={[{ slug: "dev", name: "Dev", role: null, connections: [{ name: "big", kind: "custom stdio", tools }] }]} />);
    expect(screen.queryByText("tool_0")).toBeNull();
    await userEvent.setup().click(screen.getByRole("button", { name: "14 tools" }));
    expect(screen.getByText("tool_13")).toBeInTheDocument();
  });

  it("says when there is nothing to show", () => {
    wrap(<AgentProfilesView profiles={[]} configErrors />);
    expect(screen.getByText("No agents")).toBeInTheDocument();
    expect(screen.getByText("Nothing to show until the file reads cleanly.")).toBeInTheDocument();
  });
});
