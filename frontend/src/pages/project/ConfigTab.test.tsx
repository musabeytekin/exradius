import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { HAPPY, MISSING_KEYS, UNREADABLE, WITH_ERRORS } from "@/test/projectFixtures";
import type { CatalogEntry } from "@/lib/types";
import { ConfigView } from "./ConfigTab";

const CATALOG: CatalogEntry[] = [{
  key: "github", name: "GitHub", transport: "stdio", command: "npx", args: ["-y", "@modelcontextprotocol/server-github"],
  config: [{ key: "token", label: "GitHub token", required: true, target: "env", name: "GITHUB_PERSONAL_ACCESS_TOKEN" }],
}];
const wrap = (ui: ReactNode) => render(<TooltipPrimitive.Provider>{ui}</TooltipPrimitive.Provider>);
const section = (title: string) => screen.getByRole("heading", { name: new RegExp(`^${title}`) }).closest(".rounded-lg") as HTMLElement;

describe("config view", () => {
  it("renders every section of a clean snapshot read-only", async () => {
    const onReread = vi.fn();
    wrap(<ConfigView project={HAPPY} catalog={CATALOG} onReread={onReread} />);

    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByText("ok")).toBeInTheDocument();
    expect(screen.getByTitle(HAPPY.commit!)).toHaveTextContent("3f9c2a1");

    const repos = section("Repositories");
    expect(within(repos).getByText("bank-of-anthos")).toBeInTheDocument();
    expect(within(repos).getByText("push")).toBeInTheDocument();
    expect(within(repos).getByText("remote default")).toBeInTheDocument();
    expect(within(repos).getByText("$GITHUB_TOKEN")).toBeInTheDocument();

    const ws = section("Workstation");
    expect(ws).toHaveTextContent("built from docker/workstation.Dockerfile with context docker/");
    expect(within(ws).getByText("cp -r /opt/m2 ~/.m2")).toBeInTheDocument();

    const services = section("Services");
    expect(within(services).getByText("postgres:16")).toBeInTheDocument();
    expect(within(services).getByText("5432")).toBeInTheDocument();
    expect(within(services).getByText("pg_isready -U app")).toBeInTheDocument();
    expect(services).toHaveTextContent("POSTGRES_PASSWORD=$DB_PASSWORD");

    expect(section("Environment")).toHaveTextContent("DATABASE_URL=postgres://app:$DB_PASSWORD@postgres:5432/app");

    const conns = section("Connections");
    expect(within(conns).getByText("GitHub")).toBeInTheDocument();
    expect(conns).toHaveTextContent("token=$GITHUB_TOKEN");
    expect(conns).toHaveTextContent("npx -y @acme/docs-mcp");
    expect(within(conns).getByText("https://mcp.example.com/mcp")).toBeInTheDocument();

    const agents = section("Agents");
    expect(within(agents).getByText("developer")).toBeInTheDocument();
    expect(within(agents).getByText("claude-sonnet-5-5")).toBeInTheDocument();
    expect(within(agents).getByText("Developer <dev-bot@acme.com>")).toBeInTheDocument();
    expect(within(agents).getByText("runtime default")).toBeInTheDocument();
    // Roles stay collapsed until opened.
    expect(screen.queryByText("You are a software developer on this team.")).toBeNull();
    const user = userEvent.setup();
    await user.click(within(agents).getAllByRole("button", { name: "Role" })[0]);
    expect(screen.getByText("You are a software developer on this team.")).toBeInTheDocument();

    expect(section("Notes")).toHaveTextContent("uv run pytest");

    // The raw file is collapsed until opened.
    expect(screen.queryByLabelText("Config file")).toBeNull();
    await user.click(screen.getByRole("button", { name: "exradius.yaml" }));
    expect(screen.getByLabelText("Config file").textContent).toBe(HAPPY.raw);

    await user.click(screen.getByRole("button", { name: "Re-read" }));
    expect(onReread).toHaveBeenCalledOnce();
  });

  it("puts errors and warnings on top and renders no sections without a config", () => {
    wrap(<ConfigView project={WITH_ERRORS} />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("2 errors");
    expect(alert).toHaveTextContent("services.postgres.imag: unknown key");
    expect(screen.getByText("1 warning")).toBeInTheDocument();
    expect(screen.getByText(/requires npx/)).toBeInTheDocument();
    expect(screen.getByText("2 config errors")).toBeInTheDocument();
    expect(screen.getByText("Nothing to show until the file reads cleanly.")).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /^Repositories/ })).toBeNull();
    expect(screen.getByText(".exradius/exradius.yaml")).toBeInTheDocument();
  });

  it("shows missing secrets in the health chip while rendering the config", () => {
    wrap(<ConfigView project={MISSING_KEYS} />);
    expect(screen.getByText("1 missing secret")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(section("Agents")).toBeInTheDocument();
  });

  it("explains an unreadable repository instead of failing", () => {
    wrap(<ConfigView project={UNREADABLE} />);
    expect(screen.getByRole("alert")).toHaveTextContent("repository 'https://github.com/acme/missing.git/' not found");
    expect(screen.getByText("unreadable")).toBeInTheDocument();
    expect(screen.getByText("Nothing to show until the file reads cleanly.")).toBeInTheDocument();
    expect(screen.getByText("not read")).toBeInTheDocument();
  });

  it("shows declared memory, caches with their size and service commands", () => {
    const config = HAPPY.config!;
    const project = {
      ...HAPPY,
      cache_bytes: 3 * 1024 * 1024,
      config: {
        ...config,
        resources: { memory: "6g" },
        workstation: { ...config.workstation, cache: ["~/.local/share/pnpm/store", "~/.cache/uv"] },
        services: { minio: { image: "minio/minio", command: ["server", "/data"], memory: "512m" } },
      },
    };
    wrap(<ConfigView project={project} catalog={CATALOG} />);
    const ws = section("Workstation");
    expect(within(ws).getByText("6g")).toBeInTheDocument();
    expect(within(ws).getByText("~/.cache/uv")).toBeInTheDocument();
    expect(ws).toHaveTextContent("on disk");
    const services = section("Services");
    expect(within(services).getByText("server /data")).toBeInTheDocument();
    expect(within(services).getByText("512m")).toBeInTheDocument();
  });

  it("shows the config hook with its last delivery, and replaces it", async () => {
    const onReplaceHook = vi.fn();
    const url = "https://exradius.example.com/api/hooks/config/Zm9vYmFy";
    const { rerender } = wrap(<ConfigView project={{ ...HAPPY, hook_url: url, hook_last_at: null }} onReplaceHook={onReplaceHook} />);
    expect(screen.getByTitle(url)).toHaveTextContent(url);
    expect(screen.getByText("Last delivery").nextSibling).toHaveTextContent("never");
    await userEvent.setup().click(screen.getByRole("button", { name: "Replace" }));
    expect(onReplaceHook).toHaveBeenCalledOnce();

    rerender(<TooltipPrimitive.Provider><ConfigView project={{ ...HAPPY, hook_url: url, hook_last_at: new Date(Date.now() - 120_000).toISOString() }} /></TooltipPrimitive.Provider>);
    expect(screen.getByText("Last delivery").nextSibling).toHaveTextContent("2m ago");
    expect(screen.queryByRole("button", { name: "Replace" })).toBeNull();
  });

  it("says when nothing is declared", () => {
    wrap(<ConfigView project={HAPPY} catalog={CATALOG} />);
    const ws = section("Workstation");
    expect(within(ws).getByText("not declared")).toBeInTheDocument();
  });
});
