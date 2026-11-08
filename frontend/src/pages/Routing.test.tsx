import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { ConfirmContext } from "@/lib/confirm";
import type { RoutingSettings } from "@/lib/types";
import { RoutingCard } from "./Settings";

const ago = (ms: number) => new Date(Date.now() - ms).toISOString();
const settings = (r: Partial<RoutingSettings>): RoutingSettings =>
  ({ key_set: false, key_set_at: null, last_test: null, model: "gpt-6-luna", guess_below: 0.6, ...r });

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

const row = (label: string) => screen.getByText(label).nextSibling as HTMLElement;

afterEach(() => vi.restoreAllMocks());

describe("agent routing", () => {
  it("sets the key write-only and tests it", async () => {
    vi.spyOn(api.routing, "get").mockResolvedValue(settings({}));
    const setKey = vi.spyOn(api.routing, "setKey").mockResolvedValue(settings({ key_set: true, key_set_at: ago(1000) }));
    const test = vi.spyOn(api.routing, "test").mockResolvedValue({ ok: true, at: ago(1000), ms: 840, detail: "answered web, confidence 0.97" });
    wrap(<RoutingCard />);

    expect(await screen.findByText("gpt-6-luna")).toBeInTheDocument();
    expect(row("OpenAI API key")).toHaveTextContent("not set");
    expect(screen.queryByRole("button", { name: "Test" })).toBeNull();
    const input = screen.getByLabelText("OpenAI API key");
    expect(input).toHaveAttribute("type", "password");
    const save = screen.getByRole("button", { name: "Save" });
    expect(save).toBeDisabled();

    const user = userEvent.setup();
    await user.type(input, " sk-test ");
    await user.click(save);
    expect(setKey).toHaveBeenCalledWith("sk-test");

    // A new key is tested at once.
    expect(await screen.findByText("answered web, confidence 0.97")).toBeInTheDocument();
    expect(test).toHaveBeenCalledOnce();
    expect(row("Last test")).toHaveTextContent("passed");
    expect(row("Last test")).toHaveTextContent("840ms");
    expect(row("OpenAI API key")).not.toHaveTextContent("not set");
    expect(screen.queryByLabelText("OpenAI API key")).toBeNull();
  });

  it("shows the stored last test and replaces it with a new one", async () => {
    vi.spyOn(api.routing, "get").mockResolvedValue(settings({
      key_set: true, key_set_at: ago(7200_000),
      last_test: { ok: false, at: ago(3600_000), ms: null, detail: "OpenAI answered 401: Incorrect API key provided: sk-…" },
    }));
    vi.spyOn(api.routing, "test").mockResolvedValue({ ok: true, at: ago(1000), ms: 1200, detail: "answered web, confidence 0.97" });
    wrap(<RoutingCard />);

    expect(await screen.findByText("OpenAI answered 401: Incorrect API key provided: sk-…")).toBeInTheDocument();
    expect(row("Last test")).toHaveTextContent("failed");
    expect(row("Last test")).toHaveTextContent("1h ago");
    expect(row("OpenAI API key")).toHaveTextContent("2h ago");

    await userEvent.setup().click(screen.getByRole("button", { name: "Test" }));
    expect(await screen.findByText("answered web, confidence 0.97")).toBeInTheDocument();
    expect(row("Last test")).toHaveTextContent("passed");
    expect(row("Last test")).toHaveTextContent("1.2s");
    expect(screen.queryByText(/Incorrect API key/)).toBeNull();
  });

  it("replaces and deletes the key", async () => {
    const get = vi.spyOn(api.routing, "get").mockResolvedValue(settings({ key_set: true, key_set_at: ago(60_000) }));
    const remove = vi.spyOn(api.routing, "removeKey").mockResolvedValue(undefined);
    const confirm = wrap(<RoutingCard />);
    const user = userEvent.setup();

    expect(await screen.findByText("not tested")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Replace" }));
    expect(screen.getByLabelText("New OpenAI API key")).toHaveAttribute("type", "password");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("New OpenAI API key")).toBeNull();

    get.mockResolvedValue(settings({}));
    await user.click(screen.getByRole("button", { name: "Delete the OpenAI API key" }));
    expect(confirm).toHaveBeenCalledOnce();
    expect(remove).toHaveBeenCalledOnce();
    expect(await screen.findByText("not set")).toBeInTheDocument();
  });
});
