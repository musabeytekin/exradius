import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { HealthChip } from "@/components/HealthChip";
import { Table } from "@/components/ui/controls";
import { CopyButton } from "@/components/ui/data";
import { Icon } from "@/components/ui/icons";
import { Tooltip } from "@/components/ui/overlays";
import { Banner, Button, Card, CardHeader, Chip, Mono } from "@/components/ui/primitives";
import { absoluteTime, bytes, relativeTime } from "@/lib/format";
import {
  agentsOf, catalogKeyNames, commitOf, configOf, connectionsOf, errorsOf, projectHealth, rawOf, readAtOf,
  readErrorOf, readyLabel, servicesOf, shortCommit, storeNameOf, warningsOf,
} from "@/lib/projectConfig";
import { api } from "@/lib/api";
import { cn } from "@/lib/cn";
import { qk, useCatalog } from "@/lib/queries";
import { toast } from "@/lib/toast";
import type { CatalogEntry, ConfigAgent, ConfigConnection, Project } from "@/lib/types";

export function ConfigTab({ project }: { project: Project }) {
  const qc = useQueryClient();
  const catalog = useCatalog();
  const reread = useMutation({
    mutationFn: () => api.projects.reread(project.id),
    onSuccess: (p) => {
      if (p && typeof p === "object" && "id" in p) qc.setQueryData(qk.project(project.id), p);
      qc.invalidateQueries({ queryKey: qk.projects });
      const c = p && commitOf(p);
      toast.ok("Config re-read", c ? `Commit ${shortCommit(c)}.` : undefined);
    },
    onError: (e) => toast.err("Could not re-read the config", e),
  });
  return (
    <ConfigView
      project={project}
      catalog={catalog.data}
      onReread={() => reread.mutate()}
      rereading={reread.isPending}
    />
  );
}

