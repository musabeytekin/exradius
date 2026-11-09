import { describe, expect, it } from "vitest";
import { waitingLabel } from "./states";

describe("waitingLabel", () => {
  it("names what a preparing run waits for", () => {
    expect(waitingLabel("preparing", "capacity")).toBe("waiting for cluster capacity");
    expect(waitingLabel("preparing", "build")).toBe("building images");
  });
  it("says nothing otherwise", () => {
    expect(waitingLabel("preparing", null)).toBeNull();
    expect(waitingLabel("running", "build")).toBeNull();
    expect(waitingLabel("preparing", "something-new")).toBeNull();
  });
});
