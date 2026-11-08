import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { ConfirmContext } from "@/lib/confirm";
import type { IntegrationToken } from "@/lib/types";
import { IntegrationTokensCard } from "./Settings";

const T = "2026-11-08T10:00:00Z";
const token = (t: Partial<IntegrationToken>): IntegrationToken =>
  ({ id: "t1", name: "jira", created_at: T, last_used_at: null, revoked_at: null, ...t });

function wrap(ui: ReactNode, confirm = vi.fn().mockResolvedValue(true)) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ConfirmContext.Provider value={confirm}>{ui}</ConfirmContext.Provider>
    </QueryClientProvider>,
  );
  return confirm;
}

afterEach(() => vi.restoreAllMocks());

describe("integration tokens", () => {
  it("creates a token and shows it once", async () => {
    const list = vi.spyOn(api.integrationTokens, "list").mockResolvedValue([]);
    const create = vi.spyOn(api.integrationTokens, "create").mockResolvedValue({ ...token({}), token: "exr_secret-value" });
    wrap(<IntegrationTokensCard />);
    expect(await screen.findByText("No tokens.")).toBeInTheDocument();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "New token" }));
    const submit = screen.getByRole("button", { name: "Create" });
    expect(submit).toBeDisabled();
    list.mockResolvedValue([token({})]);
    await user.type(screen.getByPlaceholderText("jira"), " jira ");
    await user.click(submit);

    expect(create).toHaveBeenCalledWith("jira");
    expect(await screen.findByText("exr_secret-value")).toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Revoke the token jira" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByText("exr_secret-value")).toBeNull();
  });

  it("drops the shown token once it is revoked", async () => {
    const list = vi.spyOn(api.integrationTokens, "list").mockResolvedValue([]);
    vi.spyOn(api.integrationTokens, "create").mockResolvedValue({ ...token({}), token: "exr_secret-value" });
    vi.spyOn(api.integrationTokens, "revoke").mockResolvedValue(undefined);
    wrap(<IntegrationTokensCard />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "New token" }));
    list.mockResolvedValue([token({})]);
    await user.type(screen.getByPlaceholderText("jira"), "jira");
    await user.click(screen.getByRole("button", { name: "Create" }));
    expect(await screen.findByText("exr_secret-value")).toBeInTheDocument();
    list.mockResolvedValue([token({ revoked_at: T })]);
    await user.click(await screen.findByRole("button", { name: "Revoke the token jira" }));
    expect(await screen.findByText("revoked")).toBeInTheDocument();
    expect(screen.queryByText("exr_secret-value")).toBeNull();
  });

  it("lists tokens, refuses a taken name and revokes after confirming", async () => {
    vi.spyOn(api.integrationTokens, "list").mockResolvedValue([
      token({ last_used_at: new Date(Date.now() - 3600_000).toISOString() }),
      token({ id: "t2", name: "old", revoked_at: T }),
    ]);
    const revoke = vi.spyOn(api.integrationTokens, "revoke").mockResolvedValue(undefined);
    const confirm = wrap(<IntegrationTokensCard />);

    const row = (await screen.findByText("jira")).closest("tr") as HTMLElement;
    expect(within(row).getByText("1h ago")).toBeInTheDocument();
    const old = screen.getByText("old").closest("tr") as HTMLElement;
    expect(within(old).getByText("revoked")).toBeInTheDocument();
    expect(within(old).queryByRole("button")).toBeNull();

    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "New token" }));
    await user.type(screen.getByPlaceholderText("jira"), "jira");
    expect(screen.getByText("A token with this name exists.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create" })).toBeDisabled();

    await user.click(within(row).getByRole("button", { name: "Revoke the token jira" }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(revoke).toHaveBeenCalledWith("t1");
  });
});
