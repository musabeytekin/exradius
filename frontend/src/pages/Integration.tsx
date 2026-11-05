import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState } from "react";
import { Segmented, Tabs } from "@/components/ui/controls";
import { CodeBlock, JsonTree, KeyValue, StateChip } from "@/components/ui/data";
import { Icon } from "@/components/ui/icons";
import { Button, Card, CardHeader, Chip, Dot, EmptyState, Mono, PageHeader, Skeleton } from "@/components/ui/primitives";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { clockTime, pretty } from "@/lib/format";
import { useAgents, useIntegration, useProjects, useStatus } from "@/lib/queries";
import type { Integration, StoredEvent } from "@/lib/types";

export function IntegrationPage() {
  const integration = useIntegration();
  const status = useStatus();
  const projects = useProjects();
  const agents = useAgents();
  const project = projects.data?.[0]?.slug ?? "billing-platform";
  const agent = agents.data?.[0]?.slug ?? "bug-fixer";

  return (
    <>
      <PageHeader
        title="Integration"
        description="Everything the integration layer needs: where the broker is, what to publish, what comes back. Values are this installation's."
      />
      {integration.isLoading || !integration.data ? (
        <Skeleton className="h-64" />
      ) : (
        <div className="flex flex-col gap-6">
          <div className="grid grid-cols-2 gap-6">
            <BrokerCard i={integration.data} brokerOk={status.data?.broker.ok} />
            <TopologyCard i={integration.data} />
          </div>
          <Schemas i={integration.data} project={project} agent={agent} />
          <Snippets i={integration.data} project={project} agent={agent} />
          <LiveTail />
        </div>
      )}
    </>
  );
}

function amqpUrl(i: Integration, masked = true) {
  const b = i.broker;
  const auth = b.username ? `${b.username}${b.password_set ? `:${masked ? "<password>" : "PASSWORD"}` : ""}@` : "";
  const vhost = b.vhost === "/" ? "/" : `/${encodeURIComponent(b.vhost)}`;
  return `amqp://${auth}${b.host}:${b.port}${vhost}`;
}

function BrokerCard({ i, brokerOk }: { i: Integration; brokerOk?: boolean }) {
  return (
    <Card>
      <CardHeader
        title="Broker"
        description="RabbitMQ, as integrations reach it."
        actions={brokerOk === undefined ? null : <Chip tone={brokerOk ? "ok" : "err"} dot>{brokerOk ? "reachable from the API" : "unreachable"}</Chip>}
      />
      <div className="flex flex-col gap-4 p-4">
        <KeyValue
          items={[
            ["Host", <Mono>{i.broker.host}</Mono>],
            ["Port", <Mono>{i.broker.port}</Mono>],
            ["Virtual host", <Mono>{i.broker.vhost}</Mono>],
            ["Username", i.broker.username ? <Mono>{i.broker.username}</Mono> : <span className="text-subtle">none</span>],
            ["Password", i.broker.password_set ? <span className="inline-flex items-center gap-1.5 text-muted"><Icon name="lock" size={12} />set in deploy/.env (RABBITMQ_PASSWORD)</span> : <span className="text-warn">not set</span>],
          ]}
        />
        <CodeBlock code={amqpUrl(i)} />
      </div>
    </Card>
  );
}

function TopologyCard({ i }: { i: Integration }) {
  return (
    <Card>
      <CardHeader title="Queues and exchange" description="Publish to the queues; bind your own queue to the exchange." />
      <div className="flex flex-col gap-3 p-4 text-sm">
        <Route dir="in" name={i.tasks_queue} kind="queue" note="Tasks you publish" />
        <Route dir="in" name={i.answers_queue} kind="queue" note="Answers to info requests" />
        <Route dir="out" name={i.events_exchange} kind={`${i.events_exchange_type} exchange`} note="Status, questions and results" />
        <div className="flex flex-wrap gap-1.5 pl-9">
          {i.routing_keys.map((k) => <Chip key={k} mono>{k}</Chip>)}
          <Chip mono tone="accent">task.#</Chip>
        </div>
        <p className="pl-9 text-xs text-subtle">Messages are JSON with <code className="font-mono">"version": 1</code>. Unknown fields are ignored.</p>
      </div>
    </Card>
  );
}

