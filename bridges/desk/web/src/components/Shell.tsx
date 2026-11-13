import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, Outlet, useNavigate, useRouterState } from "@tanstack/react-router";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { useLive } from "@/lib/live";
import { qk } from "@/lib/queries";
import { buttonClass } from "@/lib/styles";

export function Logo() {
  return (
    <svg width="22" height="22" viewBox="0 0 32 32" aria-hidden>
      <rect width="32" height="32" rx="8" fill="var(--accent)" />
      <path d="M9 16h14M16 9v14" stroke="var(--accent-fg)" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

export function Shell() {
  const me = useQuery({ queryKey: qk.me, queryFn: api.me, staleTime: 60_000 });
  const path = useRouterState({ select: (s) => s.location.pathname });
  const navigate = useNavigate();
  const qc = useQueryClient();
  useLive(me.isSuccess);

  async function signOut() {
    await api.logout().catch(() => undefined);
    qc.clear();
    navigate({ to: "/signin" });
  }

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-30 border-b border-border bg-surface/95 backdrop-blur">
        <div className="mx-auto flex h-14 max-w-5xl items-center gap-3 px-4">
          <Link to="/tasks" className="flex items-center gap-2">
            <Logo />
            <span className="text-[14px] font-semibold tracking-[-0.01em]">Desk</span>
          </Link>
          <nav className="flex items-center gap-1" aria-label="Main">
            <Link to="/tasks" className={cn("rounded-md px-2.5 py-1.5 text-sm font-medium",
              path === "/tasks" ? "bg-surface-2 text-fg" : "text-muted hover:text-fg")}>Tasks</Link>
          </nav>
          <div className="ml-auto flex items-center gap-2">
            <Link to="/tasks/new" className={buttonClass("primary", true)}>New task</Link>
            {me.data && (
              <button type="button" onClick={signOut} title={`Signed in as ${me.data.name}`}
                className="hidden text-sm text-muted hover:text-fg sm:inline">
                {me.data.name} · Sign out
              </button>
            )}
            {me.data && (
              <button type="button" onClick={signOut} className="text-sm text-muted hover:text-fg sm:hidden">Sign out</button>
            )}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-5xl px-4 py-5 safe-bottom">
        <Outlet />
      </main>
    </div>
  );
}
