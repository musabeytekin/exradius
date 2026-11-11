# Exradius Architecture

Status: agreed design, 2026-11-05. This document is the implementation brief for version one.

## 1. What Exradius is

Exradius is a self-hosted execution harness for AI agents inside one software company. It receives tasks from a message queue, runs the named agent against the named project in an isolated container, and publishes what happened back onto the queue.

Exradius does **not**:

- manage tasks (no backlog, no prioritisation, no ticket model);
- integrate with ticketing, chat or CI systems. Publishing tasks and consuming Exradius' outbound messages is the user's job, through an integration layer they build;
- support more than one company per installation;
- limit runs by turns, tokens or wall-clock time in version one;
- decide what an agent may do. An agent may fully use every MCP server it is given. What it does with them is a prompt matter.

## 2. Core concepts

| Concept | Meaning |
|---|---|
| **Project** | A software initiative. Owns one or more repositories, setup commands, environment variables, secrets and attached agents. |
| **Repository** | A git remote that belongs to a project. Checked out as a subdirectory of the project directory. |
| **Connection** | A configured MCP server (command or URL, arguments, environment). Defined globally, attachable to agents and overridable per project. |
| **Agent** | A prompt, a list of connections, and a runtime. Defined globally, attached to projects. |
| **Runtime** | The thing that executes an agent. Version one ships one: Claude Code via the Claude Agent SDK for Python. The interface is pluggable. |
| **Task** | One inbound request: "agent X, do this on project Y". Identified by a user-supplied id. |
| **Run** | One execution attempt of a task. Owns a working directory and a container. A task has one run in version one; the field exists so retries fit later. |
| **Info request** | A question the agent asks mid-task. Suspends the run until an answer arrives. |
| **Result** | The outbound message emitted when a run ends, successful or not. |

Agents are isolated: they never see each other, never share a directory, and nothing in Exradius lets one agent address another.

## 3. Components

Exradius runs on Kubernetes. It declares what a task needs as a `Run` object, and an operator
turns it into a pod; Exradius itself never starts a container. The design is
`docs/kubernetes.md`, which this section summarises.

```
 integration layer ──▶ RabbitMQ ◀──────────────────────────────┐
                          │ tasks, answers, control            │ events
                          ▼                                    │
 Frontend ──▶ api (Python) ◀── internal port 8001 ──┐    worker (Python) ──▶ Postgres
              │                                      │      │  master key
              ▼                                      │      │  writes Run objects and per-run Secrets
          Postgres, object store                     │      ▼
                                                     │  ┌──────────── Kubernetes API ────────────┐
                                                     │  │  Run objects, pods, PVCs, Secrets      │
                                                     │  │  (namespace exradius-runs)             │
                                                     │  └─────────────────▲──────────────────────┘
                                                     │                    │ watches Runs, creates pods
                                                     │             operator (Go)
                                                     │
                                          run pod: prepare, clone-*, setup-*, files, workstation + runner
```

- **api.** FastAPI, one process on two ports: 8000 for the admin panel API, the integration
  API, hooks and signed downloads; 8001 (`/internal/*` only) for run pods. Owns the Postgres
  schema.
- **worker.** Consumes tasks, answers and control messages from the broker, reads the
  config, creates a `Run` and its per-run Secret for each run, settles runs from the Run's
  status and from what the runner reported, publishes outbound events, and deletes the Run at
  the end. It holds the master key and may only ask for Runs: it cannot create pods or read
  Secrets. Concurrency (Runs in flight) is a worker setting. A second replica is safe.
- **operator.** Go, controller-runtime. Watches Runs in `exradius-runs`, creates each Run's
  workspace PVC and pod, writes the pod's security settings itself, mirrors the pod's state
  into the Run's status, and deletes the pod on cancel. It holds nothing secret.
- **Runner.** The Python process in the run pod, from the runtime bundle (image volume at
  `/opt/exradius/runtime`): it prepares the workspace, clones, writes answer attachments,
  hosts the Claude Agent SDK session and the built-in `exradius` MCP server, and talks to the
  api's internal port with the Run's token.
- **Broker.** RabbitMQ (the RabbitMQ operator in a local install). Pointing Exradius at an
  external broker is a configuration value, not a feature.
- **Postgres.** All configuration and all task/run state; the source of truth. A Run object
  holds only the live execution. CloudNativePG in a local install, a managed database in
  clouds.
- **Object store.** Transcripts, result artifacts, attachments, in any S3 API: SeaweedFS in a
  local install, S3 in AWS.
- **Frontend.** React single-page app, served by nginx. Admin panel only.

## 4. Message contracts

Every message is JSON and carries `"version": 1`. Unknown fields are ignored. Breaking changes bump the version.

### 4.1 Inbound: tasks

Queue: `exradius.tasks`. Published by the user.

```json
{
  "version": 1,
  "task_id": "JIRA-4821",
  "project": "billing-platform",
  "agent": "bug-fixer",
  "instructions": "Customers report invoices with negative totals since Monday. Find the cause and fix it.",
  "context": { "any": "json the user wants the agent to see" },
  "attachments": [
    { "name": "screenshot.png", "content_type": "image/png", "url": "https://..." },
    { "name": "stack.txt",      "content_type": "text/plain", "content_base64": "..." }
  ],
  "correlation": { "opaque": "echoed back unchanged on every outbound message" }
}
```

- `task_id` is the idempotency key. A second task with the same id is acknowledged and dropped, with a `rejected` status event.
- `project` and `agent` are slugs. Unknown slugs, or an agent not attached to the project, produce a `rejected` status event.
- `agent` is optional (agreed 2026-11-08, `docs/agent-routing.md`). Without it, Exradius picks one of the project's agents at intake with OpenAI's Decisions API; the `queued` status event says which and how (`payload.routing`), and the envelope's `agent` is the chosen slug from then on.
- `context` and `attachments` are handed to the agent verbatim: `context` is rendered into the prompt, attachments are written into a read-only `.exradius/attachments/` folder inside the project directory and their paths listed in the prompt.
- The worker persists the task in Postgres **before** acknowledging the broker message.

### 4.2 Inbound: answers

Queue: `exradius.answers`. Published by the user in reply to an info request.

```json
{
  "version": 1,
  "task_id": "JIRA-4821",
  "request_id": "a7c1...",
  "answer": "Yes, the migration ran on Monday 03:00 UTC. Here is the log.",
  "attachments": []
}
```

The first answer for a `request_id` resumes the run. Later ones are stored and logged, not delivered.

### 4.3 Outbound: events

Exchange: `exradius.events`, type topic. The user binds their own queues. Routing keys:

| Routing key | When |
|---|---|
| `task.status` | Every state change (see §5). |
| `task.info_request` | The agent asked a question. |
| `task.result` | The run ended. |

Common envelope:

```json
{
  "version": 1,
  "task_id": "JIRA-4821",
  "run_id": "uuid",
  "project": "billing-platform",
  "agent": "bug-fixer",
  "correlation": { "opaque": "..." },
  "timestamp": "2026-11-05T19:12:03Z",
  "type": "status | info_request | result",
  "payload": { }
}
```

Payloads:

```json
// status
{ "state": "running", "previous": "queued", "reason": null }

// info_request
{ "request_id": "a7c1...", "question": "Did a database migration run on Monday?", "context": "optional text the agent added" }

// result
{
  "state": "completed | failed | cancelled",
  "summary": "final assistant message, markdown",
  "output": { "structured": "json the agent submitted via submit_result, or null" },
  "artifacts": [ { "name": "report.md", "content_type": "text/markdown", "url": "https://exradius.example.com/api/artifacts/<id>/content?sig=..." } ],
  "transcript_url": "https://exradius.example.com/api/runs/<run_id>/transcript?sig=...",
  "error": null
}
```

Artifact `url` and `transcript_url` are HTTP download URLs served by the API server, built from its public base URL (`EXRADIUS_PUBLIC_API_URL`). Each carries a signature (`sig`), so the integration layer can download without an admin session; the transcript is JSON lines, one runtime event per line. The URLs do not depend on which object store is configured.

### 4.4 Directory and config hook

`docs/integration-api.md` (agreed 2026-11-08) adds three things integrations can use:
- **Config hook:** `POST /api/hooks/config/<token>`, one per project. A git host calls it on
  push, and Exradius re-reads the config.
- **Directory notice:** a `directory_changed` event on `exradius.events` with routing key
  `directory.changed`. It is sent only when the list of projects and agents changes, and it
  belongs to no task.
- **Directory:** `GET /api/integration/v1/directory`, the projects and their agents,
  authenticated by an integration token created in the panel.

Revised later that day: each token is bound to a bridge, and the directory lists only the
projects whose config repository has that bridge's file (`bridges/<name>.yaml`), with the
file's content. Bridges report the problems they find through
`PUT /api/integration/v1/status`, and the panel shows them.

## 5. Run lifecycle

States of a task (and its single run):

```
queued ──▶ preparing ──▶ running ──▶ completed
                            │  ▲         
                            │  └── resuming ◀── waiting_for_input
                            │                       ▲
                            └───────────────────────┘
   any non-terminal state ──▶ cancelled   (admin action)
   any state ─────────────▶ failed       (error)
   on intake ──────────────▶ rejected    (invalid task)
```

Every transition emits a `task.status` event.

### 5.1 Preparing

The worker reads the config afresh, records the commit, then creates the run's `Run` object
and its per-run Secret in `exradius-runs` (`docs/kubernetes.md` §5, §8.1). The Run spec is a
secret-free description: the workstation image, setup commands, memory, caches, the
project's variables (secret values by Secret key), the repositories, the services, the
agent's git variables and the runtime bundle image. The operator creates a workspace PVC,
the project's volume of mirrors and caches on its first run, and one pod:

1. `prepare` lays out the workspace, creates the cache directories and writes the task's
   attachments, read-only, under `/work/.exradius/attachments/`;
