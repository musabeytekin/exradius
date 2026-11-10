import { Tooltip } from "@/components/ui/overlays";
import { Card, CardHeader, Chip, EmptyState, Mono, Skeleton } from "@/components/ui/primitives";
import { absoluteTime, bytes, relativeTime } from "@/lib/format";
import { hostLabel, routeLabel } from "@/lib/network";
import { useTaskNetwork } from "@/lib/queries";
import type { NetworkHost } from "@/lib/types";

/** The task's network: what the egress proxy refused, then what it let through. */
export function NetworkHosts({ taskId, live }: { taskId: string; live: boolean }) {
  const network = useTaskNetwork(taskId, live);
  if (network.isLoading) return <Skeleton className="h-40" />;
  if (network.isError) return <p className="text-sm text-err">Could not load the network record: {(network.error as Error).message}</p>;
  const { reached = [], refused = [] } = network.data ?? {};
  if (!reached.length && !refused.length) {
    return (
      <Card>
        <EmptyState icon="shield" title="No requests recorded">
          Every request of the run's pod goes through the network proxy and appears here: during setup any host, for the agent only its model, repositories, connections and <code className="font-mono text-xs">network.allow</code>.
        </EmptyState>
      </Card>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      {refused.length > 0 && (
        <Card className="border-err/30">
          <CardHeader title={`Refused (${refused.length})`} description="The proxy answered 403 with this reason; the agent saw it." />
          <HostList hosts={refused} refused />
        </Card>
      )}
      <Card>
        <CardHeader title={`Reached (${reached.length})`} />
        {reached.length ? <HostList hosts={reached} /> : <p className="px-4 py-5 text-center text-xs text-subtle">Nothing yet.</p>}
      </Card>
    </div>
  );
}

function HostList({ hosts, refused }: { hosts: NetworkHost[]; refused?: boolean }) {
  return (
    <ul className="divide-y divide-border">
      {hosts.map((h) => (
        <li key={h.host} className="flex flex-col gap-1 px-4 py-2.5">
          <div className="flex items-center gap-2">
            <Mono className={refused ? "text-[13px] text-err" : "text-[13px]"}>{hostLabel(h)}</Mono>
            {h.phases.map((p) => <Chip key={p} tone={p === "agent" ? "accent" : "neutral"}>{p}</Chip>)}
            {h.routes.filter((r) => r !== "connect").map((r) => <Chip key={r} mono>{routeLabel(r)}</Chip>)}
            <span className="ml-auto flex items-center gap-3 text-xs tabular text-muted">
              <span>{h.requests} {h.requests === 1 ? "request" : "requests"}</span>
              {!refused && h.bytes > 0 && <span>{bytes(h.bytes)}</span>}
              {h.last_at && (
                <Tooltip content={absoluteTime(h.last_at)}>
                  <span className="text-subtle">{relativeTime(h.last_at)}</span>
                </Tooltip>
              )}
            </span>
          </div>
          {refused && h.reason && <p className="text-xs text-muted">{h.reason}</p>}
        </li>
      ))}
    </ul>
  );
}
