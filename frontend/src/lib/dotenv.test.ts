import { describe, expect, it } from "vitest";
import { parseDotenv } from "./dotenv";

describe("parseDotenv", () => {
  it("reads KEY=value lines, skipping blanks and comments", () => {
    const text = "# database\n\n  DB_PASSWORD = s3cr=t  \nAPI.KEY-1=abc\r\n   # indented comment\n";
    expect(parseDotenv(text)).toEqual({ values: { DB_PASSWORD: "s3cr=t", "API.KEY-1": "abc" } });
  });

  it("strips matching surrounding quotes only", () => {
    const text = `A="double"\nB='single'\nC="mismatched'\nD=" spaced "\nE="`;
    expect(parseDotenv(text).values).toEqual({ A: "double", B: "single", C: `"mismatched'`, D: " spaced ", E: '"' });
  });

  it("keeps the last value for a repeated key", () => {
    expect(parseDotenv("A=1\nA=2").values).toEqual({ A: "2" });
  });

  it("rejects a line without = naming its line number", () => {
    expect(parseDotenv("A=1\n\nJUSTAKEY\nB=2")).toEqual({ error: 'Line 3 has no "=".' });
  });

  it("rejects invalid key names and empty values", () => {
    expect(parseDotenv("=x").error).toBe("Line 1 has no key name.");
    expect(parseDotenv("BAD KEY=x").error).toMatch(/^Line 1: BAD KEY is not a valid key name/);
    expect(parseDotenv("A=1\nB=\nC=''").error).toBe("Line 2: B has no value.");
  });

  it("returns no values for empty input", () => {
    expect(parseDotenv("\n# only a comment\n")).toEqual({ values: {} });
  });
});
