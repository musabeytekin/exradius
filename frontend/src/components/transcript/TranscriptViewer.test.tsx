import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { buildTranscript, splitToolName, summarizeInput } from "@/lib/transcript";
import type { InfoRequest, TranscriptChunk } from "@/lib/types";
import { TranscriptViewer } from "./TranscriptViewer";

// Chunks as the runner stores them: Claude Agent SDK dataclasses serialised with `type`.
const chunks: TranscriptChunk[] = [
  {
    seq: 1,
    payload: {
      type: "SystemMessage",
      subtype: "init",
      data: { type: "system", subtype: "init", model: "claude-opus-5-5", cwd: "/work", tools: ["Bash", "Read", "mcp__exradius__submit_result"], mcp_servers: [{ name: "exradius", status: "connected" }, { name: "github", status: "failed" }] },
    },
  },
  {
    seq: 2,
    payload: {
      type: "AssistantMessage",
      model: "claude-opus-5-5",
      parent_tool_use_id: null,
      content: [
        { type: "ThinkingBlock", thinking: "The totals go negative after the discount refactor.", signature: "sig" },
        { type: "TextBlock", text: "I'll look at how **invoice totals** are computed." },
        { type: "ToolUseBlock", id: "toolu_1", name: "Bash", input: { command: "grep -rn 'def total' billing/", description: "Find total()" } },
      ],
    },
  },
  {
    seq: 3,
    payload: {
      type: "UserMessage",
      parent_tool_use_id: null,
      content: [{ type: "ToolResultBlock", tool_use_id: "toolu_1", content: "billing/invoice.py:42:    def total(self):", is_error: false }],
    },
  },
  {
    seq: 4,
    payload: {
      type: "AssistantMessage",
      model: "claude-opus-5-5",
      parent_tool_use_id: null,
      content: [
        { type: "ToolUseBlock", id: "toolu_2", name: "Read", input: { file_path: "/work/billing/missing.py" } },
        { type: "ToolUseBlock", id: "toolu_3", name: "mcp__github__get_issue", input: { owner: "acme", repo: "billing", issue_number: 4821 } },
        { type: "ToolUseBlock", id: "toolu_4", name: "Task", input: { description: "Search migrations", prompt: "Find the migration that ran on Monday." } },
      ],
    },
  },
  {
    seq: 5,
    payload: {
      type: "UserMessage",
      parent_tool_use_id: null,
      content: [
        { type: "ToolResultBlock", tool_use_id: "toolu_2", content: "File does not exist.", is_error: true },
        { type: "ToolResultBlock", tool_use_id: "toolu_3", content: [{ type: "text", text: '{"title":"Negative totals","state":"open"}' }], is_error: false },
      ],
    },
  },
  // Subagent work, nested under the Task call.
  {
    seq: 6,
    payload: { type: "AssistantMessage", model: "claude-opus-5-5", parent_tool_use_id: "toolu_4", content: [{ type: "ToolUseBlock", id: "toolu_5", name: "Glob", input: { pattern: "migrations/*.sql" } }] },
  },
  {
    seq: 7,
    payload: { type: "UserMessage", parent_tool_use_id: "toolu_4", content: [{ type: "ToolResultBlock", tool_use_id: "toolu_5", content: "migrations/0042_discounts.sql" }] },
  },
  {
    seq: 8,
    payload: {
      type: "AssistantMessage",
      model: "claude-opus-5-5",
      parent_tool_use_id: null,
      content: [{ type: "ToolUseBlock", id: "toolu_6", name: "mcp__exradius__request_information", input: { question: "Did migration 0042 run on Monday?" } }],
    },
  },
  { seq: 9, payload: { type: "UserMessage", parent_tool_use_id: null, content: "Answer to your question: Did migration 0042 run on Monday?\n\nYes, at 03:00 UTC." } },
  {
    seq: 10,
    payload: {
      type: "AssistantMessage",
      model: "claude-opus-5-5",
      parent_tool_use_id: null,
      content: [{ type: "ToolUseBlock", id: "toolu_7", name: "mcp__exradius__submit_result", input: { output: { cause: "discount applied twice", branch: "fix/negative-totals" }, artifacts: ["report.md"] } }],
    },
  },
  {
    seq: 11,
    payload: { type: "UserMessage", parent_tool_use_id: null, content: [{ type: "ToolResultBlock", tool_use_id: "toolu_7", content: [{ type: "text", text: "Result stored." }] }] },
  },
  {
    seq: 12,
    payload: { type: "ResultMessage", subtype: "success", duration_ms: 83_400, duration_api_ms: 60_000, is_error: false, num_turns: 9, session_id: "s1", total_cost_usd: 0.4213, usage: { input_tokens: 1200, output_tokens: 800 }, result: "Fixed." },
  },
];

