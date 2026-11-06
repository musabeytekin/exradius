import { describe, expect, it } from "vitest";
import { TEMPLATES } from "./templates";

describe("starter templates", () => {
  it("describe a role only and leave the operating rules to Exradius", () => {
    for (const t of TEMPLATES) {
      expect(t.prompt, t.key).not.toMatch(/request_information|submit_result/);
    }
  });
});
