import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ME, PEOPLE, PROJECTS, mockApi, renderAt } from "@/test/render";

const NOW = new Date().toISOString();

test("the list with its filters", async () => {
  const calls = mockApi({
    "GET /api/me": ME, "GET /api/projects": PROJECTS, "GET /api/projects/plane/people": PEOPLE,
    "GET /api/tasks*": [
      { key: "plane-2", project: "plane", title: "Second", status: "todo", updated_at: NOW, run_state: "running",
        assignee: { kind: "exradius", id: null, name: "Exradius" } },
      { key: "plane-1", project: "plane", title: "First", status: "done", updated_at: NOW, run_state: null,
        assignee: { kind: "none", id: null, name: null } },
    ],
  });
  renderAt("/tasks");
  expect(await screen.findByText("Second")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /plane-2/ })).toHaveAttribute("href", "/tasks/plane-2");
  expect(screen.getByText("Running")).toBeInTheDocument();
  expect(screen.getByText("Unassigned", { selector: "span" })).toBeInTheDocument();

  const user = userEvent.setup();
  await user.selectOptions(screen.getByLabelText("Assignee"), "exradius");
  await waitFor(() => expect(calls.some((c) => c.url === "/api/tasks?assignee=exradius")).toBe(true));
  await user.selectOptions(screen.getByLabelText("Project"), "plane");
  await waitFor(() => expect(calls.some((c) => c.url === "/api/tasks?assignee=exradius&project=plane")).toBe(true));
  await user.selectOptions(screen.getByLabelText("Status"), "done");
  await waitFor(() => expect(calls.some((c) => c.url.includes("status=done"))).toBe(true));
});

test("without a session, the sign-in page", async () => {
  mockApi({});
  vi.stubGlobal("fetch", vi.fn(async () => new Response("{}", { status: 401 })));
  renderAt("/signin?error=the%20identity%20provider%20answered%20access_denied");
  expect(await screen.findByRole("alert")).toHaveTextContent("the identity provider answered access_denied");
  expect(screen.getByRole("link", { name: "Sign in" })).toHaveAttribute("href", "/auth/login");
});
