import type { QueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, createRoute, createRouter, Outlet, redirect } from "@tanstack/react-router";
import { AppShell } from "@/components/AppShell";
import { RouteError } from "@/components/ApiStatus";
import { api, ApiError } from "@/lib/api";
import { qk } from "@/lib/queries";
import { LoginPage } from "@/pages/Login";
import { NotFound } from "@/pages/NotFound";
import { OverviewPage } from "@/pages/Overview";
import { ProjectDetailPage } from "@/pages/ProjectDetail";
import { ProjectsPage } from "@/pages/Projects";
import { ProvidersPage } from "@/pages/Providers";
import { SettingsPage } from "@/pages/Settings";
import { StoreDetailPage, StoresPage } from "@/pages/Stores";
import { TaskDetailPage } from "@/pages/TaskDetail";
import { TasksPage } from "@/pages/Tasks";

interface Ctx {
  queryClient: QueryClient;
}

export interface TasksSearch { state?: string; project?: string; agent?: string; send?: string; page?: number }

// Hand-written links like `?create=1` arrive parsed as numbers or booleans.
const str = (v: unknown) =>
  typeof v === "string" ? v || undefined : typeof v === "number" || typeof v === "boolean" ? String(v) : undefined;

const rootRoute = createRootRouteWithContext<Ctx>()({
  component: () => <Outlet />,
  notFoundComponent: NotFound,
});

const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: "/login",
  validateSearch: (s: Record<string, unknown>): { next?: string } => ({ next: str(s.next) }),
  component: LoginPage,
});

const appRoute = createRoute({
  getParentRoute: () => rootRoute,
  id: "app",
  beforeLoad: async ({ context, location }) => {
    try {
      await context.queryClient.ensureQueryData({ queryKey: qk.me, queryFn: api.auth.me, staleTime: 60_000 });
    } catch (e) {
      if (e instanceof ApiError && e.status === 401) {
        throw redirect({ to: "/login", search: { next: location.href === "/" ? undefined : location.href } });
      }
      throw e;
    }
  },
  component: AppShell,
});

const overviewRoute = createRoute({ getParentRoute: () => appRoute, path: "/", component: OverviewPage });

const projectsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/projects",
  validateSearch: (s: Record<string, unknown>): { create?: string } => ({ create: str(s.create) }),
  component: ProjectsPage,
});

export const PROJECT_TABS = ["config", "secrets", "verify", "tasks"] as const;
export type ProjectTab = (typeof PROJECT_TABS)[number];

const projectRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/projects/$projectId",
  validateSearch: (s: Record<string, unknown>): { tab?: ProjectTab } => ({
    tab: PROJECT_TABS.includes(s.tab as ProjectTab) ? (s.tab as ProjectTab) : undefined,
  }),
  component: ProjectDetailPage,
});

const providersRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/providers",
  validateSearch: (s: Record<string, unknown>): { edit?: string; create?: string } => ({ edit: str(s.edit), create: str(s.create) }),
  component: ProvidersPage,
});

const tasksRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/tasks",
  validateSearch: (s: Record<string, unknown>): TasksSearch => ({
    state: str(s.state), project: str(s.project), agent: str(s.agent), send: str(s.send),
    page: typeof s.page === "number" && s.page > 0 ? s.page : undefined,
  }),
  component: TasksPage,
});

const taskRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/tasks/$taskId",
  component: TaskDetailPage,
});

const storesRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/stores",
  validateSearch: (s: Record<string, unknown>): { create?: string } => ({ create: str(s.create) }),
  component: StoresPage,
});

const storeRoute = createRoute({ getParentRoute: () => appRoute, path: "/stores/$storeId", component: StoreDetailPage });

const settingsRoute = createRoute({ getParentRoute: () => appRoute, path: "/settings", component: SettingsPage });

const routeTree = rootRoute.addChildren([
  loginRoute,
  appRoute.addChildren([
    overviewRoute, projectsRoute, projectRoute, storesRoute, storeRoute, providersRoute,
    tasksRoute, taskRoute, settingsRoute,
  ]),
]);

export function makeRouter(queryClient: QueryClient) {
  return createRouter({
    routeTree,
    context: { queryClient },
    defaultPreload: "intent",
    defaultErrorComponent: RouteError,
    scrollRestoration: true,
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof makeRouter>;
  }
}