function Route({ dir, name, kind, note }: { dir: "in" | "out"; name: string; kind: string; note: string }) {
  return (
    <div className="flex items-center gap-3">
      <span className={cn("flex size-6 items-center justify-center rounded-md", dir === "in" ? "bg-info-soft text-info" : "bg-ok-soft text-ok")}>
        <Icon name={dir === "in" ? "arrowRight" : "arrowUpRight"} size={13} />
      </span>
      <Mono className="font-medium">{name}</Mono>
      <span className="text-xs text-subtle">{kind}</span>
      <span className="ml-auto text-xs text-muted">{note}</span>
    </div>
  );
}

function examples(i: Integration, project: string, agent: string) {
  const api_ = i.public_api_url.replace(/\/$/, "");
  const envelope = (type: string, payload: unknown) => ({
    version: 1, task_id: "JIRA-4821", run_id: "8f1c2a4e-…", project, agent,
    correlation: { ticket: "JIRA-4821" }, timestamp: "2026-11-05T19:12:03Z", type, payload,
  });
  return {
    task: {
      label: "Task", where: `publish to ${i.tasks_queue}`,
      notes: ["task_id is the idempotency key: a duplicate is acknowledged and rejected.", "project and agent are slugs; the agent must be attached to the project.", "Each attachment has exactly one of url or content_base64."],
      body: {
        version: 1, task_id: "JIRA-4821", project, agent,
        instructions: "Customers report invoices with negative totals since Monday. Find the cause and fix it.",
        context: { customer: "acme", severity: "high" },
        attachments: [{ name: "stack.txt", content_type: "text/plain", content_base64: "VHJhY2ViYWNrIC4uLg==" }],
        correlation: { ticket: "JIRA-4821" },
      },
    },
    answer: {
      label: "Answer", where: `publish to ${i.answers_queue}`,
      notes: ["The first answer for a request_id resumes the run; later ones are stored, not delivered."],
      body: { version: 1, task_id: "JIRA-4821", request_id: "a7c1e0d2-…", answer: "Yes, the migration ran on Monday 03:00 UTC.", attachments: [] },
    },
    status: {
      label: "task.status", where: `from ${i.events_exchange}`,
      notes: ["Every state change: queued, preparing, running, waiting_for_input, resuming, completed, failed, cancelled, rejected."],
      body: envelope("status", { state: "running", previous: "preparing", reason: null }),
    },
    info_request: {
      label: "task.info_request", where: `from ${i.events_exchange}`,
      notes: ["The run is paused until an answer with this request_id arrives."],
      body: envelope("info_request", { request_id: "a7c1e0d2-…", question: "Did a database migration run on Monday?", context: null }),
    },
    result: {
      label: "task.result", where: `from ${i.events_exchange}`,
      notes: ["Download URLs are signed; they work without an admin session.", "output is the JSON passed to submit_result, or null."],
      body: envelope("result", {
        state: "completed", summary: "Fixed the rounding in `InvoiceTotal`…", output: { cause: "…", branch: "fix/negative-totals" },
        artifacts: [{ name: "report.md", content_type: "text/markdown", url: `${api_}/api/artifacts/<id>/content?sig=…` }],
        transcript_url: `${api_}/api/runs/<run_id>/transcript?sig=…`, error: null,
      }),
    },
  };
}

