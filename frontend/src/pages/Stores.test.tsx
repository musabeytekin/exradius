import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "@/lib/api";
import { ConfirmContext } from "@/lib/confirm";
import { qk } from "@/lib/queries";
import type { SecretStore, StoreChange, StoreKind } from "@/lib/types";
import { HAPPY, UNREACHABLE_REASON } from "@/test/projectFixtures";
import { StoreDetailPage, StoresPage } from "./Stores";

const navigate = vi.fn();
const params = { storeId: "s1" };
vi.mock("@tanstack/react-router", async (original) => ({
  ...(await original<typeof import("@tanstack/react-router")>()),
  useNavigate: () => navigate,
  useSearch: () => ({}),
  useParams: () => params,
  Link: ({ children, to, className }: { children: ReactNode; to: string; className?: string }) => <a href={to} className={className}>{children}</a>,
}));

const T = "2026-11-11T10:00:00Z";
const KINDS: StoreKind[] = [
  { kind: "internal", label: "Exradius", external: false, available: true, reason: null, fields: [] },
  {
    kind: "vault", label: "HashiCorp Vault", external: true, available: true, reason: null,
    fields: [
      { name: "mount", label: "KV mount", description: "The mount path of a KV version 2 secrets engine.", default: "secret", required: false },
      { name: "path", label: "Path", description: "The secret's path in the mount; its fields are the store's keys.", default: null, required: true },
    ],
  },
];
const store = (s: Partial<SecretStore>): SecretStore => ({
  id: "s1", name: "bank-platform", kind: "internal", settings: {}, where: "", external: false, created_at: T,
  keys: [], error: null, used_by: [], allowed_projects: [], ...s,
});
const INTERNAL = store({ keys: ["DB_PASSWORD", "GITHUB_TOKEN"], used_by: ["bank"], allowed_projects: ["bank"] });
const VAULT = store({
  id: "s2", name: "payments", kind: "vault", settings: { mount: "secret", path: "teams/payments" }, where: "secret/teams/payments",
  external: true, keys: ["STRIPE_KEY"],
});
const DOWN = store({
  id: "s3", name: "ledger", kind: "vault", settings: { mount: "secret", path: "teams/ledger" }, where: "secret/teams/ledger",
  external: true, keys: [], error: UNREACHABLE_REASON,
});
const BILLING = { ...HAPPY, id: "p9", slug: "billing", store: "other", config: null };

