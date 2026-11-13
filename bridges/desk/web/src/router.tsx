import type { QueryClient } from "@tanstack/react-query";
import { createRootRoute, createRoute, createRouter, redirect } from "@tanstack/react-router";
import { Shell } from "@/components/Shell";
import { NewTask } from "@/pages/NewTask";
import { SignIn } from "@/pages/SignIn";
import { TaskList } from "@/pages/TaskList";
import { TaskPage } from "@/pages/TaskPage";
import type { TaskFilters } from "@/lib/api";

const text = (value: unknown) => (typeof value === "string" && value ? value : undefined);

export function makeRouter(_queryClient: QueryClient) {
  const root = createRootRoute();

  const signin = createRoute({
    getParentRoute: () => root,
    path: "/signin",
    validateSearch: (s: Record<string, unknown>): { error?: string; next?: string } => ({ error: text(s.error), next: text(s.next) }),
    component: function SignInRoute() {
      const { error, next } = signin.useSearch();
      return <SignIn error={error} next={next} />;
    },
  });

  const shell = createRoute({ getParentRoute: () => root, id: "shell", component: Shell });

  const index = createRoute({
    getParentRoute: () => shell,
    path: "/",
    beforeLoad: () => {
      throw redirect({ to: "/tasks" });
    },
  });

  const tasks = createRoute({
    getParentRoute: () => shell,
    path: "/tasks",
    validateSearch: (s: Record<string, unknown>): TaskFilters => ({
      project: text(s.project), assignee: text(s.assignee), status: text(s.status),
    }),
    component: function TasksRoute() {
      return <TaskList filters={tasks.useSearch()} />;
    },
  });

  const newTask = createRoute({
    getParentRoute: () => shell,
    path: "/tasks/new",
    validateSearch: (s: Record<string, unknown>): { project?: string } => ({ project: text(s.project) }),
    component: function NewTaskRoute() {
      return <NewTask project={newTask.useSearch().project} />;
    },
  });

  const task = createRoute({
    getParentRoute: () => shell,
    path: "/tasks/$key",
    component: function TaskRoute() {
      const { key } = task.useParams();
      return <TaskPage key={key} taskKey={key} />;
    },
  });

  const tree = root.addChildren([signin, shell.addChildren([index, tasks, newTask, task])]);
  return createRouter({ routeTree: tree, defaultPreload: "intent", scrollRestoration: true });
}

declare module "@tanstack/react-router" {
  interface Register {
    router: ReturnType<typeof makeRouter>;
  }
}
