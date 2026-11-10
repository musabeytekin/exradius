import type { NetworkHost } from "./types";

/** How a host was reached, in words: the proxy's routes by what they carry. */
const ROUTES: Record<string, string> = {
  connect: "tunnel",
  http: "plain HTTP",
  model: "model",
  git: "git",
  mcp: "connection",
};

export function routeLabel(route: string): string {
  return ROUTES[route] ?? route;
}

/** `registry.npmjs.org:443`, or the host alone when every request used one usual port. */
export function hostLabel(h: Pick<NetworkHost, "host" | "ports" | "routes">): string {
  const usual = h.ports.length === 1 && (h.ports[0] === 443 || (h.ports[0] === 80 && h.routes.includes("http")));
  return usual || h.ports.length === 0 ? h.host : `${h.host}:${h.ports.join(",")}`;
}
