import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { FreshnessChip } from "@/components/FreshnessChip";
import type { Verification, VerificationStep } from "@/lib/types";
import { VerificationView } from "./VerifyTab";

const T = "2026-11-07T10:00:00Z";
const step = (name: string, state: VerificationStep["state"], log = ""): VerificationStep =>
  ({ name, state, started_at: state === "pending" || state === "skipped" ? null : T, ended_at: state === "running" ? null : T, log });
const verification = (v: Partial<Verification>): Verification => ({
  id: "v1", project_id: "p1", state: "passed", created_at: T, started_at: T, ended_at: T, error: null,
  steps: [], commit: "1a2b3c4d5e6f7a8b", diagnosis: null, ...v,
});

describe("verification view", () => {
  it("leads a failure with the step, the diagnosis, the command and the tail", async () => {
    render(<VerificationView v={verification({
      state: "failed",
      error: "Setup: corepack enable: exited with 127",
      diagnosis: "The workstation image has no `corepack`.",
      steps: [step("Read config", "passed", "commit 1a2b3c4d5e6f"), step("Setup: corepack enable", "failed", "exited with 127\n/bin/sh: 1: corepack: not found"),
              step("Setup: yarn test", "skipped"), step("Remove directory", "passed", "removed")],
    })} />);
    expect(screen.getByText("The workstation image has no `corepack`.")).toBeInTheDocument();
    expect(screen.getByText("$ corepack enable")).toBeInTheDocument();
    expect(screen.getByText(/corepack: not found/)).toBeInTheDocument();
    expect(screen.getByText("1a2b3c4")).toBeInTheDocument();
    expect(screen.queryByText("Read config")).toBeNull(); // the other steps are collapsed
    await userEvent.setup().click(screen.getByRole("button", { name: /4 steps/ }));
    expect(screen.getByText("Read config")).toBeInTheDocument();
    expect(screen.getByText("Setup: yarn test")).toBeInTheDocument();
  });

  it("shows a pass as one line with the steps collapsed", () => {
    render(<VerificationView v={verification({ steps: [step("Read config", "passed", "commit x")] })} />);
    expect(screen.getByText("passed")).toBeInTheDocument();
    expect(screen.getByText("1a2b3c4")).toBeInTheDocument();
    expect(screen.queryByText("Read config")).toBeNull();
  });

  it("shows the current step of a running verification", () => {
    render(<VerificationView v={verification({
      state: "running", ended_at: null,
      steps: [step("Read config", "passed"), step("Build workstation", "running", "Step 3/6 : RUN make"), step("Remove directory", "pending")],
    })} />);
    expect(screen.getByText("Build workstation")).toBeInTheDocument();
    expect(screen.getByText("Step 3/6 : RUN make")).toBeInTheDocument();
  });

  it("shows an error without a failed step", () => {
    render(<VerificationView v={verification({ state: "failed", error: "broker: down", commit: null })} />);
    expect(screen.getByText("broker: down")).toBeInTheDocument();
  });
});

describe("freshness chip", () => {
  it("is one word with the short commit", () => {
    const { rerender } = render(<FreshnessChip project={{ freshness: "stale", verification: { id: "v", state: "passed", commit: "abcdef123456", ended_at: T, diagnosis: null } }} />);
    expect(screen.getByText("stale")).toBeInTheDocument();
    expect(screen.getByText("abcdef1")).toBeInTheDocument();
    rerender(<FreshnessChip project={{ freshness: null, verification: null }} />);
    expect(screen.getByText("never")).toBeInTheDocument();
  });
});
