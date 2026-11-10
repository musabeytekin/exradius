# Environment section, services, model choice

> **Since the config-repo change (`docs/config-repo.md`, 2026-11-07)** the project's file provides
> what this document puts in tables and panel tabs: `services` (with `image` or a `dockerfile`
> built from the config repository), `env`, `notes`, `workstation` (image or Dockerfile, and
> `setup`), and each agent's `model`. The `project_services` table, the Services API and tab,
> `projects.notes` and `agents.model` columns are gone. A `${secret:NAME}` resolves from the
> config's secret store, and the workstation carries every secret its services' settings use
> under its own name, which is what `$NAME` in the section means.
>
> **Since phase 3 of the move to Kubernetes (2026-11-09)** services are sidecars of the run's
> pod on its `localhost`; §2 is rewritten for it. The environment section, the delivery
> warning and the passwd fix are unchanged.
>
> **Since phase 4 (2026-11-10)** a run reaches the network through the egress proxy only
> (`docs/kubernetes.md` §11): the "Not available" line of §1 ends with the hosts the agent may
> reach instead of "Outbound network access is available."

Agreed 2026-11-07 after the Bank of Anthos live tests. Four problems and the decisions:

| problem seen | decision |
|---|---|
| the agent discovers its surroundings by trial ("no Docker", "not a git repository") | Exradius states the facts in a generated **environment section** (§1) |
| work left in the run directory is lost when the agent does not push | stays the agent's job; the environment section and the rules make the consequence explicit, and the worker adds a **report-only warning** to the result (§4). The worker never commits or pushes for the agent |
| projects whose dependencies need Docker cannot run anything | **services** on the project, GitHub Actions style (§2) |
| every run uses the runtime's default model | **model** on the agent, picked from the provider's catalog (§3) |

## 1. The environment section

A third part of the system prompt: Claude Code's preset, the operating rules, then
`# Your environment`, then the agent's role (architecture §13 is extended, not changed).
Exradius generates it from facts it holds; nothing in it is typed by the admin except the
project notes. It never names Exradius.

```
# Your environment

You run unattended in a Linux container built from the image bank-of-anthos-dev:latest.
Each folder in the working directory is a git clone of one repository:

- bank-of-anthos: branch main, origin https://github.com/musabeytekin/bank-of-anthos.git, pushing allowed
- platform-config: branch main, origin https://github.com/acme/platform-config.git, pushing NOT allowed

Not available: a Docker daemon, Kubernetes, sudo, installing packages. Outbound network access is limited to the repositories above and to: repo.maven.apache.org, pypi.org, files.pythonhosted.org.

Services running for this task, reachable by host name from this container:
- postgres (postgres:16) on port 5432, POSTGRES_USER=app, POSTGRES_DB=app, POSTGRES_PASSWORD=$DB_PASSWORD
- redis (redis:7) on port 6379
A value written as $NAME is the value of your environment variable NAME.

The working directory is deleted when the task ends. Only what you push, attach to the result,
or write in the result survives.

Notes from the project's maintainers:
<projects.notes, verbatim>
```

Sources:

- **Repositories**: `repositories` rows of the project (`name`, `branch`, `url` with any
  credentials stripped, `push_allowed`).
- **Not available**: fixed text. The agent discovers installed tools itself (`which`,
  `--version`); Exradius lists only what it cannot discover: there is no Docker daemon even
  if a client is present, no Kubernetes, no sudo, no package installation.
- **The network sentence** (`environment.network_line`): the agent phase's hosts as the
  egress proxy enforces them (`exradius.network.allowed_hosts`): the install's
  `egress.allow`, the config's `network.allow`, then the agent's connections' hosts (catalog
  `hosts`, a catalog key's URL host, a plain HTTP connection's host), each once. With
  repositories it reads "limited to the repositories above and to: <hosts>.", without them
  "limited to: <hosts>."; with no hosts "limited to the repositories above." or "There is no
  outbound network access." The model and the connections need no mention: they work.
- **Services**: the project's services (§2) with name, image, ports and settings. A
  setting is an env entry shown as `KEY=value`; a `${secret:NAME}` in it is shown as
  `$NAME`, the variable the run environment carries (project secrets are exported under
  their own names), never as the value. A secret that is only global is not in the run
  environment and shows as `<a secret you do not have>`. The `$NAME` hint line is added
  when any setting names a variable. (Added after BANK-204: the agent asked for the user
  and database because the section gave only the port.)
- **The last paragraph** is fixed text.
- **Notes**: `projects.notes`, a text field; omitted when empty.

The runner receives the section ready-made in the run spec (`environment_text`), so a
runtime only concatenates. `build_system_prompt` becomes rules + environment + role.
The resolved agent view (`GET /api/projects/{id}/agents/{slug}/resolved`) returns the
section as the worker would build it, so the panel can show it.

## 2. Services

A project may list services, the dependencies an engineer would run in Docker Desktop.
GitHub Actions `services:` is the model: an image (or a Dockerfile in the config repository)
and its settings, nothing to parse from the repository. They are declared in the config's
`services` (docs/config-repo.md §2): `name` (the host name, `[a-z][a-z0-9-]*`, at most 55
characters), `image` or `dockerfile`, `env` (values may use `${secret:NAME}`), `ports`
(integers, documentation for the agent; nothing is published), `ready`, `command` and
`memory`.

Rewritten for Kubernetes in phase 3 (2026-11-09; `docs/kubernetes.md` §7.4). Until v0.1.0
each service was a container on a per-run Docker network; now it is part of the run's pod.

