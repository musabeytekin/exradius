import { describe, expect, it } from "vitest";
import { CATALOG, credentialFields, credentialFor, repoCredential, secretLabel } from "./catalog";

describe("catalog credentials", () => {
  it("label every field that carries a secret", () => {
    for (const e of CATALOG) {
      for (const f of credentialFields(e)) {
        expect(f.label, `${e.key}.${f.key}`).not.toBe(f.secret);
        expect(f.help, `${e.key}.${f.key}`).toBeTruthy();
      }
    }
  });

  it("look up labels by secret name", () => {
    expect(secretLabel("GITHUB_TOKEN")).toBe("GitHub token");
    expect(secretLabel("JIRA_API_TOKEN")).toBe("Jira API token");
    expect(secretLabel("DATABASE_URI")).toBe("Database connection string");
    expect(credentialFor("SENTRY_ACCESS_TOKEN")?.createUrl).toMatch(/^https:\/\/sentry\.io\//);
    expect(secretLabel("GIT_TOKEN")).toBe("Repository token");
    expect(secretLabel("GIT_TOKEN_2")).toBe("Repository token 2");
    expect(secretLabel("MY_CUSTOM_KEY")).toBeUndefined();
  });

  it("map github.com and gitlab.com repositories to the catalog tokens", () => {
    expect(repoCredential("https://github.com/acme/api.git")).toMatchObject({ secret: "GITHUB_TOKEN", label: "GitHub token" });
    expect(repoCredential("https://gitlab.com/acme/api.git")?.secret).toBe("GITLAB_TOKEN");
    expect(repoCredential("https://git.acme.com/api.git")).toBeNull();
    expect(repoCredential("not a url")).toBeNull();
  });
});
