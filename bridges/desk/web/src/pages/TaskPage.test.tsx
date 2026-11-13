import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ME, PEOPLE, mockApi, renderAt } from "@/test/render";
import type { Task } from "@/lib/types";

const NOW = new Date().toISOString();

function waitingTask(): Task {
  return {
    key: "plane-1", project: "plane", project_name: "Plane", title: "Fix invoice totals",
    description: "Negative **since** Monday.", status: "in_progress",
    reporter: { id: 1, name: "Ada Lovelace" }, assignee: { kind: "exradius", id: null, name: "Exradius" },
    handed_over_by: { id: 1, name: "Ada Lovelace" }, created_at: NOW, updated_at: NOW,
    files: [{ id: 5, name: "trace.txt", content_type: "text/plain", size: 2048, uploaded_by: null, created_at: NOW }],
    run: {
      seq: 1, task_id: "plane-1-kqxz", state: "waiting_for_input", agent: "scout", agent_name: "Scout", routing: null,
      created_at: NOW, ended_at: null, handed_over_by: { id: 1, name: "Ada Lovelace" }, cancel_requested_by: null,
    },
    runs: [],
    timeline: [
      { type: "comment", id: 10, kind: "comment", body: "@[person:2] can you check?", data: {},
        author: { kind: "person", id: 1, name: "Ada Lovelace" }, created_at: NOW, run: null, agent_name: null,
        files: [], artifacts: [] },
      { type: "comment", id: 11, kind: "started", body: "", data: { agent_name: "Scout", guess: "a guess: Scout 59%, Developer 41%" },
        author: { kind: "exradius", id: null, name: "Exradius" }, created_at: NOW, run: 1, agent_name: "Scout",
        files: [], artifacts: [] },
      { type: "comment", id: 12, kind: "question", body: "Which region?", data: { context: "Both fail." },
        author: { kind: "exradius", id: null, name: "Exradius" }, created_at: NOW, run: 1, agent_name: "Scout",
        files: [], artifacts: [], request_id: "q-1", answered_by: null },
    ],
    names: { "1": "Ada Lovelace", "2": "Ben Okafor" },
    exradius: "answer",
  };
}

test("a waiting run: the question, Answer, and the live run", async () => {
  const calls = mockApi({
    "GET /api/me": ME, "GET /api/projects/plane/people": PEOPLE, "GET /api/tasks/plane-1": waitingTask(),
    "POST /api/tasks/plane-1/comments": { id: 13, effect: "answered" }, "POST /api/tasks/plane-1/cancel": undefined,
  });
  renderAt("/tasks/plane-1");
  expect(await screen.findByRole("heading", { name: "Fix invoice totals" })).toBeInTheDocument();
  expect(screen.getByText("since").tagName).toBe("STRONG");
  expect(screen.getByText("@Ben Okafor")).toBeInTheDocument();
  expect(screen.getByText("a guess: Scout 59%, Developer 41%")).toBeInTheDocument();
  expect(screen.getByText("Which region?")).toBeInTheDocument();
  expect(screen.getByText("Asks a question")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: /trace.txt/ })).toHaveAttribute("href", "/api/attachments/5");
  await waitFor(() => expect(screen.getByLabelText("Assignee")).toBeDisabled());

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Answer" }));
  const box = screen.getByLabelText("Comment") as HTMLTextAreaElement;
  expect(box.value).toBe("@Exradius ");
  await user.type(box, "eu-west-1");
  expect(screen.getByText("Answers the question")).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Comment" }));
  await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url.endsWith("/comments"))).toBe(true));
  const sent = calls.find((c) => c.url.endsWith("/comments"))!.body as FormData;
  expect(sent.get("body")).toBe("@[exradius] eu-west-1");

  await user.click(screen.getByRole("button", { name: "Cancel run" }));
  await waitFor(() => expect(calls.some((c) => c.url === "/api/tasks/plane-1/cancel")).toBe(true));
});

test("people are offered after @, and a finished result shows its files", async () => {
  const task = waitingTask();
  task.run = null;
  task.exradius = "start";
  task.assignee = { kind: "person", id: 1, name: "Ada Lovelace" };
  task.timeline = [{
    type: "comment", id: 20, kind: "result", body: "Fixed the rounding.",
    data: { state: "completed", error: null, warnings: ["1 commit not pushed"] },
    author: { kind: "exradius", id: null, name: "Exradius" }, created_at: NOW, run: 1, agent_name: "Scout",
    files: [], artifacts: [{ id: 7, name: "report.md", content_type: "text/markdown" }],
  }];
  mockApi({ "GET /api/me": ME, "GET /api/projects/plane/people": PEOPLE, "GET /api/tasks/plane-1": task });
  renderAt("/tasks/plane-1");
  expect(await screen.findByText("finished")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "report.md" })).toHaveAttribute("href", "/api/artifacts/7");
  expect(screen.getByText("1 commit not pushed")).toBeInTheDocument();
  await waitFor(() => expect(screen.getByLabelText("Assignee")).not.toBeDisabled());

  const user = userEvent.setup();
  const box = screen.getByLabelText("Comment");
  await user.type(box, "@");
  const options = within(await screen.findByRole("listbox", { name: "Mention" })).getAllByRole("option");
  expect(options.map((o) => o.textContent)).toEqual(["ExExradius", "ALAda Lovelace", "BOBen Okafor"]);
  await user.type(box, "Be{Enter}");
  expect((box as HTMLTextAreaElement).value).toBe("@Ben Okafor ");
});
