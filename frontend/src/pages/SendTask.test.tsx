import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { HAPPY } from "@/test/projectFixtures";
import { SendTaskDialog } from "./Tasks";

const navigate = vi.fn();
vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof import("@tanstack/react-router")>()),
  useNavigate: () => navigate,
}));

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

afterEach(() => vi.restoreAllMocks());

async function fill(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Task id"), "BANK-204");
  await user.type(screen.getByPlaceholderText(/negative totals/), "Fix the invoice totals.");
}

describe("send a task", () => {
  it("lets Exradius pick the agent by default and sends none", async () => {
    vi.spyOn(api.projects, "list").mockResolvedValue([HAPPY]);
    vi.spyOn(api.projects, "get").mockResolvedValue(HAPPY);
    const send = vi.spyOn(api.tasks, "send").mockResolvedValue({ queue: "exradius.tasks", message: {} });
    wrap(<SendTaskDialog initialProject="bank" onClose={() => {}} />);
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByRole("combobox", { name: "Agent" })).toHaveTextContent("Pick automatically"));
    await fill(user);
    await user.click(screen.getByRole("button", { name: "Publish task" }));

    expect(send).toHaveBeenCalledOnce();
    const message = send.mock.calls[0][0];
    expect(message).toMatchObject({ task_id: "BANK-204", project: "bank", instructions: "Fix the invoice totals." });
    expect(message).not.toHaveProperty("agent");
    expect(navigate).toHaveBeenCalledWith({ to: "/tasks/$taskId", params: { taskId: "BANK-204" } });
  });

  it("keeps the project's agents after the automatic pick", async () => {
    vi.spyOn(api.projects, "list").mockResolvedValue([HAPPY]);
    vi.spyOn(api.projects, "get").mockResolvedValue(HAPPY);
    const send = vi.spyOn(api.tasks, "send").mockResolvedValue({ queue: "exradius.tasks", message: {} });
    wrap(<SendTaskDialog initialProject="bank" onClose={() => {}} />);
    const user = userEvent.setup();

    await waitFor(() => expect(screen.getByRole("combobox", { name: "Agent" })).toBeEnabled());
    await user.click(screen.getByRole("combobox", { name: "Agent" }));
    const options = await screen.findAllByRole("option");
    expect(options.map((o) => o.textContent)).toEqual(["Pick automatically", "developerDeveloper", "scout"]);
    await user.click(screen.getByRole("option", { name: "scout" }));
    await fill(user);
    await user.click(screen.getByRole("button", { name: "Publish task" }));
    expect(send.mock.calls[0][0]).toMatchObject({ agent: "scout" });
  });
});