const infoRequests: InfoRequest[] = [
  { id: "r1", question: "Did migration 0042 run on Monday?", context: null, created_at: "2026-11-05T10:00:00Z", answered_at: "2026-11-05T10:05:00Z", answer: "Yes, at 03:00 UTC.", answer_attachments: [], late_answers: [] },
];

describe("buildTranscript", () => {
  it("pairs tool results with their calls and nests subagent work", () => {
    const items = buildTranscript(chunks);
    const tools = items.filter((i) => i.kind === "tool");
    expect(tools.map((t) => t.kind === "tool" && t.name)).toEqual([
      "Bash", "Read", "mcp__github__get_issue", "Task", "mcp__exradius__request_information", "mcp__exradius__submit_result",
    ]);
    const read = tools[1];
    expect(read.kind === "tool" && read.result?.isError).toBe(true);
    const task = tools[3];
    expect(task.kind === "tool" && task.children.map((c) => c.kind === "tool" && c.name)).toEqual(["Glob"]);
    expect(task.kind === "tool" && task.children[0].kind === "tool" && task.children[0].result?.text).toBe("migrations/0042_discounts.sql");
  });

  it("orders by sequence number whatever the arrival order", () => {
    const shuffled = [...chunks].reverse();
    expect(buildTranscript(shuffled).map((i) => i.key)).toEqual(buildTranscript(chunks).map((i) => i.key));
  });

  it("reads the runner's interrupt after a question as a pause, not as a failure", () => {
    // As stored by a real run: the runner interrupts the session after request_information.
    const items = buildTranscript([
      { seq: 1, payload: { type: "SystemMessage", subtype: "thinking_tokens", data: { estimated_tokens: 14 } } },
      { seq: 2, payload: { type: "UserMessage", parent_tool_use_id: null, content: [{ type: "TextBlock", text: "[Request interrupted by user]" }] } },
      { seq: 3, payload: { type: "ResultMessage", subtype: "error_during_execution", is_error: true, terminal_reason: "aborted_streaming", duration_ms: 1000, num_turns: 3 } },
    ]);
    expect(items).toMatchObject([
      { kind: "system", subtype: "paused" },
      { kind: "result", isError: true, interrupted: true },
    ]);
    render(<TranscriptViewer chunks={[{ seq: 3, payload: items[1].raw }]} />);
    expect(screen.getByText("Session paused for the answer")).toBeInTheDocument();
  });

  it("keeps unknown message types visible instead of dropping them", () => {
    const items = buildTranscript([{ seq: 1, payload: { type: "ConversationResetMessage", reason: "x" } }]);
    expect(items).toMatchObject([{ kind: "unknown", type: "ConversationResetMessage" }]);
  });

  it("summarises a call by its most telling argument", () => {
    expect(summarizeInput({ description: "Find total()", command: "grep -rn total" })).toBe("grep -rn total");
    expect(summarizeInput({ owner: "acme", issue_number: 4821 })).toBe("acme");
    expect(summarizeInput({ n: 3 })).toBe("3");
    expect(splitToolName("mcp__github__get_issue")).toEqual({ server: "github", tool: "get_issue" });
    expect(splitToolName("Bash")).toEqual({ server: null, tool: "Bash" });
  });
});

