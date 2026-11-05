import { useMutation, useQuery } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Table } from "@/components/ui/controls";
import { Banner, Button, Card, CardHeader, Chip, Field, Input, Mono, PageHeader, Skeleton } from "@/components/ui/primitives";
import { api, ApiError } from "@/lib/api";
import { toast } from "@/lib/toast";
import { relativeTime } from "@/lib/format";
import { qk, useIntegration, useStatus } from "@/lib/queries";
import { useTheme, type ThemePref } from "@/lib/theme";
import { Segmented } from "@/components/ui/controls";

export function SettingsPage() {
  const status = useStatus();
  const integration = useIntegration();
  const { pref, setTheme } = useTheme();
  const s = status.data;
  const i = integration.data;
  const concurrency = s ? [...new Set(s.workers.map((w) => w.concurrency))] : [];

  const installation: [string, string, ReactNode][] = s && i ? [
    ["Public API URL", "Base of the signed artifact and transcript links in task.result.", <Mono key="u">{s.public_api_url}</Mono>],
    ["Public broker address", "Where integrations connect to publish tasks and read events.", <Mono key="b">{i.broker.host}:{i.broker.port}{i.broker.vhost === "/" ? "" : `/${i.broker.vhost}`}</Mono>],
    ["Data directory", "Host directory for run directories, git mirrors and stored objects.", s.data_dir ? <Mono key="d">{s.data_dir}</Mono> : <span key="d" className="text-xs text-subtle">not reported by the API</span>],
    ["Worker concurrency", "Runs each worker executes at once.", concurrency.length ? <Mono key="c">{concurrency.join(", ")}</Mono> : <span key="c" className="text-xs text-subtle">no live worker</span>],
  ] : [];

  return (
    <>
      <PageHeader title="Settings" description="Your account, this browser's appearance, and the values this installation was set up with." />
      <div className="flex flex-col gap-6">
        <PasswordCard />
        <Card>
          <CardHeader title="Appearance" description="Stored in this browser." />
          <div className="flex items-center justify-between px-4 py-3">
            <span className="text-sm">Theme</span>
            <Segmented<ThemePref>
              value={pref}
              onChange={setTheme}
              options={[{ value: "system", label: "System", icon: "monitor" }, { value: "light", label: "Light", icon: "sun" }, { value: "dark", label: "Dark", icon: "moon" }]}
            />
          </div>
        </Card>
        {!s || !i ? (
          <Skeleton className="h-96" />
        ) : (
          <>
            <Card>
              <CardHeader title="Installation" description="Fixed when Exradius was installed." />
              <Table.Root>
                <tbody>
                  {installation.map(([label, what, value]) => (
                    <Table.Row key={label}>
                      <Table.Td className="w-[220px] align-top text-sm font-medium">{label}</Table.Td>
                      <Table.Td className="align-top text-xs text-muted">{what}</Table.Td>
                      <Table.Td className="w-[360px] align-top text-sm">{value}</Table.Td>
                    </Table.Row>
                  ))}
                </tbody>
              </Table.Root>
            </Card>
            <Card>
              <CardHeader title="Live workers" description="Seen in the last 60 seconds." />
              {s.workers.length === 0 ? (
                <div className="p-4"><Banner tone="err" title="No live workers">Start one with <code className="font-mono">docker compose up -d worker</code>.</Banner></div>
              ) : (
                <Table.Root>
                  <Table.Head>
                    <tr><Table.Th>Host</Table.Th><Table.Th>Version</Table.Th><Table.Th>Slots</Table.Th><Table.Th>Docker</Table.Th><Table.Th>{s.workers.some((x) => x.runtimes) ? "Runtimes" : "Runner image"}</Table.Th><Table.Th className="text-right">Last seen</Table.Th></tr>
                  </Table.Head>
                  <tbody>
                    {s.workers.map((wk) => (
                      <Table.Row key={wk.id}>
                        <Table.Td><Mono className="text-xs">{wk.hostname}</Mono></Table.Td>
                        <Table.Td><Mono className="text-xs text-muted">{wk.version ?? "—"}</Mono></Table.Td>
                        <Table.Td className="tabular">{wk.concurrency}</Table.Td>
                        <Table.Td><Chip tone={wk.docker_ok ? "ok" : "err"} dot title={wk.docker_detail ?? undefined}>{wk.docker_ok ? "reachable" : "unreachable"}</Chip></Table.Td>
                        <Table.Td>
                          {wk.runtimes ? (
                            <span className="flex flex-wrap items-center gap-1">
                              {wk.runtimes.map((r) => <Chip key={r.name} mono tone={r.ready ? "neutral" : "err"} dot={!r.ready} title={r.error ?? r.image ?? undefined}>{r.name}{r.version ? ` ${r.version}` : ""}{r.ready ? "" : " not ready"}</Chip>)}
                              {!wk.runtimes.length && <span className="text-xs text-subtle">none</span>}
                            </span>
                          ) : (
                            <span className="flex items-center gap-2"><Mono className="text-xs">{wk.runner_image ?? "—"}</Mono>{wk.runner_image_ok === false && <Chip tone="err">missing</Chip>}</span>
                          )}
                        </Table.Td>
                        <Table.Td className="text-right text-xs text-subtle">{relativeTime(wk.last_seen_at)}</Table.Td>
                      </Table.Row>
                    ))}
                  </tbody>
                </Table.Root>
              )}
            </Card>
          </>
        )}
      </div>
    </>
  );
}

