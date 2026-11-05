import type { QueryClient } from "@tanstack/react-query";
import { createRootRouteWithContext, createRoute, createRouter, Outlet, redirect } from "@tanstack/react-router";
import { AppShell } from "@/components/AppShell";
import { api, ApiError } from "@/lib/api";
import { qk } from "@/lib/queries";
import { AgentsPage } from "@/pages/Agents";
import { ConnectionsPage } from "@/pages/Connections";
import { IntegrationPage } from "@/pages/Integration";
import { LoginPage } from "@/pages/Login";
import { NotFound } from "@/pages/NotFound";
import { OverviewPage } from "@/pages/Overview";
import { ProjectDetailPage } from "@/pages/ProjectDetail";
import { ProjectsPage } from "@/pages/Projects";
import { ProvidersPage } from "@/pages/Providers";
import { SecretsPage } from "@/pages/Secrets";
import { SettingsPage } from "@/pages/Settings";
import { TaskDetailPage } from "@/pages/TaskDetail";
import { TasksPage } from "@/pages/Tasks";

interface Ctx {
  queryClient: QueryClient;
}

export interface TasksSearch { state?: string; project?: string; agent?: string; send?: string; page?: number }

const str = (v: unknown) => (typeof v === "string" && v ? v : undefined);

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

export const PROJECT_TABS = ["repositories", "environment", "secrets", "agents", "verify"] as const;
export type ProjectTab = (typeof PROJECT_TABS)[number];

const projectRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/projects/$projectId",
  validateSearch: (s: Record<string, unknown>): { tab?: ProjectTab } => ({
    tab: PROJECT_TABS.includes(s.tab as ProjectTab) ? (s.tab as ProjectTab) : undefined,
  }),
  component: ProjectDetailPage,
});

const connectionsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/connections",
  validateSearch: (s: Record<string, unknown>): { edit?: string; create?: string } => ({ edit: str(s.edit), create: str(s.create) }),
  component: ConnectionsPage,
});

const providersRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/providers",
  validateSearch: (s: Record<string, unknown>): { edit?: string; create?: string } => ({ edit: str(s.edit), create: str(s.create) }),
  component: ProvidersPage,
});

const agentsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/agents",
  validateSearch: (s: Record<string, unknown>): { edit?: string; create?: string } => ({ edit: str(s.edit), create: str(s.create) }),
  component: AgentsPage,
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

const integrationRoute = createRoute({ getParentRoute: () => appRoute, path: "/integration", component: IntegrationPage });

const secretsRoute = createRoute({
  getParentRoute: () => appRoute,
  path: "/secrets",
  validateSearch: (s: Record<string, unknown>): { create?: string } => ({ create: str(s.create) }),
  component: SecretsPage,
});

const settingsRoute = createRoute({ getParentRoute: () => appRoute, path: "/settings", component: SettingsPage });

const routeTree = rootRoute.addChildren([
  loginRoute,
  appRoute.addChildren([
    overviewRoute, projectsRoute, projectRoute, connectionsRoute, providersRoute, agentsRoute,
    tasksRoute, taskRoute, integrationRoute, secretsRoute, settingsRoute,
  ]),
]);

export function makeRouter(queryClient: QueryClient) {
  return createRouter({
    routeTree,
    context: { queryClient },
    defaultPreload: "intent",
    scrollRestoration: true,
  });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof makeRouter>;
  }
}