/** The snapshot rendered read-only. No queries: tests render it from fixtures. */
export function ConfigView({ project, catalog, onReread, rereading }: {
  project: Project; catalog?: CatalogEntry[]; onReread?: () => void; rereading?: boolean;
}) {
  const config = configOf(project);
  const errors = errorsOf(project);
  const warnings = warningsOf(project);
  const readError = readErrorOf(project);
  const unreadable = !!readError || !commitOf(project);
  const raw = rawOf(project);
  const services = servicesOf(config);
  const connections = connectionsOf(config);
  const agents = agentsOf(config);
  const env = Object.entries(config?.env ?? {});
  const repos = config?.repositories ?? [];
  const ws = config?.workstation;

  return (
    <div className="flex flex-col gap-4">
      <SourceCard project={project} onReread={onReread} rereading={rereading} />

      {unreadable ? (
        <Banner tone="err" title="The config repository could not be read">
          <Messages items={[...(readError ? [readError] : []), ...errors]} />
        </Banner>
      ) : errors.length > 0 && (
        <Banner tone="err" title={errors.length === 1 ? "1 error" : `${errors.length} errors`}>
          <Messages items={errors} />
        </Banner>
      )}
      {warnings.length > 0 && (
        <Banner tone="warn" title={warnings.length === 1 ? "1 warning" : `${warnings.length} warnings`}>
          <Messages items={warnings} />
        </Banner>
      )}

      {!config ? (
        <Card className="px-4 py-6 text-center text-sm text-muted">
          {errors.length || unreadable ? "Nothing to show until the file reads cleanly." : "The API returned no parsed configuration for this project."}
        </Card>
      ) : (
        <>
          <Section title="Project">
            <Pairs
              items={[
                ["Slug", <Mono key="s">{config.project?.slug ?? project.slug}</Mono>],
                ["Name", config.project?.name ?? "—"],
                ...(config.project?.description ? [["Description", config.project.description] as [string, ReactNode]] : []),
                ["Secret store", config.secret_store ? <Mono key="st">{config.secret_store}</Mono> : <Dim key="st">none</Dim>],
              ]}
            />
          </Section>

          <Section title="Repositories" count={repos.length}>
            {repos.length === 0 ? <None /> : (
              <Table.Root>
                <Table.Head>
                  <tr><Table.Th>Folder</Table.Th><Table.Th>URL</Table.Th><Table.Th>Branch</Table.Th><Table.Th>Credential</Table.Th><Table.Th>Push</Table.Th></tr>
                </Table.Head>
                <tbody>
                  {repos.map((r, i) => (
                    <Table.Row key={`${r.name}:${i}`}>
                      <Table.Td><Mono className="font-medium">{r.name}</Mono></Table.Td>
                      <Table.Td><Mono className="break-all text-xs text-muted">{r.url}</Mono></Table.Td>
                      <Table.Td>{r.branch ? <Mono className="text-xs">{r.branch}</Mono> : <Dim>remote default</Dim>}</Table.Td>
                      <Table.Td>{r.credential ? <Mono className="text-xs text-muted">{r.credential}</Mono> : <Dim>anonymous</Dim>}</Table.Td>
                      <Table.Td>{r.push ? <Chip tone="accent">push</Chip> : <Dim>read only</Dim>}</Table.Td>
                    </Table.Row>
                  ))}
                </tbody>
              </Table.Root>
            )}
          </Section>

          <Section title="Workstation">
            <Pairs
              items={[
                ["Image", ws?.dockerfile
                  ? <span key="i">built from <Mono>{ws.dockerfile}</Mono>{ws.context ? <> with context <Mono>{ws.context}</Mono></> : null}</span>
                  : ws?.image ? <Mono key="i">{ws.image}</Mono> : <Dim key="i">default image</Dim>],
                ["Setup", ws?.setup?.length ? (
                  <ol key="c" className="flex flex-col gap-1">
                    {ws.setup.map((c, i) => (
                      <li key={i} className="flex gap-2"><span className="w-4 shrink-0 text-right text-xs tabular text-subtle">{i + 1}</span><Mono className="whitespace-pre-wrap break-all text-xs">{c}</Mono></li>
                    ))}
                  </ol>
                ) : <Dim key="c">none</Dim>],
                ["Memory", config.resources?.memory
                  ? <Mono key="m" className="text-xs">{config.resources.memory}</Mono>
                  : <Dim key="m">not declared</Dim>],
                ["Cache", ws?.cache?.length ? (
                  <div key="k" className="flex flex-col gap-1">
                    {ws.cache.map((c) => <Mono key={c} className="break-all text-xs">{c}</Mono>)}
                    <span className="text-xs text-muted">
                      {project.cache_bytes != null ? `${bytes(project.cache_bytes)} on disk` : "empty"}
                    </span>
                  </div>
                ) : <Dim key="k">none</Dim>],
              ]}
            />
          </Section>

          {services.length > 0 && (
            <Section title="Services" count={services.length}>
              <Table.Root>
                <Table.Head>
                  <tr><Table.Th>Name</Table.Th><Table.Th>Image</Table.Th><Table.Th>Ports</Table.Th><Table.Th>Ready</Table.Th><Table.Th>Memory</Table.Th><Table.Th>Env</Table.Th></tr>
                </Table.Head>
                <tbody>
                  {services.map(([name, s]) => (
                    <Table.Row key={name}>
                      <Table.Td className="align-top py-3"><Mono className="font-medium">{name}</Mono></Table.Td>
                      <Table.Td className="align-top py-3">
                        {s.dockerfile
                          ? <span className="text-xs">built from <Mono className="text-xs">{s.dockerfile}</Mono>{s.context ? <> · <Mono className="text-xs">{s.context}</Mono></> : null}</span>
                          : <Mono className="text-xs text-muted">{s.image ?? "—"}</Mono>}
                        {s.command?.length ? <div><Mono className="break-all text-xs">{s.command.join(" ")}</Mono></div> : null}
                      </Table.Td>
                      <Table.Td className="align-top py-3"><Mono className="text-xs">{s.ports?.length ? s.ports.join(", ") : "—"}</Mono></Table.Td>
                      <Table.Td className="align-top py-3"><Mono className="text-xs text-muted">{readyLabel(s.ready)}</Mono></Table.Td>
                      <Table.Td className="align-top py-3">{s.memory ? <Mono className="text-xs">{s.memory}</Mono> : <Dim>—</Dim>}</Table.Td>
                      <Table.Td className="align-top py-3"><EnvList env={s.env} /></Table.Td>
                    </Table.Row>
                  ))}
                </tbody>
              </Table.Root>
            </Section>
          )}

          {env.length > 0 && (
            <Section title="Environment" count={env.length}>
              <div className="px-4 py-3"><EnvList env={config.env} /></div>
            </Section>
          )}

          {connections.length > 0 && (
            <Section title="Connections" count={connections.length}>
              <ul className="divide-y divide-border">
                {connections.map(([name, c]) => <ConnectionRow key={name} name={name} c={c} catalog={catalog} />)}
              </ul>
            </Section>
          )}

          <Section title="Agents" count={agents.length}>
            {agents.length === 0 ? <None /> : (
              <ul className="divide-y divide-border">
                {agents.map(([slug, a]) => <AgentRow key={slug} slug={slug} a={a} />)}
              </ul>
            )}
          </Section>

          {config.notes?.trim() && (
            <Section title="Notes">
              <p className="whitespace-pre-wrap px-4 py-3 text-sm text-fg">{config.notes}</p>
            </Section>
          )}
        </>
      )}

      {raw && <RawFile text={raw} path={project.config_path} />}
    </div>
  );
}