function wrap(ui: ReactNode, confirm = vi.fn().mockResolvedValue(true), stores?: SecretStore[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  // As after the list page: the store list already in the cache.
  if (stores) qc.setQueryData(qk.stores, stores);
  render(
    <QueryClientProvider client={qc}>
      <TooltipPrimitive.Provider>
        <ConfirmContext.Provider value={confirm}>{ui}</ConfirmContext.Provider>
      </TooltipPrimitive.Provider>
    </QueryClientProvider>,
  );
  return confirm;
}

beforeEach(() => {
  navigate.mockReset();
  params.storeId = "s1";
  vi.spyOn(api.stores, "kinds").mockResolvedValue(KINDS);
  vi.spyOn(api.projects, "list").mockResolvedValue([HAPPY, BILLING]);
  vi.spyOn(api.stores, "changes").mockResolvedValue([]);
});
afterEach(() => vi.restoreAllMocks());

const rowOf = (name: string) => screen.getByText(name).closest("tr") as HTMLElement;

describe("stores list", () => {
  it("lists the stores with where their values are kept", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([INTERNAL, VAULT, DOWN]);
    const keys = vi.spyOn(api.stores, "keys").mockResolvedValue([]);
    wrap(<StoresPage />);

    await screen.findByText("bank-platform");
    await waitFor(() => expect(within(rowOf("payments")).getByText("HashiCorp Vault")).toBeInTheDocument());
    expect(within(rowOf("bank-platform")).getByText("Exradius")).toBeInTheDocument();
    expect(within(rowOf("bank-platform")).getByText("2")).toBeInTheDocument();
    expect(within(rowOf("payments")).getByText("secret/teams/payments")).toBeInTheDocument();
    expect(within(rowOf("payments")).getByText("1")).toBeInTheDocument();
    expect(within(rowOf("bank-platform")).getAllByText("bank")).toHaveLength(2); // allowed, and named by
    // The list already carries the key names: nothing asked again, least of all an unreachable store.
    expect(keys).not.toHaveBeenCalled();
  });

  it("shows an unreachable store as unreachable with its reason, not as a key count", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([DOWN]);
    const keys = vi.spyOn(api.stores, "keys").mockResolvedValue([]);
    wrap(<StoresPage />);

    const row = rowOf(await screen.findByText("ledger").then((e) => e.textContent!));
    const chip = within(row).getByText("unreachable");
    expect(chip.closest("[title]")).toHaveAttribute("title", UNREACHABLE_REASON);
    expect(within(row).queryByText("0")).toBeNull();
    expect(keys).not.toHaveBeenCalled();
  });

  it("creates an internal store with the projects ticked", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([]);
    const create = vi.spyOn(api.stores, "create").mockResolvedValue(store({ id: "new", name: "shared" }));
    wrap(<StoresPage />);
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole("button", { name: "New store" }))[0]);
    const submit = await screen.findByRole("button", { name: "Create store" });
    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText("Name"), "shared");
    // Internal is the default, with no settings to fill.
    expect(await screen.findByRole("combobox", { name: "Values kept in" })).toHaveTextContent("Exradius");
    expect(screen.queryByLabelText(/^Path/)).toBeNull();
    // A new store allows no project unless ticked.
    expect(screen.getByRole("checkbox", { name: "Allow bank" })).not.toBeChecked();
    await user.click(screen.getByRole("checkbox", { name: "Allow bank" }));
    await user.click(submit);

    expect(create).toHaveBeenCalledWith({ name: "shared", kind: "internal", settings: {}, allowed_projects: ["bank"] });
    await waitFor(() => expect(navigate).toHaveBeenCalledWith({ to: "/stores/$storeId", params: { storeId: "new" } }));
  });

  it("creates a vault store with its settings, the path required and a blank mount left to its default", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([]);
    const create = vi.spyOn(api.stores, "create").mockResolvedValue(store({ id: "new", name: "payments", kind: "vault" }));
    wrap(<StoresPage />);
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole("button", { name: "New store" }))[0]);
    await user.type(await screen.findByLabelText("Name"), "payments");
    await user.click(await screen.findByRole("combobox", { name: "Values kept in" }));
    await user.click(await screen.findByRole("option", { name: /HashiCorp Vault/ }));

    const path = await screen.findByLabelText("Path");
    const mount = screen.getByLabelText(/^KV mount/);
    expect(mount).toHaveAttribute("placeholder", "secret");
    const submit = screen.getByRole("button", { name: "Create store" });
    expect(submit).toBeDisabled();
    await user.type(path, "teams/payments");
    expect(submit).toBeEnabled();
    await user.click(submit);

    expect(create).toHaveBeenCalledWith({ name: "payments", kind: "vault", settings: { path: "teams/payments" }, allowed_projects: [] });
  });

  it("offers a kind this install cannot use as disabled, with the reason", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([]);
    vi.spyOn(api.stores, "kinds").mockResolvedValue([KINDS[0], { ...KINDS[1], available: false, reason: "Vault has no address in this install." }]);
    wrap(<StoresPage />);
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole("button", { name: "New store" }))[0]);
    await user.click(await screen.findByRole("combobox", { name: "Values kept in" }));
    const vault = await screen.findByRole("option", { name: /HashiCorp Vault/ });
    expect(vault).toHaveAttribute("aria-disabled", "true");
    expect(vault).toHaveTextContent("Vault has no address in this install.");
  });
});