describe("TranscriptViewer", () => {
  it("reads like a conversation with tool calls collapsed to one line", () => {
    render(<TranscriptViewer chunks={chunks} infoRequests={infoRequests} />);
    const list = screen.getByRole("list", { name: "Transcript" });
    // Assistant prose with inline markdown.
    expect(within(list).getByText("invoice totals").tagName).toBe("STRONG");
    // A collapsed tool row shows the tool and its first argument, not the output.
    const bash = within(list).getByRole("button", { name: /Bash.*grep -rn 'def total' billing\// });
    expect(bash).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText("billing/invoice.py:42:    def total(self):")).not.toBeInTheDocument();
    // MCP tools show their server and tool name.
    expect(within(list).getByRole("button", { name: /github\s*get_issue/ })).toBeInTheDocument();
    // Session header and the final result.
    expect(screen.getByText("Session started")).toBeInTheDocument();
    expect(screen.getByText("claude-opus-5-5")).toBeInTheDocument();
    expect(screen.getByText("Session finished")).toBeInTheDocument();
    expect(screen.getByText("$0.4213")).toBeInTheDocument();
    expect(screen.getByText("1m 23s")).toBeInTheDocument();
  });

  it("expands a tool call to its full input and output", async () => {
    const user = userEvent.setup();
    render(<TranscriptViewer chunks={chunks} />);
    await user.click(screen.getByRole("button", { name: /Bash.*grep -rn/ }));
    expect(screen.getByRole("button", { name: /Bash.*grep -rn/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Command")).toBeInTheDocument();
    expect(screen.getByText("Find total()")).toBeInTheDocument();
    expect(screen.getByText(/^billing\/invoice\.py:42:\s+def total\(self\):$/)).toBeInTheDocument();
  });

  it("renders JSON output as a tree", async () => {
    const user = userEvent.setup();
    render(<TranscriptViewer chunks={chunks} />);
    await user.click(screen.getByRole("button", { name: /github\s*get_issue/ }));
    const value = screen.getByText(/Negative totals/);
    const tree = value.closest('[role="tree"]') as HTMLElement;
    expect(tree).not.toBeNull();
    // Keys and values are separate tree nodes, not one string.
    expect(within(tree).getByText('"title"')).toBeInTheDocument();
    expect(within(tree).getByText(/"open"/)).toBeInTheDocument();
  });

  it("marks failed calls and counts them", () => {
    render(<TranscriptViewer chunks={chunks} />);
    expect(screen.getByRole("button", { name: /Read.*missing\.py/ })).toHaveTextContent("error");
    expect(screen.getByText("1 failed")).toBeInTheDocument();
    expect(screen.getByText("7 tool calls")).toBeInTheDocument();
  });

  it("nests subagent steps under the call that started them", async () => {
    const user = userEvent.setup();
    render(<TranscriptViewer chunks={chunks} />);
    const task = screen.getByRole("button", { name: /Task.*Search migrations/ });
    expect(task).toHaveTextContent("1 steps");
    expect(screen.queryByRole("button", { name: /Glob/ })).not.toBeInTheDocument();
    await user.click(task);
    expect(screen.getByText("Subagent")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Glob.*migrations\/\*\.sql/ })).toBeInTheDocument();
  });

  it("shows questions with their answers, the delivered answer and the submitted result", () => {
    render(<TranscriptViewer chunks={chunks} infoRequests={infoRequests} />);
    expect(screen.getByText("Asked for information")).toBeInTheDocument();
    expect(screen.getAllByText("Did migration 0042 run on Monday?").length).toBeGreaterThan(0);
    expect(screen.getByText("answered")).toBeInTheDocument();
    expect(screen.getByText("Answer delivered · run resumed")).toBeInTheDocument();
    expect(screen.getByText("Submitted a result")).toBeInTheDocument();
    expect(screen.getByText('"fix/negative-totals"')).toBeInTheDocument();
    expect(screen.getByText("report.md")).toBeInTheDocument();
  });

  it("filters by text, including inside tool output and subagents", async () => {
    const user = userEvent.setup();
    render(<TranscriptViewer chunks={chunks} />);
    await user.type(screen.getByRole("textbox", { name: "Filter the transcript" }), "0042_discounts");
    const list = screen.getByRole("list", { name: "Transcript" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(1);
    expect(within(list).getByRole("button", { name: /Task/ })).toBeInTheDocument();
    await user.clear(screen.getByRole("textbox", { name: "Filter the transcript" }));
    await user.type(screen.getByRole("textbox", { name: "Filter the transcript" }), "nothing like this");
    expect(screen.getByText("Nothing matches")).toBeInTheDocument();
  });

  it("hides thinking on request and expands every tool at once", async () => {
    const user = userEvent.setup();
    render(<TranscriptViewer chunks={chunks} />);
    expect(screen.getByText("Thinking")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Hide thinking" }));
    expect(screen.queryByText("Thinking")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Expand tools" }));
    expect(screen.getByRole("button", { name: /Bash.*grep/ })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("button", { name: /Read.*missing/ })).toHaveAttribute("aria-expanded", "true");
    await user.click(screen.getByRole("button", { name: "Collapse tools" }));
    expect(screen.getByRole("button", { name: /Bash.*grep/ })).toHaveAttribute("aria-expanded", "false");
  });

  it("shows calls without a result as running while live", () => {
    const partial = chunks.slice(0, 2);
    const { rerender } = render(<TranscriptViewer chunks={partial} live />);
    expect(screen.getByRole("button", { name: /Bash/ }).querySelector(".animate-spin")).not.toBeNull();
    expect(screen.getByText("Live")).toBeInTheDocument();
    rerender(<TranscriptViewer chunks={partial} live={false} />);
    expect(screen.getByRole("button", { name: /Bash/ })).toHaveTextContent("no result");
  });

  it("has a designed empty state for live and finished runs", () => {
    const { rerender } = render(<TranscriptViewer chunks={[]} live />);
    expect(screen.getByText("Waiting for the agent")).toBeInTheDocument();
    rerender(<TranscriptViewer chunks={[]} />);
    expect(screen.getByText("No transcript")).toBeInTheDocument();
  });
});
