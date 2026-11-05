import { describe, expect, it } from "vitest";
import { buildSecretIndex, findRefs, insertRef, summarize, validateFields, worstOf } from "./secretRefs";
import type { Secret } from "./types";

const secret = (key: string, project_id: string | null = null): Secret => ({ id: `${project_id ?? "g"}:${key}`, key, project_id });

const index = buildSecretIndex(
  [secret("GITHUB_TOKEN"), secret("ANTHROPIC_API_KEY")],
  [
    { slug: "billing", secrets: [secret("DATABASE_URI", "p1"), secret("SENTRY_TOKEN", "p1")] },
    { slug: "web", secrets: [secret("DATABASE_URI", "p2")] },
  ],
);

describe("findRefs", () => {
  it("resolves a global secret", () => {
    const [ref] = findRefs("Bearer ${secret:GITHUB_TOKEN}", index);
    expect(ref).toMatchObject({ name: "GITHUB_TOKEN", status: "global" });
  });

  it("flags a name that exists only in projects, naming them", () => {
    const [ref] = findRefs("${secret:DATABASE_URI}", index);
    expect(ref.status).toBe("project");
    expect(ref.projects).toEqual(["billing", "web"]);
    expect(ref.message).toMatch(/billing, web/);
  });

  it("flags an unknown name", () => {
    const [ref] = findRefs("${secret:SLACK_BOT_TOKEN}", index);
    expect(ref).toMatchObject({ name: "SLACK_BOT_TOKEN", status: "unknown" });
  });

  it("finds every reference in one value", () => {
    const refs = findRefs("postgres://${secret:DB_USER}:${secret:GITHUB_TOKEN}@host", index);
    expect(refs.map((r) => [r.name, r.status])).toEqual([["DB_USER", "unknown"], ["GITHUB_TOKEN", "global"]]);
  });

  it("is case-sensitive, as the backend is", () => {
    expect(findRefs("${secret:github_token}", index)[0].status).toBe("unknown");
  });

  it.each([
    ["${secrets:GITHUB_TOKEN}", /malformed/],
    ["${secret: GITHUB_TOKEN}", /malformed/],
    ["${secret:GITHUB-TOKEN}", /GITHUB_TOKEN/],
    ["${secret:}", /names no secret/],
    ["${secret:1TOKEN}", /malformed/],
  ])("marks %s as invalid", (value, message) => {
    const [ref] = findRefs(value, index);
    expect(ref.status).toBe("invalid");
    expect(ref.message).toMatch(message);
  });

  it("catches an unterminated reference", () => {
    const refs = findRefs("Bearer ${secret:GITHUB_TOKEN", index);
    expect(refs).toHaveLength(1);
    expect(refs[0]).toMatchObject({ status: "invalid", message: expect.stringMatching(/missing \}/) });
  });

  it("ignores values without references and other ${...} syntax", () => {
    expect(findRefs("https://gitlab.com/api/v4", index)).toEqual([]);
    expect(findRefs("${HOME}/bin", index)).toEqual([]);
  });
});

describe("validateFields and summarize", () => {
  it("reports the worst status per field and collects names for the form", () => {
    const fields = {
      GITHUB_PERSONAL_ACCESS_TOKEN: "${secret:GITHUB_TOKEN}",
      DATABASE_URI: "${secret:DATABASE_URI}",
      SLACK: "${secret:SLACK_BOT_TOKEN} ${secret:GITHUB_TOKEN}",
      BROKEN: "${secret:bad name}",
      PLAIN: "literal",
    };
    const v = validateFields(fields, index);
    expect(Object.fromEntries(v.map((f) => [f.key, f.worst]))).toEqual({
      GITHUB_PERSONAL_ACCESS_TOKEN: "global",
      DATABASE_URI: "project",
      SLACK: "unknown",
      BROKEN: "invalid",
      PLAIN: null,
    });
    const s = summarize(v);
    expect(s.unknown).toEqual(["SLACK_BOT_TOKEN"]);
    expect(s.projectOnly).toEqual(["DATABASE_URI"]);
    expect(s.invalid).toHaveLength(1);
    expect(s.names.sort()).toEqual(["DATABASE_URI", "GITHUB_TOKEN", "SLACK_BOT_TOKEN"]);
  });

  it("orders statuses invalid > unknown > project > global", () => {
    expect(worstOf([])).toBeNull();
    expect(worstOf(findRefs("${secret:GITHUB_TOKEN}${secret:DATABASE_URI}", index))).toBe("project");
  });
});

describe("insertRef", () => {
  it("inserts at the caret and moves the caret after the reference", () => {
    expect(insertRef("Bearer ", "GITHUB_TOKEN")).toEqual({ value: "Bearer ${secret:GITHUB_TOKEN}", caret: 29 });
    expect(insertRef("a-b", "X", 1)).toEqual({ value: "a${secret:X}-b", caret: 12 });
  });

  it("clamps a caret outside the value", () => {
    expect(insertRef("ab", "X", 99).value).toBe("ab${secret:X}");
  });
});