function Schemas({ i, project, agent }: { i: Integration; project: string; agent: string }) {
  const ex = examples(i, project, agent);
  const [tab, setTab] = useState<keyof typeof ex>("task");
  return (
    <Card>
      <CardHeader title="Message schemas" description={`Examples use your first project (${project}) and agent (${agent}).`} />
      <Tabs.Root value={tab} onValueChange={(v) => setTab(v as keyof typeof ex)}>
        <Tabs.List className="px-3">
          {Object.entries(ex).map(([k, v]) => <Tabs.Trigger key={k} value={k}><span className="font-mono text-[12.5px]">{v.label}</span></Tabs.Trigger>)}
        </Tabs.List>
        {Object.entries(ex).map(([k, v]) => (
          <Tabs.Content key={k} value={k} className="grid grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)] gap-5 p-4">
            <CodeBlock title={v.where} lang="json" code={pretty(v.body)} />
            <ul className="flex flex-col gap-2 text-sm text-muted">
              {v.notes.map((n) => <li key={n} className="flex gap-2"><Icon name="info" size={14} className="mt-0.5 shrink-0 text-subtle" />{n}</li>)}
            </ul>
          </Tabs.Content>
        ))}
      </Tabs.Root>
    </Card>
  );
}

function Snippets({ i, project, agent }: { i: Integration; project: string; agent: string }) {
  const [lang, setLang] = useState<"python" | "curl">("python");
  const b = i.broker;
  const python = `# pip install pika
import json, pika

params = pika.URLParameters("${amqpUrl(i)}")  # password from RABBITMQ_PASSWORD
conn = pika.BlockingConnection(params)
ch = conn.channel()

# Listen first: a private queue bound to every task event.
q = ch.queue_declare(queue="", exclusive=True).method.queue
ch.queue_bind(queue=q, exchange="${i.events_exchange}", routing_key="task.#")

# Publish a task.
task = {
    "version": 1,
    "task_id": "${project}-demo-1",
    "project": "${project}",
    "agent": "${agent}",
    "instructions": "Describe the repository layout.",
    "context": None,
    "attachments": [],
}
ch.basic_publish(exchange="", routing_key="${i.tasks_queue}", body=json.dumps(task),
                 properties=pika.BasicProperties(content_type="application/json", delivery_mode=2))

def on_event(ch, method, props, body):
    event = json.loads(body)
    print(method.routing_key, event["task_id"], event["payload"])
    if method.routing_key == "task.info_request":
        answer = {"version": 1, "task_id": event["task_id"],
                  "request_id": event["payload"]["request_id"], "answer": "Yes."}
        ch.basic_publish(exchange="", routing_key="${i.answers_queue}", body=json.dumps(answer))

ch.basic_consume(queue=q, on_message_callback=on_event, auto_ack=True)
ch.start_consuming()`;
  const curl = `# Publish a task through the RabbitMQ management API (port 15672 by default).
curl -u ${b.username ?? "guest"}:"$RABBITMQ_PASSWORD" \\
  -H 'content-type: application/json' \\
  -X POST 'http://${b.host}:15672/api/exchanges/${encodeURIComponent(b.vhost)}/amq.default/publish' \\
  -d '{
    "routing_key": "${i.tasks_queue}",
    "payload_encoding": "string",
    "properties": {"content_type": "application/json", "delivery_mode": 2},
    "payload": "{\\"version\\":1,\\"task_id\\":\\"${project}-demo-1\\",\\"project\\":\\"${project}\\",\\"agent\\":\\"${agent}\\",\\"instructions\\":\\"Describe the repository layout.\\"}"
  }'

# Download an artifact or a transcript with the signed URL from task.result.
curl -fsSL -o transcript.jsonl '${i.public_api_url.replace(/\/$/, "")}/api/runs/<run_id>/transcript?sig=<sig>'`;
  return (
    <Card>
      <CardHeader
        title="Snippets"
        description="Copy, set the password, run."
        actions={<Segmented size="sm" value={lang} onChange={setLang} options={[{ value: "python", label: "Python" }, { value: "curl", label: "curl" }]} />}
      />
      <div className="p-4">
        <CodeBlock code={lang === "python" ? python : curl} lang={lang === "python" ? "python" : "bash"} maxHeight={520} />
      </div>
    </Card>
  );
}

