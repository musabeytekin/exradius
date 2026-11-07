import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { environmentTextOf } from "@/lib/environment";
import { EnvironmentPreview } from "./EnvironmentPreview";

const resolved = {
  environment_text: [
    "# Your environment",
    "",
    "You run unattended in a Linux container built from the image bank-of-anthos-dev:latest.",
    "Each folder in the working directory is a git clone of one repository:",
    "",
    "- bank-of-anthos: branch main, origin https://github.com/musabeytekin/bank-of-anthos.git, pushing allowed",
    "",
    "Services running for this task, reachable by host name from this container:",
    "- postgres (postgres:16) on port 5432",
  ].join("\n"),
};

describe("environment preview", () => {
  it("reads the text from a run or its spec, and ignores its absence", () => {
    expect(environmentTextOf(resolved)).toMatch(/^# Your environment/);
    expect(environmentTextOf({ spec: { environment_text: "# Your environment\nx" } })).toBe("# Your environment\nx");
    expect(environmentTextOf({ connections: [] })).toBeNull();
    expect(environmentTextOf({ environment_text: "   " })).toBeNull();
    expect(environmentTextOf(null)).toBeNull();
  });

  it("is collapsed until opened, then shows the text verbatim in monospace", async () => {
    const user = userEvent.setup();
    render(<EnvironmentPreview text={environmentTextOf(resolved)!} />);
    const toggle = screen.getByRole("button", { name: "What the agent is told about this project" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/postgres \(postgres:16\)/)).toBeNull();
    await user.click(toggle);
    const pre = screen.getByLabelText("What the agent is told about this project");
    expect(pre.tagName).toBe("PRE");
    expect(pre).toHaveClass("font-mono");
    expect(pre.textContent).toBe(resolved.environment_text);
  });
});