describe("store page", () => {
  it("offers to set, replace and delete an internal store's keys", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([INTERNAL]);
    vi.spyOn(api.stores, "keys").mockResolvedValue([{ key: "DB_PASSWORD", updated_at: T }, { key: "GITHUB_TOKEN", updated_at: T }]);
    wrap(<StoreDetailPage />);

    expect(await screen.findByText("DB_PASSWORD")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add key" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Paste .env" })).toBeInTheDocument();
    expect(screen.getAllByRole("button", { name: "Replace" })).toHaveLength(2);
    expect(screen.getByRole("button", { name: "Delete DB_PASSWORD" })).toBeInTheDocument();
    expect(screen.getByText("Values are write-only.")).toBeInTheDocument();
  });

  it("shows an external store's keys read-only, with where they are", async () => {
    params.storeId = "s2";
    vi.spyOn(api.stores, "list").mockResolvedValue([VAULT]);
    vi.spyOn(api.stores, "keys").mockResolvedValue([{ key: "STRIPE_KEY" }]);
    const setKey = vi.spyOn(api.stores, "setKey");
    wrap(<StoreDetailPage />);

    expect(await screen.findByText("STRIPE_KEY")).toBeInTheDocument();
    expect(screen.getAllByText("HashiCorp Vault").length).toBeGreaterThan(0);
    expect(screen.getAllByText("secret/teams/payments").length).toBeGreaterThan(0);
    expect(screen.getByText(/Set and delete values there; Exradius never stores them\./)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add key" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Paste .env" })).toBeNull();
    expect(screen.queryByRole("button", { name: /^Replace|^Set value/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Delete STRIPE_KEY" })).toBeNull();
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(setKey).not.toHaveBeenCalled();
  });

  it("shows an unreachable store's reason and no key list, and does not ask it for keys", async () => {
    params.storeId = "s3";
    vi.spyOn(api.stores, "list").mockResolvedValue([DOWN]);
    vi.spyOn(api.projects, "list").mockResolvedValue([{ ...HAPPY, store: "ledger", store_allowed: true }]);
    const keys = vi.spyOn(api.stores, "keys").mockResolvedValue([]);
    wrap(<StoreDetailPage />, undefined, [DOWN]);

    const alert = await screen.findByRole("alert");
    await waitFor(() => expect(alert).toHaveTextContent("HashiCorp Vault cannot be reached"));
    expect(alert).toHaveTextContent(UNREACHABLE_REASON);
    expect(screen.getByText("Key names are unknown while HashiCorp Vault cannot be reached.")).toBeInTheDocument();
    expect(screen.queryByText("No keys")).toBeNull();
    expect(keys).not.toHaveBeenCalled();
  });

  it("calls an allowed project's keys unknown, not missing, while the store cannot be reached", async () => {
    params.storeId = "s3";
    vi.spyOn(api.stores, "list").mockResolvedValue([{ ...DOWN, allowed_projects: ["bank"], used_by: ["bank"] }]);
    vi.spyOn(api.projects, "list").mockResolvedValue([{ ...HAPPY, store: "ledger" }]);
    vi.spyOn(api.stores, "keys").mockResolvedValue([]);
    wrap(<StoreDetailPage />);

    expect(await screen.findByText("keys unknown")).toBeInTheDocument();
    expect(screen.queryByText(/^missing/)).toBeNull();
  });

  it("treats a key list the backend refuses with 503 as unreachable", async () => {
    params.storeId = "s2";
    vi.spyOn(api.stores, "list").mockResolvedValue([VAULT]);
    // As request() throws it: the body's `detail`.
    vi.spyOn(api.stores, "keys").mockRejectedValue(new ApiError(503, UNREACHABLE_REASON));
    wrap(<StoreDetailPage />);

    expect(await screen.findByText("Key names are unknown while HashiCorp Vault cannot be reached.")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(UNREACHABLE_REASON);
  });

  it("allows a project from the list of known projects, after confirming", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([INTERNAL]);
    vi.spyOn(api.stores, "keys").mockResolvedValue([]);
    const update = vi.spyOn(api.stores, "update").mockResolvedValue(INTERNAL);
    const confirm = wrap(<StoreDetailPage />);
    const user = userEvent.setup();

    await user.click(await screen.findByRole("combobox", { name: "Project to allow" }));
    // Only projects not yet allowed are offered.
    expect((await screen.findAllByRole("option")).map((o) => o.textContent)).toEqual(["billing"]);
    await user.click(screen.getByRole("option", { name: "billing" }));
    await user.click(screen.getByRole("button", { name: "Allow" }));

    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ title: "Allow billing to use bank-platform?" }));
    expect(update).toHaveBeenCalledWith("s1", { allowed_projects: ["bank", "billing"] });
  });

  it("allows a project that names the store without being allowed", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([store({ allowed_projects: [], used_by: ["bank"] })]);
    vi.spyOn(api.stores, "keys").mockResolvedValue([]);
    const update = vi.spyOn(api.stores, "update").mockResolvedValue(INTERNAL);
    wrap(<StoreDetailPage />);
    const user = userEvent.setup();

    expect(await screen.findByText("not allowed")).toBeInTheDocument();
    expect(screen.getByText("No project may use this store.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Allow bank" }));
    expect(update).toHaveBeenCalledWith("s1", { allowed_projects: ["bank"] });
  });

  it("disallows a project after confirming, and not without", async () => {
    vi.spyOn(api.stores, "list").mockResolvedValue([store({ allowed_projects: ["bank", "billing"] })]);
    vi.spyOn(api.stores, "keys").mockResolvedValue([]);
    const update = vi.spyOn(api.stores, "update").mockResolvedValue(INTERNAL);
    const confirm = wrap(<StoreDetailPage />, vi.fn().mockResolvedValueOnce(false).mockResolvedValue(true));
    const user = userEvent.setup();

    await user.click(await screen.findByRole("button", { name: "Remove bank" }));
    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({ title: "Remove bank from bank-platform?" }));
    expect(update).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Remove bank" }));
    expect(update).toHaveBeenCalledWith("s1", { allowed_projects: ["billing"] });
  });

  it("shows the change log, newest first, and never a value", async () => {
    const changes: StoreChange[] = [
      { id: 5, at: T, actor: "admin", action: "settings_changed", key: null, project: null,
        detail: { before: { kind: "internal", settings: {} }, after: { kind: "vault", settings: { mount: "secret", path: "teams/bank" } } } },
      // A detail the log should never carry: the page must not show it either.
      { id: 4, at: T, actor: "admin", action: "key_set", key: "DB_PASSWORD", project: null, detail: { value: "hunter2-value" } },
      { id: 3, at: T, actor: "admin", action: "key_deleted", key: "OLD_TOKEN", project: null, detail: { reason: "store moved", value: "s3cr3t-value" } },
      { id: 2, at: T, actor: "admin", action: "project_allowed", key: null, project: "bank", detail: null },
      { id: 1, at: T, actor: "migration", action: "store_created", key: null, project: null, detail: { kind: "internal" } },
    ];
    vi.spyOn(api.stores, "list").mockResolvedValue([INTERNAL]);
    vi.spyOn(api.stores, "keys").mockResolvedValue([]);
    const list = vi.spyOn(api.stores, "changes").mockResolvedValue(changes);
    wrap(<StoreDetailPage />);

    const set = await screen.findByText((_, el) => el?.tagName === "TD" && el.textContent === "set DB_PASSWORD");
    const log = set.closest("table") as HTMLElement;
    expect(list).toHaveBeenCalledWith("s1");
    const rows = within(log).getAllByRole("row").slice(1).map((r) => r.lastElementChild!.textContent);
    expect(rows).toEqual([
      "moved the store from internal to vault (mount=secret path=teams/bank)",
      "set DB_PASSWORD",
      "deleted OLD_TOKEN",
      "allowed bank",
      "created the store",
    ]);
    expect(within(log).getByText("migration")).toBeInTheDocument();
    expect(document.body).not.toHaveTextContent("hunter2-value");
    expect(document.body).not.toHaveTextContent("s3cr3t-value");
  });
});