function PasswordCard() {
  const me = useQuery({ queryKey: qk.me, queryFn: api.auth.me });
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [repeat, setRepeat] = useState("");
  const [error, setError] = useState<{ field: "current" | "next"; message: string } | null>(null);
  const change = useMutation({
    // Values are read from state, not passed as mutation variables, so they are not kept in the cache.
    mutationFn: () => api.auth.changePassword(current, next),
    onSuccess: () => {
      setCurrent(""); setNext(""); setRepeat(""); setError(null);
      toast.ok("Password changed", "Other sessions stay signed in until they expire.");
    },
    onError: (e) => {
      const status = e instanceof ApiError ? e.status : 0;
      // 400: the current password is wrong; 422: the new one is rejected.
      setError({ field: status === 400 || status === 401 || status === 403 ? "current" : "next", message: e.message });
    },
  });
  const mismatch = !!repeat && repeat !== next;
  const same = !!next && next === current;
  const short = !!next && next.length < 8;
  const ready = !!current && !!next && repeat === next && !same && !short;

  return (
    <Card>
      <CardHeader title="Password" description={<>Signed in as <Mono>{me.data?.username ?? "…"}</Mono>.</>} />
      <form
        className="grid grid-cols-[1fr_1fr_1fr_auto] items-start gap-3 px-4 py-4"
        onSubmit={(e) => { e.preventDefault(); if (ready) change.mutate(); }}
      >
        <Field label="Current password" error={error?.field === "current" ? error.message : undefined}>
          <Input type="password" autoComplete="current-password" value={current} invalid={error?.field === "current"} onChange={(e) => { setCurrent(e.target.value); setError(null); }} />
        </Field>
        <Field label="New password" hint="At least 8 characters." error={error?.field === "next" ? error.message : same ? "Same as the current one." : undefined}>
          <Input type="password" autoComplete="new-password" value={next} invalid={error?.field === "next" || same} onChange={(e) => { setNext(e.target.value); setError(null); }} />
        </Field>
        <Field label="Repeat new password" error={mismatch ? "Does not match." : undefined}>
          <Input type="password" autoComplete="new-password" value={repeat} invalid={mismatch} onChange={(e) => setRepeat(e.target.value)} />
        </Field>
        <Button type="submit" variant="primary" className="mt-[22px]" loading={change.isPending} disabled={!ready}>Change password</Button>
      </form>
    </Card>
  );
}