function LiveTail() {
  const [since] = useState(() => new Date(Date.now() - 3600_000).toISOString());
  const [events, setEvents] = useState<StoredEvent[]>([]);
  const [paused, setPaused] = useState(false);
  const [filter, setFilter] = useState<"all" | "status" | "info_request" | "result">("all");
  const cursor = useRef(since);
  const seen = useRef(new Set<string>());

  const poll = useQuery({
    queryKey: ["events", "tail"],
    queryFn: async () => {
      const batch = await api.events.recent({ since: cursor.current, limit: 500 });
      return batch;
    },
    refetchInterval: paused ? false : 2000,
    refetchIntervalInBackground: false,
    gcTime: 0,
  });

  useEffect(() => {
    const batch = poll.data;
    if (!batch?.length) return;
    const fresh = batch.filter((e) => !seen.current.has(e.id));
    fresh.forEach((e) => seen.current.add(e.id));
    cursor.current = batch[batch.length - 1].created_at;
    if (fresh.length) setEvents((prev) => [...prev, ...fresh].slice(-500));
  }, [poll.data]);

  const shown = useMemo(() => [...events].reverse().filter((e) => filter === "all" || e.type === filter), [events, filter]);

  return (
    <Card>
      <CardHeader
        title={<span className="flex items-center gap-2">Live tail of {"exradius.events"} <Dot tone={paused ? "neutral" : "ok"} pulse={!paused} /></span>}
        description="The last hour, newest first, as stored before publishing. Polls every two seconds."
        actions={
          <>
            <Segmented
              size="sm"
              value={filter}
              onChange={setFilter}
              options={[{ value: "all", label: "All" }, { value: "status", label: "status" }, { value: "info_request", label: "info_request" }, { value: "result", label: "result" }]}
            />
            <Button size="sm" variant="secondary" icon={paused ? "play" : "stop"} onClick={() => setPaused(!paused)}>{paused ? "Resume" : "Pause"}</Button>
          </>
        }
      />
      {poll.isLoading ? (
        <div className="p-4"><Skeleton className="h-24" /></div>
      ) : shown.length === 0 ? (
        <EmptyState compact icon="pulse" title="Waiting for your first event" action={<Link to="/tasks" search={{ send: "1" }} className="text-sm text-accent hover:underline">Send a task</Link>}>
          Nothing was published in the last hour. Events appear here within two seconds of being stored.
        </EmptyState>
      ) : (
        <ul className="max-h-[520px] divide-y divide-border overflow-y-auto">
          {shown.map((e) => <TailRow key={e.id} e={e} />)}
        </ul>
      )}
    </Card>
  );
}

function TailRow({ e }: { e: StoredEvent }) {
  const [open, setOpen] = useState(false);
  const p = e.payload.payload as { state?: string; question?: string };
  return (
    <li className="animate-fade-in">
      <button type="button" onClick={() => setOpen(!open)} className="flex w-full items-center gap-3 px-4 py-2 text-left hover:bg-surface-2/60" aria-expanded={open}>
        <span className="w-16 text-xs tabular text-subtle">{clockTime(e.created_at)}</span>
        <Mono className="w-36 text-xs">task.{e.type}</Mono>
        <Mono className="min-w-0 flex-1 truncate text-xs text-muted">{e.task_id}</Mono>
        {p.state ? <StateChip state={p.state} /> : p.question ? <span className="max-w-xs truncate text-xs text-muted">{p.question}</span> : null}
        {!e.published_at && <Chip tone="warn" title="Stored but not published to the broker">unpublished</Chip>}
        <Icon name="chevronDown" size={13} className={cn("text-subtle transition-transform", !open && "-rotate-90")} />
      </button>
      {open && (
        <div className="flex flex-col gap-2 border-t border-border bg-surface-2/40 px-4 py-3">
          <JsonTree value={e.payload} defaultExpandDepth={2} />
          <Link to="/tasks/$taskId" params={{ taskId: e.task_id }} className="self-start text-xs text-accent hover:underline">Open task</Link>
        </div>
      )}
    </li>
  );
}
