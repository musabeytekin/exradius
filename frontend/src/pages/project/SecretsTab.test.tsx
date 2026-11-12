import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import { ConfirmContext } from "@/lib/confirm";
import type { Project, SecretStore, StoreKind } from "@/lib/types";
import { HAPPY, MISSING_KEYS, STORE_NOT_ALLOWED, STORE_UNREACHABLE, UNREACHABLE_REASON } from "@/test/projectFixtures";
import { MissingKeysForm, SecretsTab } from "./SecretsTab";

vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof import("@tanstack/react-router")>()),
  Link: ({ children, to, className }: { children: ReactNode; to: string; className?: string }) => <a href={to} className={className}>{children}</a>,
}));

describe("missing keys form", () => {
  it("has a write-only input per missing key and saves the filled ones together", async () => {
    const onSave = vi.fn().mockResolvedValue(true);
    render(<MissingKeysForm keys={["DB_PASSWORD", "API_TOKEN", "SENTRY_DSN"]} onSave={onSave} />);
    const user = userEvent.setup();

    const save = screen.getByRole("button", { name: "Save all" });
    expect(save).toBeDisabled();
    const db = screen.getByLabelText("DB_PASSWORD");
    expect(db).toHaveAttribute("type", "password");
    expect(screen.getByLabelText("API_TOKEN")).toHaveAttribute("type", "password");

    await user.type(db, "pw");
    await user.type(screen.getByLabelText("SENTRY_DSN"), "https://x");
    expect(save).toBeEnabled();
    await user.click(save);

    expect(onSave).toHaveBeenCalledTimes(1);
    expect(onSave).toHaveBeenCalledWith({ DB_PASSWORD: "pw", SENTRY_DSN: "https://x" });
    expect(db).toHaveValue("");
  });

  it("keeps the values when saving fails", async () => {
    const onSave = vi.fn().mockResolvedValue(false);
    render(<MissingKeysForm keys={["DB_PASSWORD"]} onSave={onSave} />);
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("DB_PASSWORD"), "pw");
    await user.click(screen.getByRole("button", { name: "Save all" }));
    expect(screen.getByLabelText("DB_PASSWORD")).toHaveValue("pw");
  });
});

const T = "2026-11-11T10:00:00Z";
const ALL = ["GITHUB_TOKEN", "DB_PASSWORD", "DOCS_TOKEN", "TRACKER_TOKEN"];
const KINDS: StoreKind[] = [
  { kind: "internal", label: "Exradius", external: false, available: true, reason: null, fields: [] },
  { kind: "vault", label: "HashiCorp Vault", external: true, available: true, reason: null, fields: [] },
];
const INTERNAL: SecretStore = {
  id: "s1", name: "bank-platform", kind: "internal", settings: {}, where: "", external: false, created_at: T,
  keys: ALL, error: null, used_by: ["bank"], allowed_projects: ["bank"],
};
const VAULT: SecretStore = {
  ...INTERNAL, kind: "vault", settings: { mount: "secret", path: "teams/bank" }, where: "secret/teams/bank", external: true,
};

function wrap(project: Project, confirm = vi.fn().mockResolvedValue(true)) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <TooltipPrimitive.Provider>
        <ConfirmContext.Provider value={confirm}>
          <SecretsTab project={project} />
        </ConfirmContext.Provider>
      </TooltipPrimitive.Provider>
    </QueryClientProvider>,
  );
  return confirm;
}

const noValueInputs = () => expect(document.querySelector('input[type="password"]')).toBeNull();

beforeEach(() => {
  vi.spyOn(api.stores, "kinds").mockResolvedValue(KINDS);
});
afterEach(() => vi.restoreAllMocks());

describe("secrets tab: internal store", () => {
  it("lists every required key as present and offers to replace each", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([INTERNAL]);
    vi.spyOn(api.stores, "keys").mockResolvedValue(ALL.map((key) => ({ key, updated_at: T })));
    wrap(HAPPY);

    expect(await screen.findAllByText("present")).toHaveLength(4);
    expect(screen.getByText("All 4 required keys present")).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Replace" })).toHaveLength(4);
    expect(screen.queryByRole("button", { name: "Save all" })).toBeNull();
  });

  it("asks for the missing keys write-only and saves them to the store", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([INTERNAL]);
    vi.spyOn(api.stores, "keys").mockResolvedValue(ALL.filter((k) => k !== "DB_PASSWORD").map((key) => ({ key })));
    const setKeys = vi.spyOn(api.stores, "setKeys").mockResolvedValue([]);
    wrap(MISSING_KEYS);
    const user = userEvent.setup();

    const input = await screen.findByLabelText("DB_PASSWORD");
    expect(screen.getByText("1 of 4 required keys missing")).toBeInTheDocument();
    expect(input).toHaveAttribute("type", "password");
    expect(screen.getAllByText("present")).toHaveLength(3);
    await user.type(input, "pw");
    await user.click(screen.getByRole("button", { name: "Save all" }));
    expect(setKeys).toHaveBeenCalledWith("s1", { DB_PASSWORD: "pw" });
  });
});