2. one `clone-<n>` per repository refreshes its mirror on the project volume, clones it into
   `/work/<folder>` and checks out the configured branch (the remote's default without one);
3. one sidecar `service-<name>` per service starts, in order, and must become ready
   (`docs/environment-and-services.md` §2); services stay up until the pod ends and are
   reached by name on the pod's `localhost`;
4. one `setup-<n>` per setup command runs it with `/work` as working directory, inside the
   workstation image (so toolchains match), with the caches mounted. A non-zero exit fails
   the run;
5. the `files` sidecar starts, then the workstation container (§5.2).

The workspace PVC holds `/work`, the run user's home `/home/runner`, `/tmp` and the runtime
state. Home and `/tmp` are on disk, like an engineer's home directory, because package
manager caches and build output land there: one `yarn install` of a large Node project leaves
800 MiB in its cache, and Yarn Berry stages as much again in `/tmp`. Setup and the agent share
them, so a cache filled by setup is warm for the agent.

Each init container is a step in the Run's status, which Verify shows the same way. A pod
that cannot be scheduled waits, and the panel says "waiting for cluster capacity"; the run
stays `preparing`.

Nothing is shared between concurrent runs but the project's caches and mirrors: each has its
own pod, workspace and services. Any number of agents can work on the same project at once.

### 5.2 Running

The workstation container runs the project's image with the agent's runtime bundle mounted
into it (`docs/runtime-bundles.md`). The project image is any glibc-based Linux image with
`bash` and `git` on `PATH` (Claude Code's shell tool uses bash), for example `python:3.14` or
`maven:3-eclipse-temurin-21`; musl images such as Alpine are not supported. A project without
an image gets `exradius-project-default` (Ubuntu LTS with git, curl, ca-certificates and
build-essential), which is a default, not a base. Nothing has to inherit from an Exradius
image. The container gets:

- the workspace's `work` at `/work`, the working directory, with the attachments read-only;
- the workspace's `home`, `tmp` and `state` at `/home/runner`, `/tmp` and `/exradius/state`;
- environment: the project's variables, the project's secrets (from the per-run Secret), the
  variables of the agent's model provider (or the default provider; see `docs/providers.md`),
  the agent's git identity and `EXRADIUS_API_URL`; the run token as a file the runner deletes
  once read (`docs/kubernetes.md` §7.8);
- the runtime bundle mounted read-only at `/opt/exradius/runtime`, running
  `/opt/exradius/runtime/bin/exradius-runner <run id>`.

The run moves to `running` when the workstation container starts.

The runner fetches the run spec from the internal API (prompt, connections, task instructions, context, attachment list), assembles the Claude Agent SDK options and starts the session. The prompt is split as §13 describes: the system prompt is Claude Code's preset, then the fixed operating rules, then the agent's prompt; the first user message is only the ticket:

```
# Task <task id>
<instructions>

# Context
<context json, pretty-printed>

# Attachments
- .exradius/attachments/screenshot.png (image/png)
```

Neither part mentions workspaces or Exradius internals beyond the `exradius` tools below. The agent simply starts in a directory that contains the project's repositories.

Every event from the SDK stream is forwarded to the internal API and stored as the transcript.

### 5.3 The `exradius` MCP server

Every agent gets one built-in connection, hosted in-process by the runner:

| Tool | Effect |
|---|---|
| `request_information(question, context?)` | Records an info request, publishes `task.info_request`, returns "Your question was sent. Stop working now; you will be resumed with the answer." The runner then interrupts the session, which moves the run to `waiting_for_input`. |
| `submit_result(output, artifacts?)` | Stores structured output and uploads the listed files from the working directory to the object store. Can be called more than once; the last call wins. |

Any other destination for results (posting to a chat tool, writing to a wiki) is just the agent using a connection the admin attached. The `task.result` event is emitted regardless.

### 5.4 Waiting and resuming

When the agent asks a question, the runner interrupts the session, reports `waiting` and
**stays alive**: the pod, its workspace and uncommitted work all stay as they are. When an
answer arrives, the worker moves the run to `resuming`; the runner, long-polling the internal
API, gets the answer, waits for the `files` sidecar to write any answer attachments, reports
`resumed` and resumes the runtime session in the same process (for Claude Code: the SDK
`resume` option with the stored session id) with the answer as the next user message. The run
returns to `running`.

A run can cycle through `waiting_for_input` any number of times. The pod's resources stay
reserved while it waits. A pod lost while the run waits is recreated on the same workspace
when the answer arrives, and its runner starts in resume mode (`docs/kubernetes.md` §8.3).

### 5.5 Ending

The run ends when the session finishes (`completed`), the runner reports an error or the pod
ends without an outcome (`failed`), or the admin cancels (`cancelled`: the operator deletes
the pod with its 30-second grace period). After the session the runner runs the delivery check
and reports its warnings with the outcome. The worker uploads the transcript, publishes
`task.result`, revokes the token and deletes the Run, which removes the pod, the workspace and
the Secret.

A cancelled run that was `waiting_for_input` also frees its pod and workspace. There is no
automatic expiry of waiting runs; the admin panel lists them.

## 6. Data model

```
projects          id, slug, name, image, setup_commands[], created_at
repositories      id, project_id, name (folder), url, branch, auth_secret_id, push_allowed
env_vars          id, project_id, key, value
secrets           id, project_id (nullable = global), key, value_encrypted
connections       id, slug, name, transport (stdio|http), command, args[], url, env (json, may reference secrets by name)
agents            id, slug, name, prompt, runtime (enum), provider_id (nullable = default),
                  identity_name, identity_email, created_at
providers         id, slug, name, kind, auth_method, credential_encrypted, settings (json),
                  is_default, last_test (json), created_at, updated_at
agent_connections agent_id, connection_id
project_agents    project_id, agent_id, connection_overrides (json), secret_overrides (json)
tasks             id (user task_id), project_id, agent_id, instructions, context (json), correlation (json), state, received_at
attachments       id, task_id, name, content_type, object_key
runs              id, task_id, state, runtime_session_id, run_object, started_at, ended_at, error
info_requests     id, run_id, question, context, created_at, answered_at, answer, answer_attachments (json)
events            id, task_id, run_id, type, payload (json), published_at
transcript_chunks id, run_id, seq, payload (json)
artifacts         id, run_id, name, content_type, object_key
admin_user        id, username, password_hash
workers           id, hostname, started_at, last_seen_at, concurrency, runtimes (json),
                  operator (json), version
project_verifications  id, project_id, state, created_at, started_at, ended_at, error,
                  steps (json), worker_id, commit, diagnosis, token_hash, tool_reports (json)
provider_tests    id, provider_id, state, created_at, started_at, ended_at, detail, error,
                  fingerprint, worker_id, token_hash, report (json)
```

The project-side tables above were replaced on 2026-11-07 by config repositories (`docs/config-repo.md` §8); §12 "Config repositories" has the current model.

Secrets and provider credentials are encrypted with a key from the Exradius environment. They are decrypted only when a Run is created (the project's own secret variables, into its per-run Secret), for the run spec, or for the egress proxy (the provider's, the repositories' and the HTTP connections' credentials, which never reach a pod; `docs/kubernetes.md` §10, §11), and never returned by the admin API.

## 7. Runtime interface

```python
class Runtime(Protocol):
    name: str
    label: ClassVar[str]
    capabilities: ClassVar[RuntimeCapabilities]      # provider kinds and auth methods it accepts
    credential_variables: ClassVar[frozenset[str]]   # every variable a provider mapping may set

    @classmethod
    def provider_environment(cls, provider: ProviderCredential) -> dict[str, str]: ...
    async def probe(self) -> ProbeResult: ...        # "Reply with OK", for provider tests
    async def start(self, spec: RunSpec, events: EventSink) -> RunOutcome: ...
    async def resume(self, spec: RunSpec, session_id: str, message: str,
                     events: EventSink) -> RunOutcome: ...

@dataclass
class RunSpec:
    run_id: str
    working_dir: Path
    state_dir: Path
    prompt: str                         # the first user message: the ticket
    connections: list[ConnectionSpec]   # includes the built-in exradius server
    env: dict[str, str]
    system_prompt: str = ""             # appended to the runtime's own (§13)

@dataclass
class RunOutcome:
    status: Literal["completed", "waiting_for_input", "failed"]
    session_id: str | None
    final_message: str | None
    error: str | None
```

`ClaudeCodeRuntime` implements this with the Claude Agent SDK. A second runtime is a new class and an enum value; nothing else changes.

## 8. Admin panel

Version one scope:

- CRUD for projects, repositories, environment variables, secrets (write-only), connections, agents, and agent-to-project attachments with overrides.
- "Test connection" for a connection: start it and list its tools.
- Task list with state and filters; task detail with transcript viewer, info requests and answers, result and artifacts.
- Cancel a run. Retry is out of scope (a task has one run).
- Single admin login, session cookie. The schema has a user table so roles can come later.

## 9. Security notes

- Agents get exactly the connections attached to them. Capability is tool availability; there are no per-action permission flags.
- Connections that reach production systems (logs, databases) should be configured with read-only credentials. The panel displays a reminder on the connection form; it does not enforce it.
- **Pod security** (`docs/kubernetes.md` §7.8). The operator writes it, and a Run cannot change it: the run user (uid 1000), a read-only root filesystem with `/work`, the home, `/tmp` and the state directory on the run's workspace, no capabilities, no privilege escalation, no ServiceAccount token, no service links. Services run as their image's user with a small capability set. Run pods use user namespaces by default (`runs.userNamespaces`: every uid of the pod, a service's root included, is an unprivileged uid on the node); an install may instead choose a sandbox runtime (`runs.runtimeClassName: gvisor`: the pod's processes talk to gVisor's user-space kernel, not the node's). Build pods run at Pod Security `baseline` with the node-installed seccomp (and AppArmor) profiles of `deploy/`, or `privileged` without them (§12 there).
- **NetworkPolicy** (`docs/kubernetes.md` §11). A run pod reaches the cluster DNS, the egress proxy and the api's internal port, nothing else: not the Kubernetes API, not Exradius's Postgres or RabbitMQ, not another run, not the internet directly. Build pods likewise. The api's internal port takes run, build and proxy pods only.
- **The egress proxy** (`docs/kubernetes.md` §11). Every request of a run goes through it. Setup (clones, services, setup commands) reaches any public host; the agent reaches only its model, its repositories, its connections, the config's `network.allow` and the install's `egress.allow`, and a refusal is a `403` naming the host. Private and cluster-internal addresses are refused in every phase unless the install allows them. Every request is recorded (`egress_records`), and the task page lists the hosts reached and refused.
- **Credential injection.** No pod holds the model credential, a repository credential or an HTTP connection's secret header: the proxy adds them on its routes, and refuses a push to a repository without `push: true`. The pod holds a placeholder (`ANTHROPIC_API_KEY=placeholder`), `ANTHROPIC_BASE_URL` at the proxy, and git `insteadOf` rewrites. What a pod still holds: the project's own secret variables, a stdio connection's secret environment (the server process needs it; a known gap) and its run token, which the agent cannot read (above).
- **DNS** is the one channel the proxy does not see: a run resolves names through the cluster DNS, so an agent can leak data in query names. Decided, not built: the proxy would answer run pods' DNS itself (`docs/kubernetes.md` §22).
- The runtime bundle is mounted read-only and is world-readable and executable, so the unprivileged run user runs it whatever users the project image defines. The project image cannot change the runtime, and the runtime does not depend on anything in the image except glibc and `/bin/sh`. Setup commands run without the bundle.
- The internal run API accepts only the per-run token, which is revoked when the run ends; its `/internal/egress/*` routes accept only the proxy's ServiceAccount token, checked with a TokenReview.

## 10. Repository layout and stack

One repository (`docs/kubernetes.md` §18):

```
exradius/
  backend/            Python 3.14, uv, FastAPI, SQLAlchemy + Alembic, aio-pika, httpx
    exradius/
      api/            admin API (8000) + internal API (8001)
      worker/         broker consumer, Run objects and settling, Verify, provider probes
      runner/         the run pod's process (inside the runtime bundle): prepare, clone,
                      files sidecar, session, exradius MCP server, delivery check
      runtimes/       Runtime protocol, ClaudeCodeRuntime
      models/, schemas/, messaging/
    tests/
  operator/           Go module (controller-runtime): api/v1alpha1 (Run), internal/podspec,
                      internal/status, internal/controller, cmd/
  charts/exradius/    the Helm chart; the CRD in files/crds/ is generated from operator/
  frontend/           React + TypeScript + Vite, served by nginx
  deploy/
    backend.Dockerfile               exradius-backend: api and worker (no Node, no Agent SDK)
    operator.Dockerfile              exradius-operator
    runtime-claude-code.Dockerfile   exradius-runtime-claude-code: the Claude Code runtime bundle
    runtime-claude-code/             the bundle's launcher script
    project-default.Dockerfile       exradius-project-default: image for projects without one
    kind/                            the local cluster: make dev-up, dev-down, images, e2e
    Makefile                         make bundle-claude-code: the bundle directory alone
  Tiltfile            the developer loop on kind
  go.work
  docs/
```

## 11. Deferred

Recorded so they are not re-debated:

- run limits (turns, tokens, time) and automatic expiry of waiting runs;
- retries and multiple runs per task;
- persistent per-project cache directories to speed up setup commands;
- per-project API keys;
- roles and multiple admin users;
- external broker support beyond a connection string.

Multi-host workers came with Kubernetes: runs are pods the scheduler places on any node, and
any worker replica can settle any run (`docs/kubernetes.md`).

## 12. Implementation notes

Details the design left open, and the simplest choice made for each in version one.

Since 0.2.0 Exradius runs on Kubernetes (`docs/kubernetes.md`), in phases
(`docs/kubernetes-plan.md`). Phase 1 runs image-based projects; phase 2 adds Dockerfiles,
built as `Build` objects (`docs/kubernetes.md` §12); phase 3 adds services as sidecars, the
project volume of mirrors and caches, and Verify's service steps, so every valid config
runs again (`capabilities.py` blocks nothing). The notes below describe 0.2.0; where v0.1.0
did it with Docker, the Kubernetes section is named instead.

**Layout and images**

- The package lives at `backend/exradius/` as in §10. Shared modules sit next to the listed folders: `config.py` (`EXRADIUS_*` settings), `crypto.py`, `states.py` (the §5 state machine), `resolve.py` (secrets and connections), `storage.py`, `db.py` and `cli.py`.
- Five images. `exradius-backend` runs the api and worker; it has git, but no Docker SDK, no Node and no Agent SDK (the `runner` dependency group is not installed), and runs as uid 1000. `exradius-operator` is the Go operator (distroless, uid 65532). `exradius-frontend` is nginx with the panel. `exradius-runtime-claude-code` holds only the Claude Code bundle (`docs/runtime-bundles.md`). `exradius-project-default` is the workstation image of projects that name none. Tilt builds them all locally; `make -C deploy/kind images` builds and pushes them with the version tag. The chart passes the bundle and default images to the worker as `EXRADIUS_RUNTIME_IMAGE_CLAUDE_CODE` and `EXRADIUS_PROJECT_DEFAULT_IMAGE`.
- A stdio connection in a run needs its command in the workstation image; the default image has neither `npx` nor `uvx`, which the config check reports as a warning.
- The object store is any S3 API through the `EXRADIUS_S3_*` variables: SeaweedFS in a local install (from the chart; MinIO's community edition is no longer maintained), S3 in AWS. Keys are validated before use: no empty, `.` or `..` segments, no leading `/`, backslash or NUL. The filesystem and in-memory stores are for tests only. There is no data directory any more: config-repository mirrors live in an `emptyDir` cache of the api and worker pods (`EXRADIUS_CACHE_DIR`) and are fetched again after a restart.
- Download URLs in `task.result` are signed with an HMAC-SHA256 of the URL path, keyed by a value derived from `EXRADIUS_SECRET_KEY`; a key in `EXRADIUS_SECRET_KEY_PREVIOUS` still verifies (kubernetes.md §10.1). `GET /api/artifacts/{id}/content` and `GET /api/runs/{run_id}/transcript` accept a valid `sig` or the admin session. A signature is bound to its path and does not expire in version one. The transcript endpoint serves the stored JSON lines file once the run has ended, and assembles it from the stored chunks while the run is still going. Behind a reverse proxy that adds a path prefix, include the prefix in `EXRADIUS_PUBLIC_API_URL`; the signature covers only the API path.

**Data model additions** (beyond §6)

- `runs`:
  - `token_hash`: sha256 of the current run token.
  - `reported_status`: the outcome the runner reported.
  - `summary` and `output`: the result fields.
  - `resume_request_ids`: the answers delivered by the current resume.
  - `run_object`: the name of the run's `Run` (`run-<run id>`) once the worker has created it, so recovery can tell a Run that disappeared from one never created. It replaced `container_id`, `directory` and `worker_id` (migration `0006`).
  - `delivery_warnings`: what the runner's delivery check found, for `task.result`.
  - `waiting_for`: `capacity` while the run's pod cannot be scheduled (the run stays `preparing`).
- `info_requests`:
  - `published_at` and `delivered_at`.
  - `late_answers`: answers after the first, which are stored but not delivered.
- `attachments.source_url`: for URL attachments.
- `workers` and `project_verifications`: see the onboarding additions below. These tables were added for `docs/onboarding.md` §4 and are not part of §6.
- `events.created_at`.
- `events.task_id` is not a foreign key, because rejected tasks are not stored in `tasks` (see below).
- JSON columns use JSONB on Postgres, except `projects.snapshot` and `runs.config`, which are plain `json`: JSONB reorders object keys, and a config's services, env, connections and agents keep the file's order (services start in that order).
- The tables of §6 that described a project in the panel (`repositories`, `env_vars`, `secrets`, `connections`, `agents`, `agent_connections`, `project_agents`, `project_services`) are gone; see "Config repositories" below for what replaced them.

**Intake and the broker**

- A rejected task is not inserted into `tasks`. Its `task.status` event (`state: rejected`, `run_id: null`, with a `reason`) is stored in `events` and published. A duplicate `task_id` does not touch the existing task.
- Worker concurrency is `EXRADIUS_WORKER_CONCURRENCY` (values `worker.concurrency`). It is both the prefetch count of the task consumer and the number of run slots: a slot is held while a run of this worker prepares, runs or resumes, and given back while it waits for input. A task message is persisted and acknowledged only when a slot is free, so the broker holds the backlog. Answers and control messages use a separate channel.
- Base64 attachments are uploaded to the object store at intake, before the ack. URL attachments are downloaded while the run is preparing.
- The worker is the only publisher of outbound events. Each event is first stored in `events`, then published with AMQP `message_id` = `events.id`. If publishing fails, the row keeps `published_at = NULL` and the run continues. Version one does not republish.
- Event timestamps are UTC with second precision (`2026-11-05T19:12:03Z`).
- `result.output` is the JSON value passed to `submit_result`, or `null`. It is not wrapped in an object. `summary` is the final assistant message (the SDK `ResultMessage.result`).
- Admin cancel travels from the API to the workers over an internal durable queue, `exradius.control`. That queue is not part of the public contract in §4. Any worker can handle it. The same queue carries `{"action": "run_reported", "run_id"}` when the runner reports through `/reported` (below), and Verify and provider-test requests.

**Run lifecycle** (`worker/runs.py`, `docs/kubernetes.md` §4, §8)

- Every transition locks the run row (`SELECT … FOR UPDATE OF runs`), updates both run and task, and records the status event in the same transaction.
- `RunManager.settle(run_id)` is the one place that turns what happened into transitions. It reads the Run (or takes it from a watch event) and the run row under its lock: Run status `Running` moves `preparing` to `running`; a final status (`Succeeded`, `Failed`) ends the run, as `completed` when the runner reported that, else `failed` with the failed step's log tail, the operator's message or "runner exited with code N without reporting an outcome"; `resuming` with a report moves to `running`; `reported_status = waiting_for_input` moves `running` to `waiting_for_input` and publishes the pending `task.info_request` events. It is idempotent, and called for every event of the worker's watch on Runs, for every `run_reported` control message, and by a sweep of every unfinished run every 15 s, so a lost event or message only delays.
- Finalisation happens exactly once, guarded by `runs.ended_at IS NULL`. It stores the transcript under the key `runs/<run_id>/transcript.jsonl`, publishes `task.result` (with the delivery warnings), revokes the token and deletes the Run; garbage collection removes the pod, the workspace PVC and the Secret. Cancel, failure and completion all go through it, so a cancel racing with a pod's end produces one result.
- The runner never changes state. It reports to the internal API: `/reported` (`waiting`, `resumed`) while it stays alive, `/outcome` once at the end. These are seconds apart from the call, because the runner interrupts the session right away.
- A run waits on its first outstanding question. All questions answered by the time it resumes are delivered together in one message. An answer that arrives before the run reached `waiting_for_input` is picked up as soon as it does.
- Cancel: the worker moves the run to `cancelled` and sets `spec.cancel: true`; the operator deletes the pod (30 s grace); the runner, on SIGTERM, stops the session, runs the delivery check and reports; the worker finalises once the Run says the pod is gone, or after 90 s if no operator answers. The run token stays valid until finalisation, so the cancelled runner can still report.
- A pod lost while the run waits (deleted by hand, node gone) is recreated when the answer arrives: the worker bumps `spec.podGeneration` under the run-row lock, and the operator creates a new pod on the same workspace whose runner starts in resume mode. A pod lost while the run is running fails it ("the run's pod was lost: …").
- Crash recovery runs when a worker starts, before it consumes any message. Run objects survive the worker, so any worker can settle any run (§4 of `docs/kubernetes.md`):
  1. The worker lists the Runs and settles every unfinished run (`ended_at IS NULL`, not `queued`) from its Run. An unfinished run without one fails with "the run's Kubernetes object disappeared". A run in `preparing` whose Run was never created fails with "the worker restarted while preparing the run" when no other worker sent a heartbeat in the last minute, or once it has been preparing for 30 minutes. Runs in `preparing`, `running` and `resuming` take a slot of the new worker.
  2. A Run whose run has ended is deleted, as are verify and probe Runs whose verification or test is no longer open.
  3. Waiting runs whose answer arrived while no worker could act on it are resumed.

  Waiting runs are otherwise left alone, and §5.5's rule of no automatic expiry still holds. Verifications and provider tests held by a worker that died are failed at start; their ownership still uses the advisory-lock presence (`worker/presence.py`). Without Postgres (SQLite in tests), a worker assumes it is the only one.
- Setup commands (`workstation.setup`) run as init containers `setup-<n>` of the run's pod, from the workstation image, with `/work` and the workstation environment (the config's `env` and the exported secrets, see "Config repositories"). A non-zero exit fails the pod, and the step's log tail becomes the run's error. They do not run again when the session resumes in the same pod; they do run when a lost pod is recreated.

**Run pods and workspaces** (`docs/kubernetes.md` §5, §7)

- The worker builds the Run spec in `worker/run_objects.py`: it is secret-free, and every value that holds a secret is a key of the per-run Secret (`token`, `env.<NAME>`, `service.<name>.<NAME>`). There is no provider or repository credential in it: the egress proxy adds those (`docs/kubernetes.md` §10, §11). The Secret is created right after the Run with an ownerReference to it, so it goes with the Run.
- The operator writes the pod's security itself. Exradius's own containers (prepare, clones, setup, the files sidecar, the workstation) get the run uid and gid (`runs.user`, default `1000:1000`), `runAsNonRoot`, a read-only root filesystem, no privilege escalation and all capabilities dropped; the run user is set per container, not on the pod, because services keep their image's user (`docs/kubernetes.md` §7.4). The pod has seccomp `RuntimeDefault`, no ServiceAccount token, no service links, `fsGroup` with `OnRootMismatch`, `supplementalGroupsPolicy: Strict` and a 30-second grace period. The Run spec cannot change any of it.
- The workspace is one PVC per Run (`runs.workspace.size`, default 50 Gi): `work` at `/work` (attachments read-only at `/work/.exradius/attachments`), `home` at `/home/runner`, `tmp` at `/tmp` (1777) and `state` at `/exradius/state`. `ClaudeCodeRuntime` sets `CLAUDE_CONFIG_DIR=/exradius/state/claude`, so session files survive a recreated pod. The working directory is always `/work`. Removing a workspace is deleting the PVC; there is no Exradius code for read-only trees, sockets or deep `node_modules` any more.
- `prepare` (the runner, as the run user) creates the layout on the first start of a pod and writes the task's attachments, fetched from `GET /internal/runs/{id}/attachments/{id}`, with mode `0444`. URL attachments are downloaded into the object store by the worker before it creates the Run. Answer attachments are written by the `files` sidecar (run uid + 1, the run's group) into `.exradius/attachments/<request_id>/` with mode `0444` as they are delivered; the runner resumes once they are there. Steps leave markers in `state/steps/`, so a recreated pod skips `prepare` and the clones.
- `clone-<n>` refreshes the repository's bare mirror on the project volume (`mirrors/<folder>.git`, under a file lock next to it, which serialises concurrent runs of the project), then clones it with `git clone --no-hardlinks --branch <branch>` (the remote's default branch without one) and resets `origin` to the plain URL. Without a project volume (`runs.projectVolume.enabled: false`) it clones straight from the remote. No credential is in the container: each command gets `-c url.<proxy route>.insteadOf=<url>` (`git.insteadof`), so git asks the egress proxy's git route (`/git/<host>/<path>`), which adds the repository's `credential` as HTTP Basic (a bare token becomes `x-access-token:<token>`). The mirror and the clone keep the plain URL as `origin`.
- The run token is generated when the worker creates the Run, kept hashed on the run (`runs.token_hash`), carried by the per-run Secret, and revoked at finalisation. It is never a variable: `prepare` and `files` read it from the Secret mounted as a file, and `prepare` hands a copy to the workstation through an in-memory volume, which the runner deletes once read. The runner is then non-dumpable, so the agent, which runs as the same uid, can read neither the file nor the runner's memory or `/proc/1/environ` (`docs/kubernetes.md` §7.8).
- The api's internal port (8001, Service `exradius-api-internal`) serves only `/internal/*`; port 8000 refuses those paths. New routes for run pods: `POST /internal/runs/{id}/reported`, `GET /internal/runs/{id}/answers?wait=` (long-poll, up to 60 s), `GET /internal/runs/{id}/attachments` and `/attachments/{id}` (a task attachment's id, or `<request id>.<n>` for an answer attachment); `/outcome` gains `warnings` and the status `cancelled`. Verify Runs use `/internal/verifications/{id}/spec` and `/tools`, probe Runs `/internal/probes/{id}/result`, each with its own token (`project_verifications.token_hash`, `provider_tests.token_hash`).