function SourceCard({ project, onReread, rereading }: { project: Project; onReread?: () => void; rereading?: boolean }) {
  const commit = commitOf(project);
  const readAt = readAtOf(project);
  const store = storeNameOf(project);
  return (
    <Card>
      <div className="flex items-start gap-6 px-4 py-3.5">
        <dl className="grid min-w-0 flex-1 grid-cols-[auto_1fr] gap-x-6 gap-y-1.5 text-sm">
          <dt className="text-muted">Repository</dt>
          <dd className="flex min-w-0 items-center gap-1">
            {project.config_url ? <><Mono className="truncate" title={project.config_url}>{project.config_url}</Mono><CopyButton text={project.config_url} label="" /></> : <Dim>not reported</Dim>}
          </dd>
          <dt className="text-muted">Branch</dt>
          <dd>{project.config_branch ? <Mono>{project.config_branch}</Mono> : <Dim>remote default</Dim>}</dd>
          <dt className="text-muted">File</dt>
          <dd>{project.config_path ? <Mono>{project.config_path}</Mono> : <Dim>exradius.yaml, then .exradius/exradius.yaml</Dim>}</dd>
          <dt className="text-muted">Commit</dt>
          <dd className="flex items-center gap-2">
            {commit ? <Mono title={commit}>{shortCommit(commit)}</Mono> : <Dim>not read</Dim>}
            {readAt && (
              <Tooltip content={absoluteTime(readAt)}><span className="text-xs text-subtle">read {relativeTime(readAt)}</span></Tooltip>
            )}
          </dd>
          {store && (
            <>
              <dt className="text-muted">Secret store</dt>
              <dd><Mono>{store}</Mono></dd>
            </>
          )}
        </dl>
        <div className="flex shrink-0 flex-col items-end gap-2">
          <HealthChip info={projectHealth(project)} />
          {onReread && <Button variant="secondary" size="sm" icon="refresh" loading={rereading} onClick={onReread}>Re-read</Button>}
        </div>
      </div>
    </Card>
  );
}

function ConnectionRow({ name, c, catalog }: { name: string; c: ConfigConnection; catalog?: CatalogEntry[] }) {
  const entry = c.catalog ? catalog?.find((e) => e.key === c.catalog) : undefined;
  const declared = catalogKeyNames(entry);
  const configKeys = Object.keys(c.config ?? {});
  return (
    <li className="flex items-start gap-4 px-4 py-3">
      <Mono className="w-36 shrink-0 pt-0.5 font-medium">{name}</Mono>
      <div className="flex min-w-0 flex-1 flex-col gap-1.5 text-sm">
        {c.catalog ? (
          <>
            <div className="flex items-center gap-2">
              <span className="font-medium text-fg">{entry?.name ?? c.catalog}</span>
              <Chip>catalog</Chip>
              {entry?.transport && <span className="text-xs text-subtle">{entry.transport}</span>}
            </div>
            {configKeys.length === 0 ? <Dim>no config keys</Dim> : (
              <ul aria-label="config keys" className="flex flex-col gap-0.5">
                {configKeys.map((k) => (
                  <li key={k} className="font-mono text-[11.5px] leading-[1.6] break-all">
                    <span className="text-fg">{k}</span><span className="text-subtle">=</span><span className="text-muted">{c.config?.[k]}</span>
                    {declared.optional.includes(k) && <span className="ml-2 font-sans text-2xs text-subtle">optional</span>}
                  </li>
                ))}
              </ul>
            )}
          </>
        ) : c.url ? (
          <>
            <div className="flex items-center gap-2"><Chip>http</Chip><Mono className="break-all text-xs">{c.url}</Mono></div>
            {c.headers && Object.keys(c.headers).length > 0 && <EnvList env={c.headers} label="headers" />}
          </>
        ) : (
          <>
            <div className="flex items-center gap-2"><Chip>stdio</Chip><Mono className="break-all text-xs">{[c.command, ...(c.args ?? [])].filter(Boolean).join(" ")}</Mono></div>
            {c.env && Object.keys(c.env).length > 0 && <EnvList env={c.env} />}
          </>
        )}
      </div>
    </li>
  );
}