**Lifecycle, per run.** Each service is a native sidecar of the run's pod: an init container
with `restartPolicy: Always`, after the clones and before the setup commands, in file order.
Kubernetes starts them one by one and starts the next container only when the service is
started, so setup commands and the agent always find their services up. They keep running
while the run waits for input (the pod keeps running), and they stop with the pod. Deleting
the Run at the end removes them with everything else; there is no network, label or sweep of
Exradius's own.

**Readiness.** `ready` becomes the sidecar's startup probe, tried every second:

| `ready` | startup probe |
|---|---|
| `{command: [...]}` | that command, run in the service's container (no shell) |
| `{tcp: p}` | a TCP connection to port `p` from the kubelet. It connects to the pod's address, so a service that listens only on 127.0.0.1 needs a `command` instead |
| `null` | the image's `HEALTHCHECK` (`CMD` as is, `CMD-SHELL` through `/bin/sh -c`), which the worker reads from the image's registry because Kubernetes ignores it; without one, or when the image's config cannot be read, the service counts as started once its process runs |

A service that is not started 120 s after its container started (an image pull does not
count), or that exits or restarts before it is started, fails the run in `preparing` with the
service's name and image, what happened and its log tail:

```
service cache (valkey/valkey:8.1-alpine) did not become ready within 120s waiting for port 6379
<the service's last log lines>
```

The operator decides this, not Kubernetes, which would restart a failing sidecar forever. A
service that crashes after it started is restarted by Kubernetes and the run goes on.

**One network namespace.** A pod's containers share one network namespace, so the services
and the workstation share `localhost`, as on an engineer's machine where compose publishes
every service's ports there:

- The pod's `hostAliases` map every service name to `127.0.0.1`, so `postgres:5432` resolves
  as before. This is the kubelet-managed `/etc/hosts`, which a project cannot change.
- Ports are shared: the config check refuses a port listed by two services, and `localhost`
  is a reserved service name. Two services that listen on the same undeclared port collide;
  the second fails in Verify.
- Parallel runs never share a service: each run is its own pod.

**What a service gets.** Its image's own user, entrypoint and writable root filesystem; the
config's `command` as the container's arguments (Docker's `CMD`; the image's `ENTRYPOINT` is
kept); its `env`, with plain values inline and values holding a secret from the run's
Secret; the capabilities common server images need (`CHOWN`, `SETUID`, `SETGID`, `FOWNER`,
`DAC_OVERRIDE`, `NET_BIND_SERVICE`, every other one dropped), no privilege escalation and
seccomp `RuntimeDefault`; no mounts (an image's `VOLUME` lives in the writable layer). A
declared `memory` is its request and limit; without one it gets the install's default request
and no limit.

Names that would shadow something runs resolve are refused: `api`, `localhost`,
`workstation` and anything starting with `exradius`.

**Verify** shows a "Start service <name>" step per service, after the clones and before the
setup commands, with what it waits for ("waiting for command pg_isready -h 127.0.0.1 -U app"),
then "ready (...)" or the failure with the log tail. There is no "Stop services" step: the
verify Run is deleted at the end.

**The panel** shows services read-only in the project's Config tab (name, image or
Dockerfile, ports, readiness, memory, env with secrets as placeholders). Nothing about them
is edited there.

## 3. Model on the agent

`agents.model` (text, nullable; `null` = the runtime's default). The runtime passes it
through (`ClaudeAgentOptions(model=...)`). Provider catalog entries list the models they
offer (`anthropic`: `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, with a
short description each); the panel's agent form offers "Runtime default" first, the
provider's models, and "Other" with a text field for an id the list does not know. The
provider test (`POST /api/providers/{id}/test`) accepts an optional `model` so a chosen
id can be checked before saving. The run's transcript already records the model used;
`AgentOut` and the resolved view expose `model`.

## 4. Report-only delivery warning

At finalize, before the run directory is removed, the worker checks each repository
clone with `git status --porcelain` and `git log @{upstream}..` (or `git log <base>..` when
no upstream). When a clone has uncommitted changes or commits that were not pushed, the
result payload gains `warnings: ["bank-of-anthos: 2 commits on BANK-204-fix were not pushed
and were discarded with the task directory", ...]`, and the task.result event carries it.
The worker changes nothing in the clone and pushes nothing.

## 5. Small fixes in the same batch

- **Run user home**: runs use uid 1000 with `HOME=/home/runner`, but images carry a passwd
  entry for uid 1000 with another home (Ubuntu's `ubuntu`), and Java reads the home from
  passwd. The worker mounts a generated `/etc/passwd` and `/etc/group` read-only into the
  agent and setup containers: `root` plus `runner:x:<uid>:<gid>::/home/runner:/bin/bash`
  and the matching group. Document in runtime-bundles §2.4.
- **Co-author line**: the Claude Code runtime passes `{"includeCoAuthoredBy": false}` (and
  empty `attribution`) as `--settings` (docs/run-environment.md §7; it was a
  `settings.json` under `CLAUDE_CONFIG_DIR` until then). Commits then carry only the
  identity and the Exradius trailers.
- **Task id required**: the panel's New task form requires a task id (the ticket key) and
  no longer invents one; the API keeps accepting a missing id for integrations.
- **Notes**: `projects.notes` text field and a Notes tab (one text area, hint "Write what
  you would tell a new engineer on their first day."), shown in the environment section.

## 6. Out of scope

Worker-side commits, pushes or pull requests; compose files; Docker inside runs; services
with mounts or published ports; per-task model overrides.