**Prompt and runtime**

- `# Context` is omitted when `context` is null, and `# Attachments` when there are none. The run spec already carries `agent_prompt` apart from the ticket, so the internal API did not change. The runner builds both parts (`build_system_prompt`, `build_task_message` in `exradius/runner/prompt.py`). An empty agent prompt leaves the rules alone in the append. A test checks that `OPERATING_RULES` matches the block in §13 word for word.
- The resume message reads "Answer to your question: <question>", then the answer, then the paths of any answer attachments.
- `ClaudeCodeRuntime` uses these SDK options:
  - the `claude_code` system prompt preset, with `append` set to the operating rules, the environment section and the agent's prompt (§13). Resumed sessions get the same system prompt;
  - `model` when the agent names one (otherwise the CLI's default);
  - `permission_mode="bypassPermissions"` (capability equals the tools attached);
  - `strict_mcp_config=True`.

  It interrupts the session once `request_information` has been called, and resumes with the SDK `resume` option.
- The built-in `exradius` server is registered with `alwaysLoad: true`, so its tools are always in the prompt and never deferred behind Claude Code's tool search.
- Each SDK message is stored as one transcript chunk, a JSON object with a `type` field. Sequence numbers continue across the sessions of a run and across a recreated pod (an offset file in the state directory).

**Configuration resolution**

- Every `${secret:NAME}` resolves from the secret store the config names (`secret_store`); there are no project or global secrets, overrides or fallbacks. `exradius/resolve.py` substitutes at Run creation (worker) and for the run spec (API); a missing key raises `MissingSecret` ("missing secret NAME in store S").
- The connection name `exradius` is reserved for the built-in server.
- The model credential comes only from a provider (`docs/providers.md`).

**Admin**

- The admin user is created at API start from `EXRADIUS_ADMIN_USERNAME` and `EXRADIUS_ADMIN_PASSWORD`, only when no admin user exists. The variable never changes an existing password. `POST /api/auth/password` (`current_password`, `new_password` of at least 8 characters) changes it and answers 204, or 400 if the current password is wrong.
- Passwords are hashed with scrypt.
- The session is a signed cookie (`exradius_session`).
- Secret store values and git host credentials are encrypted with Fernet using `EXRADIUS_SECRET_KEY`. The API accepts and replaces their values but never returns them. Keys in `EXRADIUS_SECRET_KEY_PREVIOUS` only decrypt, and `exradius rotate-key` re-encrypts every stored value with the primary (kubernetes.md §10.1).

**Onboarding additions** (`docs/onboarding.md` §4)

- **Heartbeats.** Every 15 s each worker upserts its row in `workers`: hostname, concurrency, version, the runtime bundles it is configured with (`runtimes: [{name, version, ready, image, error}]`; pulling them is the nodes' business) and the operator as its leader Lease shows it (`operator: {alive, holder, renewed_at, detail}`, alive when the Lease `exradius-operator` was renewed within its duration plus 10 s). A worker deletes its row when it shuts down cleanly. `GET /api/system/status` lists only workers seen in the last 60 s. The table is for reporting, and for one recovery question: whether another worker is alive. `GET /api/system/images` is gone: there is no daemon whose images could be listed.
- **System status.**
  - The database check is `SELECT 1`.
  - The broker check is the API's own connection, which is shared with publishing.
  - `provider` reports the default provider and the number of providers (see Providers below). The object store and the Anthropic key are no longer reported.
  - `operator` is the most recently seen live worker's view of the operator; `null` while no worker reported one.
- **Integration info.** Username, vhost and whether a password is set come from `EXRADIUS_BROKER_URL`; the password is never returned. User name and password may instead come from `EXRADIUS_BROKER_USERNAME` and `EXRADIUS_BROKER_PASSWORD`, which replace the URL's user info (the chart takes them from the RabbitMQ operator's generated Secret). Host and port come from `EXRADIUS_PUBLIC_BROKER_HOST` and `EXRADIUS_PUBLIC_BROKER_PORT` (values `broker.publicHost`, `broker.publicPort`), because the API's own broker URL names the in-cluster Service.
- **Verification.**
  - `POST /api/projects/{id}/verify` stores a `pending` row and publishes `{"action": "verify"}` on `exradius.control`. If the broker cannot be reached, it answers 503 and marks the row failed.
  - The worker that takes the message claims the row (`worker_id`) before acknowledging it. The job then occupies one of the worker's run slots.
  - The first step is "Read config" (commit, file and branch, errors, warnings, missing keys, and what this version cannot run yet); it fails when the project is not runnable, and nothing else runs. Once it passes, the worker creates a verify Run (`verify-<id>`, `docs/kubernetes.md` §13), the same pod as a task's, and records its steps as `pending`: "Prepare workspace", "Clone <repo> (<branch>)" for each repository, "Setup: <command>" for each setup command, and "List tools <name>" for each connection an agent uses. Steps therefore appear after the verification started. The worker polls the Run every second and mirrors its step states and log tails (up to 4000 characters) into the verification; a pod waiting for room shows "waiting for cluster capacity: <scheduler message>" on the first step. The verify pod's runner (`exradius-runner --verify`) lists each connection's tools after setup and reports them through `/internal/verifications/{id}/tools`. After a failure the remaining steps are `skipped`. At the end the Run is deleted, which removes everything the verification made: there are no cleanup steps.
  - At startup, verifications claimed by a worker that is no longer alive are failed and their Runs deleted.
  - Setup commands see the workstation environment, exactly as in a run.
- **Sending from the panel.**
  - `POST /api/tasks` validates the body with the task schema, where `version` may be omitted, and publishes it on `exradius.tasks`.
  - `POST /api/tasks/{task_id}/answer` builds the answer message and publishes it on `exradius.answers`.
  - Both answer 202 with the exact message published, or 503 if the broker cannot be reached.
  - Neither checks that the project, agent, task or request exists. The worker answers exactly as it does for an integration, for example with a `rejected` status event.
- **Events.** `GET /api/tasks/{task_id}/events` returns the stored events of that task id oldest first, including rejections of duplicates that reused it. `GET /api/events` returns the latest `limit` events (default 100) oldest first. With `since`, it returns the first `limit` events created after that time.
- **Stream.** `GET /api/tasks/{task_id}/stream` (`text/event-stream`) first replays everything stored for the task, then polls Postgres about once a second.
  - Events are sent as `event: status | info_request | result`, where `data` is the stored envelope and `id` the event id. Transcript chunks are sent as `event: transcript`, with `data` `{run_id, seq, payload}`.
  - Each poll re-reads a 5-second window behind its cursor and skips ids already sent, so an event committed out of order is still delivered.
  - A `: heartbeat` comment is sent after 15 s of silence.
  - The stream ends after the `result` event, or after a `rejected` status for a task id that was never stored. A task that does not exist yet is waited for, because the panel opens the stream right after sending.

**Providers** (`docs/providers.md`)

- **Model.** `providers` holds named credential profiles. `credential_encrypted` is Fernet-encrypted like secrets and never returned. A partial unique index on `is_default` allows at most one default. The API makes the first provider the default and moves the flag with `POST /api/providers/{id}/default`, which clears the old default before setting the new one. An agent names a provider by its `slug` in the config (`agents.<name>.provider`; absent uses the default). The design asked for a unique slug-like `name`; providers already had a unique `slug`, so that is what configs name, and `name` stays the display name.
- **Rules.** `kind`, `auth_method` and `settings` are validated against the auth methods the runtimes declare: unknown setting names and missing required ones are refused. Changing the kind or auth method needs a new credential, and agents whose runtime cannot use the new pair block the change. `last_test` is cleared when the credential, settings, kind or auth method change, but not on a rename. Delete answers 409 while agents name the provider ("used by agents bank/developer; …"), or while it is the default and others exist. The last provider can be deleted. `agent_count` counts the agents that name the provider, plus, for the default, the agents without a provider of their own. Agents are counted from each project's latest read config (`projects.snapshot`), so usage follows a re-read.
- **Capabilities.** Each runtime class declares `capabilities` (provider kinds, auth methods, their settings fields and notice), and `GET /api/system/runtimes` returns them. `ClaudeCodeRuntime` accepts `anthropic` with `api_key` and `oauth_token`.
- **Injection** (`docs/kubernetes.md` §10, since phase 4). When it creates the Run, the worker resolves the agent's provider or the default and records it (`runs.provider_id`); the Run spec names the runtime's mapping for the workstation only, with no secret: `ANTHROPIC_BASE_URL` at the egress proxy's model route (`<egress>/anthropic`) and `ANTHROPIC_API_KEY=placeholder` for `api_key`, `CLAUDE_CODE_OAUTH_TOKEN=placeholder` for `oauth_token`. The proxy reads the provider's credential from the api (`model_route`) and sends it as Claude Code would: `x-api-key` for a key, `Authorization: Bearer` for a subscription token. Sessions get `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, because Claude Code's telemetry and update checks go to Anthropic's hosts directly, which the agent phase refuses. Project variables and secrets named like any provider variable of the runtime (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_BASE_URL`) are dropped with a warning, so the run gets only its provider's mapping. Setup containers get no provider. Intake rejects a task with `no provider configured` when the agent names none and there is no default, and with `unknown provider 'x'` when it names a slug no provider has. A run whose provider has disappeared by the time its Run is created fails with the same message.
- **Tests.** `POST /api/providers/{id}/test` for `api_key` calls `GET https://api.anthropic.com/v1/models` from the API container (20-second timeout) and answers 200 `{ok, detail, error}`, with up to five model ids in `detail`. For `oauth_token` it stores a `pending` row in `provider_tests`, publishes `{"action": "test_provider", "test_id"}` on `exradius.control` and answers 202 `{test_id}` (503, with the row failed, if the broker is down). The worker claims the row before acknowledging and, in a run slot, creates a probe Run (`probe-<id>`, `docs/kubernetes.md` §13): the default project image with the runtime bundle, `emptyDir`s instead of a workspace, only the provider's variables (from the per-run Secret), running `exradius-runner --probe claude_code --report <id>`. That is a one-turn Claude Code session with no tools, sent "Reply with OK", with a 180-second limit. The probe posts `{ok, detail, error}` to `/internal/probes/{id}/result` (and prints it as an `EXRADIUS_PROBE` line); the worker records it on the test, or the workstation's log tail when the pod ends without a report, and deletes the Run. `GET /api/providers/{id}/tests/{test_id}` reports it. Both kinds of test store `last_test`, but only if the provider's credential and settings are unchanged since the test began (`fingerprint`). Tests held by a worker that died are failed at startup.
- **Migration.** At API start, when no provider exists, `EXRADIUS_ANTHROPIC_API_KEY` becomes the default provider "Anthropic (migrated)" (`api_key`), and a warning is logged. After that it is not read. (The migration from a global secret `ANTHROPIC_API_KEY` went with the secrets table.)