function AgentRow({ slug, a }: { slug: string; a: ConfigAgent }) {
  const [open, setOpen] = useState(false);
  const identity = [a.identity?.name, a.identity?.email ? `<${a.identity.email}>` : null].filter(Boolean).join(" ");
  return (
    <li className="px-4 py-3">
      <div className="flex items-start gap-4">
        <div className="w-36 shrink-0">
          <Mono className="font-medium">{slug}</Mono>
          <div className="text-xs text-muted">{a.name || slug}</div>
        </div>
        <dl className="grid min-w-0 flex-1 grid-cols-[auto_1fr] gap-x-5 gap-y-1 text-xs">
          <dt className="text-muted">Model</dt><dd>{a.model ? <Mono className="text-xs">{a.model}</Mono> : <Dim>runtime default</Dim>}</dd>
          <dt className="text-muted">Provider</dt><dd>{a.provider ? <Mono className="text-xs">{a.provider}</Mono> : <Dim>default provider</Dim>}</dd>
          {a.runtime && <><dt className="text-muted">Runtime</dt><dd><Mono className="text-xs">{a.runtime}</Mono></dd></>}
          <dt className="text-muted">Identity</dt><dd>{identity ? <Mono className="text-xs">{identity}</Mono> : <Dim>default</Dim>}</dd>
          <dt className="text-muted">Connections</dt>
          <dd className="flex flex-wrap gap-1">{a.connections?.length ? a.connections.map((c) => <Chip key={c} mono>{c}</Chip>) : <Dim>none</Dim>}</dd>
        </dl>
      </div>
      {a.role?.trim() && (
        <div className="mt-2 pl-40">
          <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="flex items-center gap-1.5 text-xs font-medium text-muted hover:text-fg">
            <Icon name="chevronRight" size={12} className={cn("transition-transform", open && "rotate-90")} />
            Role
          </button>
          {open && (
            <pre className="mt-1.5 max-h-80 overflow-auto rounded-md border border-border bg-code px-3 py-2 font-mono text-[11.5px] leading-[1.6] text-fg whitespace-pre-wrap">{a.role}</pre>
          )}
        </div>
      )}
    </li>
  );
}

function RawFile({ text, path }: { text: string; path?: string | null }) {
  const [open, setOpen] = useState(false);
  return (
    <Card>
      <div className="flex items-center gap-2 px-4 py-2.5">
        <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="flex min-w-0 flex-1 items-center gap-1.5 text-left text-sm font-medium text-muted hover:text-fg">
          <Icon name="chevronRight" size={13} className={cn("shrink-0 transition-transform", open && "rotate-90")} />
          <span className="font-mono text-[12.5px]">{path || "exradius.yaml"}</span>
        </button>
        {open && <CopyButton text={text} />}
      </div>
      {open && (
        <pre aria-label="Config file" className="max-h-[560px] overflow-auto border-t border-border bg-code px-4 py-3 font-mono text-[12px] leading-[1.65] text-fg whitespace-pre">{text}</pre>
      )}
    </Card>
  );
}

function Section({ title, count, children }: { title: string; count?: number; children: ReactNode }) {
  return (
    <Card>
      <CardHeader title={<span className="flex items-center gap-2">{title}{count !== undefined && <span className="rounded-full bg-surface-2 px-1.5 text-2xs font-normal tabular text-muted">{count}</span>}</span>} />
      {children}
    </Card>
  );
}

function Pairs({ items }: { items: [string, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[minmax(110px,auto)_1fr] gap-x-6 gap-y-2 px-4 py-3 text-sm">
      {items.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0 text-fg">{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function EnvList({ env, label }: { env?: Record<string, string> | null; label?: string }) {
  const rows = Object.entries(env ?? {});
  if (!rows.length) return <Dim>—</Dim>;
  return (
    <ul aria-label={label} className="flex flex-col gap-0.5">
      {rows.map(([k, v]) => (
        <li key={k} className="font-mono text-[11.5px] leading-[1.6] break-all">
          <span className="text-fg">{k}</span><span className="text-subtle">=</span><span className="text-muted">{v}</span>
        </li>
      ))}
    </ul>
  );
}

function Messages({ items }: { items: string[] }) {
  return (
    <ul className="mt-1 flex flex-col gap-0.5">
      {items.map((m, i) => <li key={i} className="font-mono text-xs">{m}</li>)}
    </ul>
  );
}

const Dim = ({ children }: { children: ReactNode }) => <span className="text-xs text-subtle">{children}</span>;
const None = () => <p className="px-4 py-3 text-xs text-subtle">None</p>;
