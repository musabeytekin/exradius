import { describe, expect, it } from "vitest";
import { hostLabel, routeLabel } from "./network";

describe("network labels", () => {
  it("names the proxy's routes", () => {
    expect(routeLabel("connect")).toBe("tunnel");
    expect(routeLabel("mcp")).toBe("connection");
    expect(routeLabel("other")).toBe("other");
  });
  it("shows a port only when it is not the usual one", () => {
    expect(hostLabel({ host: "pypi.org", ports: [443], routes: ["connect"] })).toBe("pypi.org");
    expect(hostLabel({ host: "archive.ubuntu.com", ports: [80], routes: ["http"] })).toBe("archive.ubuntu.com");
    expect(hostLabel({ host: "db.example", ports: [5432], routes: ["connect"] })).toBe("db.example:5432");
    expect(hostLabel({ host: "x.example", ports: [443, 8443], routes: ["connect"] })).toBe("x.example:443,8443");
  });
});
