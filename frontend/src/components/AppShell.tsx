import { UnreachableBanner } from "./ApiStatus";
import { workerHealthy } from "@/lib/workers";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { NAV } from "@/lib/nav";
import { qk, useStatus } from "@/lib/queries";
import { useTheme } from "@/lib/theme";
import type { SystemStatus } from "@/lib/types";
import { CommandPalette } from "./CommandPalette";
import { Icon } from "./ui/icons";
import { Menu, Tooltip } from "./ui/overlays";
import { Dot, Kbd } from "./ui/primitives";

export function AppShell() {
  const [paletteOpen, setPaletteOpen] = useState(false);
  const path = useRouterState({ select: (s) => s.location.pathname });
  const status = useStatus();
  const me = useQuery({ queryKey: qk.me, queryFn: api.auth.me, staleTime: 60_000 });
  const waiting = useQuery({
    queryKey: qk.tasks({ state: "waiting_for_input", limit: 100 }),
    queryFn: () => api.tasks.list({ state: "waiting_for_input", limit: 100 }),
    refetchInterval: 20_000,
  });
  const { pref, cycle } = useTheme();
  const navigate = useNavigate();
  const qc = useQueryClient();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const health = systemHealth(status.data);
  const waitingCount = waiting.data?.length ?? 0;

  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 flex h-screen w-[232px] shrink-0 flex-col border-r border-border bg-surface">
        <div className="flex h-14 items-center gap-2.5 px-4">
          <Logo />
          <span className="text-[14px] font-semibold tracking-[-0.01em]">Exradius</span>
          <span className="ml-auto rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[10px] text-subtle">{status.data?.version ?? ""}</span>
        </div>
        <div className="px-3 pb-2">
          <button
            type="button"
            onClick={() => setPaletteOpen(true)}
            className="flex h-8 w-full items-center gap-2 rounded-md border border-border bg-bg px-2.5 text-sm text-subtle transition-colors hover:border-border-strong hover:text-muted"
          >
            <Icon name="search" size={14} />
            <span className="flex-1 text-left">Jump to…</span>
            <Kbd>⌘K</Kbd>
          </button>
        </div>
        <nav className="flex flex-1 flex-col gap-0.5 overflow-y-auto px-3 py-2" aria-label="Main">
          {NAV.map((n) => {
            const active = n.to === "/" ? path === "/" : path === n.to || path.startsWith(`${n.to}/`);
            return (
              <Link
                key={n.to}
                to={n.to}
                className={cn(
                  "group relative flex h-8 items-center gap-2.5 rounded-md px-2.5 text-sm font-medium transition-colors",
                  active ? "bg-surface-2 text-fg" : "text-muted hover:bg-surface-2/60 hover:text-fg",
                )}
              >
                {active && <span className="absolute -left-3 top-1.5 bottom-1.5 w-[3px] rounded-r bg-accent" />}
                <Icon name={n.icon} size={16} className={active ? "text-fg" : "text-subtle group-hover:text-muted"} />
                {n.label}
                {n.to === "/tasks" && waitingCount > 0 && (
                  <Tooltip content={`${waitingCount} waiting for input`}>
                    <span className="ml-auto rounded-full bg-warn-soft px-1.5 text-2xs font-semibold tabular text-warn">{waitingCount}</span>
                  </Tooltip>
                )}
              </Link>
            );
          })}
        </nav>
        <div className="flex flex-col gap-1 border-t border-border p-3">
          <Link to="/" className="flex h-8 items-center gap-2 rounded-md px-2.5 text-xs text-muted hover:bg-surface-2/60 hover:text-fg">
            <Dot tone={health.tone} pulse={health.tone !== "ok"} />
            <span className="truncate">{health.label}</span>
          </Link>
          <div className="flex items-center gap-1">
            <Menu.Root>
              <Menu.Trigger asChild>
                <button type="button" className="flex h-8 min-w-0 flex-1 items-center gap-2 rounded-md px-2.5 text-sm text-muted hover:bg-surface-2/60 hover:text-fg">
                  <span className="flex size-5 items-center justify-center rounded-full bg-accent-soft text-2xs font-semibold uppercase text-accent">
                    {me.data?.username.slice(0, 1) ?? "·"}
                  </span>
                  <span className="truncate">{me.data?.username ?? ""}</span>
                </button>
              </Menu.Trigger>
              <Menu.Content align="start">
                <Menu.Label>Signed in as {me.data?.username}</Menu.Label>
                <Menu.Item icon="settings" onSelect={() => navigate({ to: "/settings" })}>Settings</Menu.Item>
                <Menu.Separator />
                <Menu.Item
                  icon="logout"
                  onSelect={async () => {
                    await api.auth.logout().catch(() => {});
                    qc.clear();
                    navigate({ to: "/login", search: { next: undefined } });
                  }}
                >
                  Log out
                </Menu.Item>
              </Menu.Content>
            </Menu.Root>
            <Tooltip content={`Theme: ${pref}. Click to change.`}>
              <button
                type="button"
                onClick={cycle}
                aria-label={`Theme: ${pref}`}
                className="flex size-8 items-center justify-center rounded-md text-subtle hover:bg-surface-2 hover:text-fg"
              >
                <Icon name={pref === "light" ? "sun" : pref === "dark" ? "moon" : "monitor"} size={15} />
              </button>
            </Tooltip>
          </div>
        </div>
      </aside>
      <main className="min-w-0 flex-1">
        <UnreachableBanner />
        <div className="mx-auto w-full max-w-[1240px] px-8 pb-16 pt-8">
          <Outlet />
        </div>
      </main>
      <CommandPalette open={paletteOpen} onOpenChange={setPaletteOpen} />
    </div>
  );
}

function systemHealth(s: SystemStatus | undefined): { tone: "ok" | "warn" | "err" | "neutral"; label: string } {
  if (!s) return { tone: "neutral", label: "Checking system…" };
  if (!s.database.ok || !s.broker.ok) return { tone: "err", label: !s.database.ok ? "Database unreachable" : "Broker unreachable" };
  if (s.workers.length === 0) return { tone: "err", label: "No live workers" };
  if (s.workers.some((w) => !workerHealthy(w))) return { tone: "warn", label: "Worker needs attention" };
  if (s.operator && !s.operator.alive) return { tone: "err", label: "Operator not running" };
  if (s.provider && !s.provider.configured) return { tone: "warn", label: "No model provider" };
  if (s.provider?.default?.last_test && !s.provider.default.last_test.ok) return { tone: "warn", label: "Provider test failed" };
  const slots = s.workers.reduce((n, w) => n + w.concurrency, 0);
  return { tone: "ok", label: `${s.workers.length} worker${s.workers.length === 1 ? "" : "s"} · ${slots} slots` };
}

export function Logo({ size = 22 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="8" className="fill-fg" />
      <circle cx="16" cy="16" r="8.5" fill="none" stroke="var(--accent)" strokeWidth="2.5" />
      <circle cx="16" cy="16" r="3" fill="var(--accent)" />
      <path d="M16 7.5v-3M16 27.5v-3M7.5 16h-3M27.5 16h-3" stroke="var(--accent)" strokeWidth="2.5" strokeLinecap="round" />
    </svg>
  );
}