**Runtime bundles** (`docs/runtime-bundles.md`)

- **The bundle.** `deploy/runtime-claude-code.Dockerfile` builds `/opt/exradius/runtime` in a Debian stage and copies it into a `scratch` image labelled `exradius.role=runtime-bundle`, `exradius.runtime=claude_code` and `exradius.version`:
  - `python/`: a relocatable CPython 3.14.8 from python-build-standalone (installed by uv), which needs glibc 2.17 or later. The `exradius` package goes in without its server dependencies, plus the `runner` dependency group: the Agent SDK, `httpx` and `pydantic`. Compiled wheels are limited to `manylinux_2_28`, so project images need glibc 2.28 or later. The SDK's own copy of the CLI is deleted.
  - `bin/claude`: native Claude Code 2.1.318, downloaded from `downloads.claude.ai/claude-code-releases` (the installer's source) and checked against the release manifest's SHA-256 for `linux-x64` or `linux-arm64`. That is the version Agent SDK 0.2.178 bundles; the SDK pin and this pin move together.
  - `bin/exradius-runner`: a `/bin/sh` launcher with no `PATH` lookups. It sets `EXRADIUS_RUNTIME_DIR` to its own bundle directory, then runs `python/bin/python3 -I -B -m exradius.runner.main`. `-I` keeps the project image's Python, `PYTHON*` variables, user site and `/work` out of the runner. The launcher sets no `DISABLE_AUTOUPDATER`, because that would reach the agent (see Isolation). The CLI is started by path from a read-only bundle, so an update could not take effect anyway.
  - `VERSION`: `exradius <version>`, `runtime claude_code`, `claude-code <version>`.
  - `ClaudeCodeRuntime` passes `$EXRADIUS_RUNTIME_DIR/bin/claude` as the SDK's `cli_path`. Outside a bundle (tests, development) it leaves `cli_path` unset.
  - The packaging is python-build-standalone plus a launcher rather than PyInstaller: nothing has to be frozen, and the SDK and `mcp` load exactly as in development. `make -C deploy bundle-claude-code` exports the directory to `deploy/build/runtime-claude-code/`.
- **Mounting.** Run pods mount the bundle image named by `EXRADIUS_RUNTIME_IMAGE_<RUNTIME>` as an image volume at `/opt/exradius/runtime`, read-only, in `prepare`, `clone-*`, `files` and the workstation, not in setup containers (`docs/kubernetes.md` §7.2). A rebuilt bundle is a new image, so a running pod never sees it change. There is no extraction and no volume cleanup any more.
  - Setup containers get `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=safe.directory` and `GIT_CONFIG_VALUE_0=*`, and the workstation gets it as the first of its command-line git config. Without it, git in a stock image can reject a checkout that it does not see owned by the run user.
- **Isolation** (runtime-bundles.md §5, `exradius/runner/environment.py`). The runner's first act is `isolate()`:
  - It removes `EXRADIUS_RUNTIME_DIR` (the launcher's) and `EXRADIUS_RUN_TOKEN_FILE` from its process, reads the token from that file and deletes it when it can, and keeps a copy of everything else as the container's environment. Before that, `protect_process()` makes the runner non-dumpable.
  - It then removes `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `VIRTUAL_ENV`, `NODE_OPTIONS` and `NODE_PATH` from its own process only.
  - `RunSpec.env` is that copy. `ClaudeCodeRuntime` passes it, plus `CLAUDE_CONFIG_DIR`, as the SDK's `env`. The SDK starts the CLI with the runner's cleaned environment overlaid by `env`, so the CLI, and the agent's shell and stdio MCP servers, get exactly the container's environment (the project's variables and secrets, the provider's placeholder and base URL, the proxy variables) plus `CLAUDE_CONFIG_DIR` and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`. `isolate()` also takes `EXRADIUS_EGRESS_URL` and `EXRADIUS_EGRESS_PEERS` out of it: the runner's own route to the proxy.
  - The only other variables are the SDK's own markers (`CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_AGENT_SDK_VERSION`, `CLAUDE_CODE_SDK_READS_SESSION_STATE`, `PWD`).
  - The bundle is never on `PATH`, so `python`, `node`, `cargo` or `mvn` are the project image's.
  - `tests/test_runner_environment.py` checks this through the real SDK transport against a fake CLI that records its environment. A Docker test checks that a project `PYTHONPATH` holding an `exradius` package, `sitecustomize.py` and `json.py` does not affect the bundle's runner.

**Agent identity** (`docs/agent-identity.md`, `exradius/worker/git_identity.py`)

- **Model.**
  - The identity is the config's `agents.<name>.identity`: `name` defaults to the agent's name, `email` to `<agent>@<project slug>.exradius.local`.
  - Both are validated as git idents: no angle brackets, no control characters, and an `x@y` email.
  - `repositories[].push` (default false) needs a `credential`; a file that would push without one is a config error.
- **In the Run spec's `git.env`** (the workstation only) the worker puts:
  - `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, `GIT_COMMITTER_EMAIL`, `EXRADIUS_AGENT` (slug) and `EXRADIUS_TASK_ID`;
  - `GIT_CONFIG_GLOBAL=/exradius/state/git/config`. These override project variables of the same names.
  - Setup containers and the provider probe get none of this: they have no agent or task.
- **Global git config in the state mount.**
  - The run's global config lives at `<state>/git/config`, not `$HOME/.gitconfig` (the home also persists, but belongs to the agent and its tools), named by `GIT_CONFIG_GLOBAL` (git 2.32 or later; Ubuntu 22.04 and Debian bookworm images have it). With older git, only the identity variables apply.
  - The runner creates `config` once with just `[include] path = exradius.gitconfig`, and rewrites `exradius.gitconfig` at every start (`runner/gitconfig.py`). So the agent's own `git config --global` changes survive resume, and the managed settings follow the current agent and repositories.
  - `exradius.gitconfig` sets `user.name`, `user.email`, `push.autoSetupRemote=true` and `init.defaultBranch=main`. All values are quoted. `core.hooksPath` and the credential helpers are command-line config (see "Git at the command-line level").
- **Commit trailers.** The bundle's `git-hooks/prepare-commit-msg` (`/opt/exradius/runtime/git-hooks`, read-only, so the agent cannot edit it) runs `git interpret-trailers --if-exists doNothing` for `Exradius-Task: $EXRADIUS_TASK_ID` and `Exradius-Agent: $EXRADIUS_AGENT`, so amending or rebasing never duplicates them. Because `core.hooksPath` replaces the hooks directory, a repository's own hooks (and any husky-style setup) do not run in v1.
- **Push** (since phase 4, `docs/kubernetes.md` §11).
  - Each http(s) repository gets `url.<egress>/git/<host><path>.insteadOf=<url>` at the git command-line level, so fetch and push of `origin` (or any remote with that URL) go to the egress proxy's git route. The proxy adds the repository's credential and refuses `git-receive-pack` for a repository without `push: true`: git prints `fatal: remote error: pushing to <url> is not allowed for this task (push: false)` (an `ERR` packet in the ref advertisement).
  - No credential and no credential helper are in the pod; `git remote -v` shows the route, because git expands `insteadOf` there.
  - SSH remotes cannot go through the proxy and get no route; their clone and push fail (they had no credential support before either).
  - `tests/test_git_identity.py` commits in a prepared run directory with real git and pushes through a stand-in route that adds the credential; `egress/internal/proxy/reverse_test.go` clones and pushes through the real proxy against `git http-backend`, push refused for `push: false`.

**Environment section, services and model** (`docs/environment-and-services.md`)

- **Data model.** Model, notes and services now come from the config file (`agents.<name>.model`, `notes`, `services`; see "Config repositories"); only `provider_tests.model` (the model a worker-run provider test uses) is a column. There is no image probe: the design dropped the "Installed" line, so the agent finds its tools itself.
- **Environment section** (`exradius/environment.py`). `config_environment_text` renders the documented layout from the config: the workstation ("built from the image x", or "built from <dockerfile>" for a built one, the path from the repository root), its repositories in file order ("the default branch" when the file gives no `branch`), the fixed "Not available" line, its services in file order with their settings (a built service shows as `(built from <dockerfile>)`), the fixed closing paragraph and the notes (stripped; left out when empty). Repository URLs lose `user:password@`, the query and the fragment; scp-like `git@host:path` URLs are kept as written. "pushing allowed" means what the worker actually configures: `push`, a credential and an http(s) URL. With no repositories the second line reads "The working directory contains no repositories."
  - Service settings (`service_settings`): each env entry as `KEY=value`, plain values first, then those that use secrets, each group in stored order. A `${secret:NAME}` shows as `$NAME`, because the workstation exports every secret its services' settings use under its own name (`resolve.exported_secrets`); a name that is not a valid variable name (it has `.` or `-`) is not exported and shows as `<a secret you do not have>`. Secret values never appear. When any setting names a variable, the list ends with "A value written as $NAME is the value of your environment variable NAME."
  - The API builds the section for each run spec (`RunSpecResponse.environment_text`, so it is rebuilt whenever a runner starts, a recreated pod included) and for the resolved agent view (`ResolvedAgentOut.environment_text`, with `image` and `model`). `build_system_prompt` joins rules, environment and role with blank lines and skips empty parts.
- **Model.** `agents.<name>.model` in the config; absent is the runtime's default. Ids are validated loosely (`^[A-Za-z0-9][A-Za-z0-9._:@/\[\]-]*$`), so any id the provider accepts works. The run spec carries `model`; `ClaudeCodeRuntime` passes it as `ClaudeAgentOptions.model` and to the provider probe. The model catalog is `providers.MODELS` (anthropic: `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, each with a one-line description), returned as `models: [{id, description}]` on each provider kind of `GET /api/system/runtimes` and on `ProviderOut`.
  - `POST /api/providers/{id}/test` takes an optional body `{model}`. For `api_key` it calls `GET /v1/models/{model}` (free; the id or alias must exist for the key) instead of listing models. For `oauth_token` the worker's probe runs `exradius-runner --probe claude_code --model <id>`. A test with a model is stored as `last_test` only when it passes, since a wrong id says nothing against the credential.
- **Secret references** in `env` and in services' `env` resolve from the store when the Run is created, for setup containers and the workstation alike. A missing key fails the run in `preparing` (in Verify, "Read config" already fails).
- **Services in a run** (`docs/kubernetes.md` §7.4, `docs/environment-and-services.md` §2). Each service is a native sidecar `service-<name>` of the run's pod, after the clones, in file order, from its image (or its built image), with its image's user and entrypoint, `command` as the container's arguments, the capability set `CHOWN SETUID SETGID FOWNER DAC_OVERRIDE NET_BIND_SERVICE`, no privilege escalation, a writable root and no mounts. Its env holds plain values inline and values with a secret as `service.<name>.<KEY>` of the per-run Secret.
  - Readiness is the sidecar's startup probe, every second: `{"command"}` an exec probe (10 s timeout), `{"tcp": p}` a TCP probe on the pod's address, `null` the image's `HEALTHCHECK`, which `worker/registry.py` reads from the image's registry (anonymous bearer tokens; the registries in `EXRADIUS_INSECURE_REGISTRIES` over HTTP; cached an hour per reference) and gives as `ready.exec` (`CMD-SHELL` through `/bin/sh -c`). An image whose config cannot be read counts as started once it runs, and the worker logs why.
  - The operator fails the Run (`reason: ServiceFailed`) when a service is not started 120 s after its container started, or exits or restarts before; the kubelet's own failure threshold (240 probes) is only a backstop. The step keeps the service's log (the previous instance's after a restart) and a one-line `message`; the run's error is `service <name> (<image>) <message>` and the log tail, with "exceeded its declared memory (512m)" for an `OOMKilled` under a declared limit. A service step that passed stays passed: Kubernetes restarts a service that crashes later, and stops every sidecar when the workstation ends.
  - The pod's `hostAliases` map every service name to `127.0.0.1`: the services and the workstation share one network namespace, as compose publishes ports on `localhost`. The config check refuses a port listed by two services and the service name `localhost`. There are no Docker networks, labels or orphan sweeps any more: deleting the Run deletes the pod.
  - `waiting_for_input` leaves the services running, because the pod keeps running. Only a pod recreated after it was lost (`docs/kubernetes.md` §8.3) starts them empty.
- **Verify.** After "Read config" and the builds, the verify Run's steps: "Prepare workspace", "Clone …", "Start service <name>" (log "waiting for <check>", then "ready (<check>)" or the failure), "Setup: …", "List tools …" (`docs/setup-and-verify.md` §1.4). There is no "Stop services" step.
- **Delivery warning** (`exradius/worker/delivery.py`). Before claiming the finalize, the worker runs `INSPECT_CLONES_SCRIPT` when the run directory holds clones. The script reports, per clone, `git --no-optional-locks status --porcelain` and, for each local branch (and a detached HEAD), `git rev-list --count <branch> --not --remotes`.
  - It runs in a container from the project image as the run user, with only `/work` mounted read-only, `network_mode=none`, no project variables or secrets, and a 60-second limit. It never runs in the worker, because the clone's `.git/config` is the agent's and options such as `core.fsmonitor` would run commands as the worker.
  - The resulting `warnings` (always present in `task.result`, empty when there is nothing to report) read, for example, "app: 2 commits on T-1-fix were not pushed and were discarded with the task directory" or "app: uncommitted changes in 3 files were discarded with the task directory". A failed check gives no warnings and is logged.
- **Run user** and **co-author line**: see `docs/runtime-bundles.md` §2.4 and `docs/agent-identity.md` §3. Each run directory holds `etc/passwd` and `etc/group`, which are bind-mounted read-only into setup and run containers but not into provider probes or the delivery check.

**Config repositories** (`docs/config-repo.md`, implemented 2026-11-07)

- **Data model** (one initial migration `0001`, replacing 0001–0007; the database and data directory were wiped). `projects` (`id`, `slug`, `config_url`, `config_branch`, `config_path`, `snapshot`, `snapshot_commit`, `snapshot_read_at`, `created_at`), `git_credentials` (`host` unique, `kind` token|basic, `value_encrypted`), `secret_stores` (`name` unique, `kind` internal), `secret_store_entries` (unique `(store_id, key)`, `value_encrypted`, `updated_at`), `image_builds` (`project_id`, `name`, `tree_hash`, `image`, `state` running|succeeded|failed, `started_at`, `ended_at`, `log_tail`). `tasks.agent` is the agent's slug (a string). `runs` gain `config_commit`, `config` (the parsed config of that commit, secret references as written) and `agent_snapshot` (`slug`, `name`, `model`, `provider`, `runtime`, `identity {name, email}`, `connections`).
  - `runs.config` is not in the design: the run spec, resumes and the delivery check use the config the run started with, so a push to the config repository never changes a run in flight, and no git read happens at resume.
- **The schema** (`exradius/schemas/project_config.py`). Pydantic models with `extra="forbid"` and `strict=True`; numbers in string places become strings (as in compose files), booleans there are refused ("write true, false, yes or no in quotes"), because YAML reads `yes` and `on` as true. The loader refuses a mapping key given twice (PyYAML keeps the last), with the line and column. `parse_config` checks structure first, then the rules: where `${secret:NAME}` may appear (anything else is "secrets are not allowed here", a malformed reference is its own error), `secret_store` required once a secret is referenced, unique repository folders, `push` needs a `credential` (a credential without `${secret:…}` is a warning), image xor Dockerfile, `context` only with a Dockerfile, paths relative and without `..`, the Dockerfile a file and the context a directory at the commit with no symlink on the way, service names (`[a-z][a-z0-9-]*`, not `api`, not `workstation`, not `exradius*`, not a repository folder), unique ports, `ready` with exactly one of command or tcp, connection names and exactly one of `catalog`/`command`/`url` with only that kind's fields, the catalog's config keys, agents' connections known and listed once, and at least one agent. Every message starts with its YAML path (`services.db.ready.cmd: unknown key`, `repositories[0].push: needs a credential`, `(file): …`). A file with any error has no config. After the checks, Dockerfile and context paths are stored from the repository root.
  - `workstation` is a reserved service name (not in the design): it would share the tag `exradius/<project>/workstation`.
  - A launcher (`npx`, `uvx`) on the default workstation is a warning, for catalog entries (`requires`) and custom stdio connections alike.
  - Flow mappings cannot hold an unquoted reference: `{token: ${secret:X}}` is not YAML, because `{` opens a flow mapping. The design's example was fixed to quote them; block style needs no quotes.
- **Reading** (`exradius/config_repo.py`, `exradius/project_state.py`). The mirror is `<EXRADIUS_CACHE_DIR>/config-repos/<project id>.git` in the reading pod's own cache, fetched under a file lock with the credential of the URL's host (`git_credentials`; a token is sent as `x-access-token:<token>`, `basic` as given; local paths and `file://` have no host). Without `config_branch`, the branch is the remote's default (the mirror's `HEAD`, which follows the remote at every fetch). The file is `config_path`, or `exradius.yaml`, then `.exradius/exradius.yaml`. A repository, branch or file that cannot be read raises `ConfigReadError`; callers turn it into a snapshot with that error and no commit. Reads happen in `POST /api/projects`, re-read and PATCH (API), and at intake, in `preparing` and in Verify (worker); each stores the snapshot on the project.
  - Health (`project_state.assess`): errors are the file's plus a slug change ("project.slug: the file says 'x', but this project was added as 'y'; …"; the row keeps its slug). Missing keys are the required keys the store lacks (all of them when it does not exist). The reason, in order: `config error: <first error>`, `secret store S does not exist`, `missing secret A in store S` / `missing secrets A, B in store S`. A file that names a store but references no secret does not need the store.
- **Intake** reads the config afresh (not the cache), so an agent added to the file is known to the next task. Rejections: `unknown project 'x'`, the health reason, `unknown agent 'x' in project 'p' (it defines a, b)`, `no provider configured` / `unknown provider 'x'`, and the existing ones. `POST /api/tasks` still checks nothing itself; the panel sees the rejection as a `rejected` status event on the task's stream.
- **Preparing** reads the config again, fails the run with the health reason or "agent 'x' is no longer defined in project 'p'", records `config_commit`, `config` and `agent_snapshot`, builds the images, then clones, writes attachments, starts the services and runs `workstation.setup`, all from that config. Container start (first start and each resume) takes model, provider, runtime, identity and connections from `runs.config`; the run spec carries the role as `agent_prompt`.
- **Secrets in the workstation.** The workstation environment is the config's `env` with secrets substituted, plus every store key that the services' settings reference, under its own name, so the environment section's `$DB_PASSWORD` is true. Repository credentials and connection headers are used by reference only and never exported (since phase 4 the egress proxy holds them). Provider variable names in `env` are dropped as before.
- **Builds** (`exradius/worker/builds.py`, `docs/kubernetes.md` §12). One job per Dockerfile (`workstation`, then services in file order). The tag is `exradius/<project slug>/<name>:<first 12 hex digits>` of the context's git tree hash at the run's commit, under `builds.registry`; when the Dockerfile lies outside the context, of the SHA-1 of `<tree hash>:<Dockerfile blob hash>`, so editing that Dockerfile rebuilds too (the design hashed the context only). The context is `git archive` of that tree, stored in the object store; a Dockerfile outside the context is added to the tar as `.exradius.Dockerfile`. Each tag is a `Build` object created if absent (concurrent runs wait on the same one; this replaced v0.1.0's per-project file lock), run by the operator in a single-use rootless BuildKit pod that fetches the context from `/internal/builds/{id}/context` with the Build's token and pushes the image and `<image>:buildcache`. A succeeded Build is "up to date"; a failed one is replaced by the next caller. Build output goes to the Build's status and `image_builds.log_tail` (4000 characters). A failed build fails the run in `preparing` with "building <name> from <dockerfile> failed:", what ended it, and the tail. Nothing removes old tags yet.
- **Catalog** (`exradius/catalog.py`): github, gitlab, postgres, slack, sentry, linear, jira, fetch, playwright, filesystem, each with transport, command and args or URL, `requires`, and config keys (`key`, `label`, `required`, `target` env|header|arg, `name`, `format`, `default`). `expand` gives the `mcpServers`-style definition; `GET /api/catalog` lists the entries. Optional keys with a default: gitlab `api_url` (`https://gitlab.com/api/v4`), filesystem `path` (`/work`).
- **API** (§11). `POST /api/projects {config_url, config_branch?, config_path?}` answers 201 with the project, 422 with `detail: [errors]` when nothing can be read or the file names no valid `project.slug`, and 409 when the slug exists. `ProjectOut`: `id, slug, name, description, config_url, config_branch, config_path, commit, read_at, branch, path, health (ok|config_errors|missing_secrets), runnable, reason, errors, warnings, store, store_id, store_exists, required_keys [{name, present}], missing_keys, agents [slug], config (parsed, references as written), raw (the file's text), created_at`. Warnings include agents naming a provider that does not exist. `GET` answers from the cache; `POST …/reread` and `PATCH {config_branch, config_path}` (blank or `null` = default) read again. `DELETE` answers 409 while the project has tasks and removes the config mirror. `GET /api/projects/{id}/agents/{slug}/resolved` (409 when the config has errors): `project_id, commit, agent, name, role, model, runtime, provider, resolved_provider {id, slug, name, kind, auth_method} | null, provider_error, identity {name, email}, connections [{name, catalog, transport, command, args, url, env, headers, secrets}], image, environment_text`, with secrets as `$NAME`. `GET/POST /api/git-credentials`, `DELETE /api/git-credentials/{id}` (host lowercased and unique, 409 on a second one; values never returned). Stores: `GET/POST /api/stores` and `GET/DELETE /api/stores/{id}` (`StoreOut {id, name, kind, created_at, keys, used_by}`; delete is 409 while a project's latest read names it), `GET /api/stores/{id}/keys` (`[{key, updated_at}]`), `PUT /api/stores/{id}/keys/{key} {value}`, `DELETE /api/stores/{id}/keys/{key}`. `RunOut` gains `config_commit` and `agent_snapshot`. Removed: repositories, env vars, secrets, connections (and their test), agents, attachments of agents, services and the repository check; "test connection" has no replacement yet, so `mcp` left the server dependencies.

**Verification without clicks** (`docs/setup-and-verify.md` §1, implemented 2026-11-07)

- Migration `0002` adds `project_verifications.commit` (set by "Read config", also when the file has errors) and `diagnosis` (written by `_finish` for a failure from `exradius/worker/diagnose.py`: substring and regex rules over the first failed step's name, command and log tail, first match wins, `null` when nothing matches).
- `POST /api/projects` asks for a verification once the file was read (with or without validation errors); re-read and `PATCH` ask for one when `snapshot_commit` changed to a commit (the PATCH case is not in the design: it is a re-read with another branch or path). At most one verification per project is `pending` or `running`: a request then returns that one (the project row is locked while checking), and the manual Verify answers with its id. When the broker cannot be reached, the automatic request leaves the new row failed with `broker: …` and the add or re-read still succeeds; the manual Verify answers 503 as before.
- `ProjectOut` gains `verification` (the latest one's `id, state, commit, ended_at, diagnosis`) and `freshness` (`current`, `stale`, `failed`, `running`, `never`; passed at another commit than `snapshot_commit` is `stale`, so are rows from before `0002`). `VerificationOut` gains `commit` and `diagnosis`. Nothing is gated on freshness.

**The run environment** (`docs/run-environment.md` §3-§8, implemented 2026-11-07; the principle is §14)

- **Git at the command-line level** (`worker/git_identity.py`). `workstation_git` returns the
  identity and `GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n`: `safe.directory=*`,
  `core.hooksPath=<bundle>/git-hooks` and, since phase 4, each repository's
  `url.<proxy route>.insteadOf` (there are no credential helpers any more). The container gets them after every other variable,
  and any `GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n`/`PARAMETERS` in the project's `env` is
  dropped, so neither a file nor the project's environment replaces them. Setup containers
  and the delivery check get `safe.directory` alone. The managed global config keeps
  `user.*`, `push.autoSetupRemote` and `init.defaultBranch`.
  - The hooks directory holds every name of githooks(5) except `push-to-checkout` (its
    presence alone replaces git's `updateInstead`), each the same dispatcher: trailers in
    `prepare-commit-msg`, then `exec` of the project's hook with the arguments and stdin.
    The project's hooks path is the last `core.hooksPath` of `git config --show-scope
    --get-all` that is not `command` (system, global, local or worktree, so the agent's
    own `git config --global core.hooksPath` counts too), else
    `$(git rev-parse --git-common-dir)/hooks`. Not `git rev-parse --git-path hooks`, as
    the design said: that already answers with the forced path. A hooks path that is the
    dispatcher's own directory is skipped. `--show-scope` needs git 2.26.
- **Claude Code settings** (`runtimes/claude_code.py`). `SETTINGS` (`includeCoAuthoredBy:
  false`, `attribution.commit` and `.pr` empty) goes to the SDK's `settings` option, which
  the CLI receives as `--settings`; command-line settings outrank `.claude/settings.json`,
  `.claude/settings.local.json` and user settings key by key. `setting_sources` stays the
  default (all), so `CLAUDE.md`, `AGENTS.md` and `.claude/` load. Nothing is written to
  `CLAUDE_CONFIG_DIR/settings.json` any more. The gap left: managed settings
  (`/etc/claude-code/managed-settings.json` and `managed-settings.d/`) outrank
  `--settings`, and a project's workstation image could bake one. Mounting our own file
  there would close it but was not done: it is a Docker-level change to a system path of
  the project's image, for a case no project has.
- **Memory** (`docs/kubernetes.md` §6, `docs/run-environment.md` §4). A declared size is request and limit of its containers (`quantity()` turns `6g` into `6Gi`); undeclared gets the operator's default request (`--memory-request`, `--service-memory-request`) and no limit, and every container a CPU request without a limit. The scheduler places pods by their requests; one that fits nowhere is `Pending`, and `settle` sets `runs.waiting_for = 'capacity'` while the Run's `Scheduled` condition says `Unschedulable` (the task row says "waiting for cluster capacity"; no status event). `OOMKilled` under a declared limit becomes "the run exceeded its declared memory (6g) and was killed (exit 137)", "setup command '…' exceeded its declared memory (6g) …", or "service <name> (<image>) exceeded its declared memory (512m): …". v0.1.0's `MemoryGate`, budget and "Reserve memory" step are gone.
- **Caches** (`worker/caches.py`, `docs/kubernetes.md` §7.6). `cache_specs` names each entry `<n>-<sanitised path>` (its index in the list, the path's characters outside `[A-Za-z0-9._-]` as `_`); the Run spec carries `{path, name}`. The operator mounts `caches/<name>` of the project volume (or, without one, of the run's workspace) at the path in the setup containers and the workstation. `prepare`, as the run user, creates the directories and every mount point below the home or `/tmp`: the kubelet would create a missing `~/.cache` as root. The panel's cache size (`ProjectOut.cache_bytes`) is gone. The project volume `exradius-project-<slug>` is created by the operator on the project's first Run (owned by no Run; a deleted one still terminating is waited for) and deleted by the worker on the api's `project_deleted` control message, or by its sweep every minute when the project no longer exists.
- **Service `command`** is the container's `args`; the entrypoint is the image's.
- **Store changes** (`config_routes._verify_after_keys`). After `POST /api/stores`,
  `PUT /api/stores/{id}/keys` and `PUT /api/stores/{id}/keys/{key}`, every project whose
  latest read names the store, is runnable now and whose freshness is neither `current`
  nor `running` gets a verification through `_request_verification` (the one-open rule;
  without the broker it fails at once and the store change still succeeds).

## 13. Operating rules and prompt layout (agreed 2026-11-06)

Supersedes the prompt layout in §5.2. Tasks arrive from external trackers and never mention
Exradius; agents must not depend on the admin's prompt or the ticket to learn how they run.
Exradius therefore owns the operating rules and injects them itself.

**System prompt** = Claude Code's preset, then the Exradius operating rules, then the
generated environment section (`docs/environment-and-services.md` §1), then the agent's
prompt (the role). **User message** = the task text, context and attachments only.

Operating rules (fixed text, maintained in `exradius/runner/prompt.py`):

```
You are working unattended on a task that was assigned through an external system such as
an issue tracker. The task text below was written by a person who does not know how you
run and who will see only two things from you: the questions you ask and the result you
deliver.

- You start inside the project's directory. Each folder in it is a git clone of one of
  the project's repositories, checked out on its working branch with origin set to the
  real remote.
- If you cannot proceed without something only that person can tell you, call
  request_information with one clear question written for them, then stop; you will be
  resumed with the answer. Each question is a round trip through their system, so ask
  only when necessary and never more than one question at a time.
- When you are done, call submit_result with a result written for that person: what you
  found, what you changed, and what they should do next. Attach files that help.
- Nobody is watching. Do not ask for confirmation or permission; decide and act. If the
  task cannot be done, say why in the result.
- The project directory exists only for this task and is discarded afterwards, so
  changes left uncommitted are lost. If your work changes a repository, commit it there
  on a new branch whose name starts with the task id shown in the task heading, and push
  that branch to origin; say in the result what you pushed and where. If the push is
  refused, attach a patch of your commits (git format-patch) to the result and say so.
- Refer to files by their path inside the repository, never by the directory you run in.
```

The user message layout: `# Task <task id>` (the id is the ticket key, which a developer
would know), `# Context` (when present), `# Attachments` (when present). The resume message stays as it is. The agent's final assistant text is still
recorded as the result summary when `submit_result` is not called.

Admin-side consequences: starter prompts describe roles only and never mention the tools;
the guide panel shows the operating rules verbatim so an admin knows they are injected.

## 14. The run environment principle (agreed 2026-11-07)

> From the inside, a run looks like a developer's own workstation. Exradius deviates only
> where security requires it, and every deviation, and everything Exradius needs to stay
> true inside a run, is enforced at the level of the kernel, Kubernetes or the command line,
> never through a setting a project, a tool or the agent can reset.

Across the first four projects (bank, Outline, Saleor, Plane) one finding kept its shape: a
tool assumed a normal developer environment and the run container was not one. Every fix
moved from a setting at the configuration level, where userland tools expect to be in
charge, to one where they cannot interfere:

| Finding | Root cause | Fix |
|---|---|---|
| Maven ignored `~/.m2` (bank) | `HOME` set as an environment variable; Java reads the home from passwd | a generated `/etc/passwd` mounted into the container |
| A run was killed, exit 137 (Outline) | the home and `/tmp` were tmpfs, so a package install lived in RAM | bind mounts on disk |
| `postgres` resolved to Exradius's own database (bank) | runs joined the stack network | a dedicated network |
| Commit trailers missing (Plane) | `core.hooksPath` set in the run's global git config; husky's `prepare` script wrote a repo-local one, which wins | the hooks path at the git command-line level, with a dispatcher that also runs the project's own hooks |
| `yarn install` timed out behind the egress proxy (Outline, phase 4) | Yarn 2 and later read only their own proxy settings, not `HTTPS_PROXY` | `YARN_HTTP(S)_PROXY` in every run container, written by the operator |
| Maven could not reach Maven Central behind the proxy (bank, phase 4) | Maven 3.9's resolver reads proxies only from `settings.xml` | `-Daether.connector.http.useSystemProperties=true` in the operator's `JAVA_TOOL_OPTIONS` |

Where each thing Exradius sets inside a run lives now (the audit of
`docs/run-environment.md` §2.1, rewritten for Kubernetes in phases 3 and 4):

| What | Level |
|---|---|
| uid 1000 as `runner`, home `/home/runner` | passwd and group files from a ConfigMap, mounted read-only by subPath |
| read-only root, no capabilities, no privilege escalation, the run user | the container's security context, written by the operator |
| no ServiceAccount token, no service links, supplementary groups | the pod spec, written by the operator |
| services on `localhost` by name | the pod's one network namespace and `hostAliases` (kubelet-managed `/etc/hosts`) |
| `/work`, home, `/tmp`, state, attachments | subPaths of the run's workspace PVC |
| caches, git mirrors | subPaths of the project volume |
| declared memory | the container's request and limit (its cgroup) |
| `safe.directory`, `core.hooksPath`, each repository's route to the egress proxy (`insteadOf`) | git command line (`GIT_CONFIG_*`) |
| what a run reaches on the network | NetworkPolicy (DNS, the egress proxy, the api's internal port only) and the proxy's decision by phase and host, from the pod watch |
| the model, repository and connection credentials | the egress proxy's routes; the pod holds placeholders (`ANTHROPIC_API_KEY=placeholder`) |
| HTTP clients use the proxy | `HTTP(S)_PROXY`/`NO_PROXY`, `YARN_HTTP(S)_PROXY`, `JAVA_TOOL_OPTIONS` (Java's proxy properties, and Maven's resolver told to use them), written by the operator after the project's own |
| author and committer identity | `GIT_AUTHOR_*`/`GIT_COMMITTER_*`, plus the managed global config |
| `push.autoSetupRemote`, `init.defaultBranch` | managed global config (conveniences a project may change) |
| Claude Code settings the runtime needs | `--settings` (only managed settings rank higher; §12) |

A new requirement inside a run follows the same rule: find the level a project's own
tooling cannot reach, or record the gap in §12.