describe("secrets tab: store not allowed", () => {
  it("says the store does not allow the project, names the keys, and allows it after confirming", async () => {
    const store = { ...INTERNAL, allowed_projects: ["payments"] };
    vi.spyOn(api.stores, "list").mockResolvedValue([store]);
    vi.spyOn(api.stores, "keys").mockResolvedValue(ALL.map((key) => ({ key })));
    const update = vi.spyOn(api.stores, "update").mockResolvedValue({ ...store, allowed_projects: ["payments", "bank"] });
    const confirm = wrap(STORE_NOT_ALLOWED);
    const user = userEvent.setup();

    expect(await screen.findByText(/is not allowed for this project/)).toBeInTheDocument();
    expect(screen.getByText(ALL.join(", "))).toBeInTheDocument();
    // Neither present nor missing: the keys are not this project's to see.
    expect(screen.queryByText("present")).toBeNull();
    expect(screen.queryByText(/required keys missing/)).toBeNull();
    noValueInputs();

    await user.click(screen.getByRole("button", { name: "Allow bank" }));
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ title: "Allow bank to use bank-platform?" }));
    await waitFor(() => expect(update).toHaveBeenCalledWith("s1", { allowed_projects: ["payments", "bank"] }));
  });

  it("does not allow the project when the confirmation is declined", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([{ ...INTERNAL, allowed_projects: [] }]);
    vi.spyOn(api.stores, "keys").mockResolvedValue([]);
    const update = vi.spyOn(api.stores, "update");
    wrap(STORE_NOT_ALLOWED, vi.fn().mockResolvedValue(false));
    await userEvent.setup().click(await screen.findByRole("button", { name: "Allow bank" }));
    expect(update).not.toHaveBeenCalled();
  });

  it("follows the store's own list when the project report predates it", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([{ ...INTERNAL, allowed_projects: [] }]);
    vi.spyOn(api.stores, "keys").mockResolvedValue([]);
    wrap(HAPPY);
    expect(await screen.findByRole("button", { name: "Allow bank" })).toBeInTheDocument();
  });
});

describe("secrets tab: external store", () => {
  it("shows key presence read from Vault with nothing to type", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([VAULT]);
    vi.spyOn(api.stores, "keys").mockResolvedValue(["GITHUB_TOKEN", "DOCS_TOKEN", "TRACKER_TOKEN"].map((key) => ({ key })));
    const setKey = vi.spyOn(api.stores, "setKey");
    wrap(MISSING_KEYS);

    expect(await screen.findByText("1 of 4 required keys missing")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/Values are read from HashiCorp Vault at/)).toBeInTheDocument());
    expect(screen.getByText("secret/teams/bank")).toBeInTheDocument();
    expect(screen.getByText(/set or change them there\./)).toBeInTheDocument();
    const missing = screen.getByText("DB_PASSWORD").closest("tr") as HTMLElement;
    expect(within(missing).getByText("missing")).toBeInTheDocument();
    expect(screen.getAllByText("present")).toHaveLength(3);
    noValueInputs();
    expect(screen.queryByRole("button", { name: /Save all|Replace|Set value/ })).toBeNull();
    expect(setKey).not.toHaveBeenCalled();
  });

  it("shows an unreachable store with its reason and no key as missing", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([{ ...VAULT, keys: [], error: UNREACHABLE_REASON }]);
    vi.spyOn(api.stores, "keys").mockResolvedValue([]);
    wrap(STORE_UNREACHABLE);

    const alert = await screen.findByRole("alert");
    await waitFor(() => expect(alert).toHaveTextContent("HashiCorp Vault cannot be reached"));
    expect(alert).toHaveTextContent(UNREACHABLE_REASON);
    expect(screen.getByText("Its keys are unknown while it cannot be reached.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
    expect(screen.queryByText("missing")).toBeNull();
    expect(screen.queryByText(/required keys missing/)).toBeNull();
    expect(screen.queryByText("DB_PASSWORD")).toBeNull();
    noValueInputs();
  });

  it("does not ask a store the store list reports unreachable for its keys", async () => {
    // As the store page does (Stores.tsx: "Not asked again while the store's backend is known to be
    // out of reach") and ExternalStoreKeys itself does with useStoreKeys(store.error ? null : store.id).
    vi.spyOn(api.stores, "list").mockResolvedValue([{ ...VAULT, keys: [], error: UNREACHABLE_REASON }]);
    const keys = vi.spyOn(api.stores, "keys").mockResolvedValue([]);
    wrap(STORE_UNREACHABLE);
    await screen.findByText("Its keys are unknown while it cannot be reached.");
    expect(keys).not.toHaveBeenCalled();
  });

  it("treats a key list refused with 503 as unreachable, not missing", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([VAULT]);
    vi.spyOn(api.stores, "keys").mockRejectedValue(new ApiError(503, UNREACHABLE_REASON));
    wrap(STORE_UNREACHABLE);

    expect(await screen.findByText("Its keys are unknown while it cannot be reached.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(UNREACHABLE_REASON);
    expect(screen.queryByText("missing")).toBeNull();
    noValueInputs();
  });
});
