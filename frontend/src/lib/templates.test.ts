/// <reference types="node" />
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { OPERATING_RULES, TEMPLATES } from "./templates";

describe("starter templates", () => {
  it("describe a role only and leave the operating rules to Exradius", () => {
    for (const t of TEMPLATES) {
      expect(t.prompt, t.key).not.toMatch(/request_information|submit_result/);
    }
  });

  // The guide shows the rules verbatim; keep them identical to what the runner injects.
  // Skipped when the backend is not next to the frontend (the frontend image builds alone).
  // Vitest runs from frontend/; jsdom gives import.meta.url an http scheme, so resolve from there.
  const runner = resolve(process.cwd(), "../backend/exradius/runner/prompt.py");
  it.skipIf(!existsSync(runner))("show the same operating rules as the runner", () => {
    const py = readFileSync(runner, "utf8");
    const m = /OPERATING_RULES = """\\\n([\s\S]*?)"""/.exec(py);
    expect(m, "OPERATING_RULES in prompt.py").not.toBeNull();
    expect(OPERATING_RULES).toBe(m![1]);
  });
});
