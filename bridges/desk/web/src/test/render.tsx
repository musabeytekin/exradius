import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryHistory } from "@tanstack/react-router";
import { render } from "@testing-library/react";
import { vi } from "vitest";
import { makeQueryClient } from "@/lib/queries";
import { makeRouter } from "@/router";

export interface Call {
  method: string;
  url: string;
  body: unknown;
}

/** Answers Desk's API from ``routes`` ("GET /api/me": value or function) and records calls. */
export function mockApi(routes: Record<string, unknown>) {
  const calls: Call[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    calls.push({ method, url, body: init?.body });
    const key = `${method} ${url}`;
    const found = Object.keys(routes).find((k) => k === key || (k.endsWith("*") && key.startsWith(k.slice(0, -1))));
    if (!found) return new Response(JSON.stringify({ detail: `no route ${key}` }), { status: 404 });
    const value = typeof routes[found] === "function" ? (routes[found] as () => unknown)() : routes[found];
    if (value === undefined) return new Response(null, { status: 204 });
    return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return calls;
}

export function renderAt(path: string) {
  const client = makeQueryClient();
  const router = makeRouter(client);
  router.update({ history: createMemoryHistory({ initialEntries: [path] }) });
  return render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
}

export const ME = { id: 1, name: "Ada Lovelace", email: "ada@acme.test", groups: ["team-a"], exradius: "Exradius" };
export const PROJECTS = [{ slug: "plane", name: "Plane", runnable: true }];
export const PEOPLE = [{ id: 1, name: "Ada Lovelace" }, { id: 2, name: "Ben Okafor" }];
