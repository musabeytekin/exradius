import { describe, expect, it } from "vitest";
import { emailError, needsEmail } from "./identity";

describe("agent identity", () => {
  it("flags the placeholder the migration backfills, and nothing on an API without the field", () => {
    expect(needsEmail({ identity_email: "tracer@exradius.invalid" })).toBe(true);
    expect(needsEmail({ identity_email: "Tracer@EXRADIUS.invalid " })).toBe(true);
    expect(needsEmail({ identity_email: "bots@acme.com" })).toBe(false);
    expect(needsEmail({})).toBe(false);
  });

  it("checks the email loosely and rejects the placeholder", () => {
    expect(emailError("")).toBeNull();
    expect(emailError("bots@acme.com")).toBeNull();
    expect(emailError("bots at acme")).toMatch(/Not an email/);
    expect(emailError("Bot <bots@acme.com>")).toMatch(/angle brackets/);
    expect(emailError("tracer@exradius.invalid")).toMatch(/placeholder/);
  });
});
