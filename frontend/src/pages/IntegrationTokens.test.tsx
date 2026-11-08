import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { ConfirmContext } from "@/lib/confirm";
import type { Bridge, IntegrationToken } from "@/lib/types";
import { BridgesCard, IntegrationTokensCard } from "./Settings";

const T = "2026-11-08T10:00:00Z";
const token = (t: Partial<IntegrationToken>): IntegrationToken =>
  ({ id: "t1", name: "jira", bridge: "jira", created_at: T, last_used_at: null, revoked_at: null, ...t });
const bridge = (b: Partial<Bridge>): Bridge => ({
  bridge: "jira", tokens: 0, projects: [], reported_at: null, reporting: false, software: null, started_at: null, problems: [], ...b,
});

function wrap(ui: ReactNode, confirm = vi.fn().mockResolvedValue(true)) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <TooltipPrimitive.Provider>
        <ConfirmContext.Provider value={confirm}>{ui}</ConfirmContext.Provider>
      </TooltipPrimitive.Provider>
    </QueryClientProvider>,
  );
  return confirm;
}

afterEach(() => vi.restoreAllMocks());

describe("integration tokens", () => {
  it("creates a token for a typed bridge and shows it once", async () => {
    vi.spyOn(api.bridges, "list").mockResolvedValue([]);
    const list = vi.spyOn(api.integrationTokens, "list").mockResolvedValue([]);
    const create = vi.spyOn(api.integrationTokens, "create").mockResolvedValue({ ...token({}), token: "exr_secret-value" });
    wrap(<IntegrationTokensCard />);
    expect(await screen.findByText("No tokens.")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "New token" }));
    const submit = screen.getByRole("button", { name: "Create" });
    expect(submit).toBeDisabled();
    list.mockResolvedValue([token({})]);
    await user.type(await screen.findByLabelText("Bridge"), " jira ");
    expect(screen.getByLabelText(/^Name/)).toHaveAttribute("placeholder", "jira");
    await user.click(submit);

    expect(create).toHaveBeenCalledWith({ bridge: "jira", name: null });
    expect(await screen.findByText("exr_secret-value")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Revoke the token jira" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByText("exr_secret-value")).toBeNull();
  });

  it("offers the known bridges, preselects the one without a token, and sends the name", async () => {
    vi.spyOn(api.bridges, "list").mockResolvedValue([
      bridge({ bridge: "github", tokens: 1, projects: ["saleor"] }),
      bridge({ bridge: "jira", tokens: 0, projects: ["bank", "plane"] }),
    ]);
    vi.spyOn(api.integrationTokens, "list").mockResolvedValue([token({ name: "github", bridge: "github" })]);
    const create = vi.spyOn(api.integrationTokens, "create").mockResolvedValue({ ...token({ name: "jira-prod" }), token: "exr_x" });
    wrap(<IntegrationTokensCard />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New token" }));

    const picker = await screen.findByRole("combobox", { name: "Bridge" });
    expect(picker).toHaveTextContent("jira");
    await user.type(screen.getByLabelText(/^Name/), "jira-prod");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(create).toHaveBeenCalledWith({ bridge: "jira", name: "jira-prod" });
  });

  it("takes a bridge no project uses yet, and refuses a name that defaults to a taken one", async () => {
    vi.spyOn(api.bridges, "list").mockResolvedValue([bridge({ bridge: "github", tokens: 1, projects: ["saleor"] })]);
    vi.spyOn(api.integrationTokens, "list").mockResolvedValue([token({ name: "github", bridge: "github" })]);
    const create = vi.spyOn(api.integrationTokens, "create").mockResolvedValue({ ...token({ name: "linear", bridge: "linear" }), token: "exr_x" });
    wrap(<IntegrationTokensCard />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New token" }));

    // The only bridge is preselected; its name is taken by its token.
    expect(await screen.findByRole("combobox", { name: "Bridge" })).toHaveTextContent("github");
    expect(screen.getByText("A token with this name exists.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();

    await user.click(screen.getByRole("combobox", { name: "Bridge" }));
    await user.click(await screen.findByRole("option", { name: "Another bridge" }));
    await user.type(screen.getByLabelText("Bridge"), "Linear");
    expect(screen.getByText("Lowercase letters, digits and dashes, starting with a letter.")).toBeInTheDocument();
    await user.clear(screen.getByLabelText("Bridge"));
    await user.type(screen.getByLabelText("Bridge"), "linear");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(create).toHaveBeenCalledWith({ bridge: "linear", name: null });
  });

  it("drops the shown token once it is revoked", async () => {
    vi.spyOn(api.bridges, "list").mockResolvedValue([]);
    const list = vi.spyOn(api.integrationTokens, "list").mockResolvedValue([]);
    vi.spyOn(api.integrationTokens, "create").mockResolvedValue({ ...token({}), token: "exr_secret-value" });
    vi.spyOn(api.integrationTokens, "revoke").mockResolvedValue(undefined);
    wrap(<IntegrationTokensCard />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New token" }));
    list.mockResolvedValue([token({})]);
    await user.type(await screen.findByLabelText("Bridge"), "jira");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("exr_secret-value")).toBeInTheDocument();
    list.mockResolvedValue([token({ revoked_at: T })]);
    await user.click(await screen.findByRole("button", { name: "Revoke the token jira" }));
    expect(await screen.findByText("revoked")).toBeInTheDocument();
    expect(screen.queryByText("exr_secret-value")).toBeNull();
  });

  it("lists tokens with their bridge and revokes after confirming", async () => {
    vi.spyOn(api.bridges, "list").mockResolvedValue([]);
    vi.spyOn(api.integrationTokens, "list").mockResolvedValue([
      token({ name: "jira-prod", last_used_at: new Date(Date.now() - 3600_000).toISOString() }),
      token({ id: "t2", name: "old", bridge: "github", revoked_at: T }),
    ]);
    const revoke = vi.spyOn(api.integrationTokens, "revoke").mockResolvedValue(undefined);
    const confirm = wrap(<IntegrationTokensCard />);

    const row = (await screen.findByText("jira-prod")).closest("tr") as HTMLElement;
    expect(within(row).getByText("jira")).toBeInTheDocument();
    expect(within(row).getByText("1h ago")).toBeInTheDocument();
    const old = screen.getByText("old").closest("tr") as HTMLElement;
    expect(within(old).getByText("github")).toBeInTheDocument();
    expect(within(old).getByText("revoked")).toBeInTheDocument();
    expect(within(old).queryByRole("button")).toBeNull();

    const user = userEvent.setup();
    await user.click(within(row).getByRole("button", { name: "Revoke the token jira-prod" }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(revoke).toHaveBeenCalledWith("t1");
  });
});

describe("bridges", () => {
  it("shows each bridge's projects, tokens, last report and problems", async () => {
    const recent = new Date(Date.now() - 120_000).toISOString();
    vi.spyOn(api.bridges, "list").mockResolvedValue([
      bridge({ bridge: "github", tokens: 1, projects: [] }),
      bridge({
        bridge: "jira", tokens: 2, projects: ["bank", "plane"], reported_at: recent, reporting: true,
        software: "exradius-jira 0.2.0 (exradius-bridge-kit 0.1.0)",
        problems: [
          { project: "plane", message: "bridges/jira.yaml: projects: PLANE is also claimed by bank" },
          { project: null, message: "Jira answered 401 for the bot account" },
        ],
      }),
      bridge({ bridge: "linear", reported_at: "2026-11-01T08:00:00Z", reporting: false }),
    ]);
    wrap(<BridgesCard />);

    const jira = await screen.findByRole("listitem", { name: "jira" });
    expect(within(jira).getByText("Projects").nextSibling).toHaveTextContent("bankplane");
    expect(within(jira).getByText("Tokens").nextSibling).toHaveTextContent("2");
    expect(within(jira).getByText("2m ago")).toBeInTheDocument();
    expect(within(jira).getByText("exradius-jira 0.2.0 (exradius-bridge-kit 0.1.0)")).toBeInTheDocument();
    const problems = within(jira).getByRole("list", { name: "Problems reported by jira" });
    expect(problems).toHaveTextContent("planebridges/jira.yaml: projects: PLANE is also claimed by bank");
    expect(problems).toHaveTextContent("jiraJira answered 401 for the bot account");

    expect(within(screen.getByRole("listitem", { name: "github" })).getByText("no reports")).toBeInTheDocument();
    expect(within(screen.getByRole("listitem", { name: "linear" })).getByText(/^not reporting since /)).toBeInTheDocument();
  });
});
