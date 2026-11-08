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

```
                 user's integration layer
                 (publishes tasks + answers, consumes events)
                            │        ▲
                            ▼        │
                   ┌────────────────────────┐
                   │   RabbitMQ (broker)    │
                   └────────────────────────┘
                      │                 ▲
          consumes    │                 │ publishes
                      ▼                 │
   ┌──────────┐   ┌──────────────┐   ┌──────────┐    ┌──────────────┐
   │ Frontend │──▶│  API server  │◀──│  Worker  │───▶│ Docker daemon│
   │ (React)  │   │  (FastAPI)   │   │          │    │              │
   └──────────┘   └──────┬───────┘   └────┬─────┘    └──────┬───────┘
                         │                │                 │
                         ▼                ▼                 ▼
                  ┌────────────┐   ┌──────────────┐   ┌────────────────┐
                  │  Postgres  │   │ Object store │   │ Run container  │
                  │            │   │ (fs or S3)   │   │  └─ runner     │
                  └────────────┘   └──────────────┘   │     └─ SDK     │
                                                      └───────┬────────┘
                                                              │ internal HTTP
                                                              ▼
                                                         API server
```

- **API server.** FastAPI. Serves the admin panel API and the internal run API that containers call back into. Owns the Postgres schema.
- **Worker.** Consumes tasks and answers from the broker, prepares run directories, starts and stops run containers, publishes outbound events. Concurrency is a worker setting (prefetch count). Several workers may run on one host; version one targets a single host.
- **Runner.** A small Python process that is the container's entrypoint. Hosts the Claude Agent SDK session and the built-in `exradius` MCP server. Talks to the API server over internal HTTP with a per-run token.
- **Broker.** RabbitMQ, shipped in the docker-compose stack. Pointing Exradius at an external broker is a configuration value, not a feature.
- **Postgres.** All configuration and all task/run state. The broker is never the source of truth.
- **Object store.** Transcripts, result artifacts, attachments. The filesystem by default (a directory inside the shared data directory that the API server and the worker both mount); any S3 API optionally.
- **Frontend.** React single-page app. Admin panel only.

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

The worker:

1. Creates the run directory on the host: `/var/lib/exradius/runs/<run_id>/` containing
   - `project/` — becomes the container's working directory;
   - `state/` — runtime state that must survive suspend/resume (for Claude Code, the session files under its home directory);
   - `home/` — the run user's home, mounted at `/home/runner` (`$HOME`). It is on disk, like an engineer's home directory, because package manager caches and build output land there: one `yarn install` of a large Node project leaves 800 MiB in its cache (`~/.yarn/berry/cache` for Outline), and in memory (a tmpfs) two such runs at once exhausted a 7.7 GB Docker VM. Setup commands, the agent container and every resume share it, so a cache filled by setup is warm for the agent; it goes away with the run directory (§5.5);
   - `tmp/` — the run's `/tmp`, mode 1777, on disk for the same reason: Yarn Berry stages every package archive in `/tmp/xfs-*/archive.zip` until it exits, as much again as its cache (814 MiB for Outline);
   - `attachments` are written to `project/.exradius/attachments/`.
2. For each repository of the project: refreshes the per-project bare mirror at `/var/lib/exradius/mirrors/<project>/<repo>.git` with `git fetch`, then clones from the mirror into `project/<repo-folder>/` with `git clone --local` (hardlinked objects, fast) and checks out the configured branch.
3. Runs the project's setup commands with `project/` as working directory, inside the run image (so toolchains match). Non-zero exit fails the run.

Nothing is shared between concurrent runs except the read-only mirrors. Any number of agents can work on the same project at once.

### 5.2 Running

The worker starts a container from the project's image with the agent's runtime bundle mounted into it (`docs/runtime-bundles.md`). The project image is any glibc-based Linux image with `bash` and `git` on `PATH` (Claude Code's shell tool uses bash), for example `python:3.14` or `maven:3-eclipse-temurin-21`; musl images such as Alpine are not supported. A project without an image gets `exradius-project-default` (Ubuntu LTS with git, curl, ca-certificates and build-essential), which is a default, not a base. Nothing has to inherit from an Exradius image. The container gets:

- `project/` mounted at `/work`, the working directory;
- `state/` mounted at the runtime's state location;
- environment: the project's variables, the project's secrets (decrypted at this moment only), the variables of the agent's model provider (or the default provider; see `docs/providers.md`), `EXRADIUS_RUN_TOKEN` and `EXRADIUS_API_URL`;
- the runtime bundle volume mounted read-only at `/opt/exradius/runtime`, with `/opt/exradius/runtime/bin/exradius-runner` as entrypoint, receiving the run id.

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

On `waiting_for_input` the container exits and the run directory stays on disk, uncommitted work included. When an answer arrives, the worker starts a new container with the same mounts and the runner resumes the runtime session (for Claude Code: the SDK `resume` option with the stored session id) with the answer as the next user message. The run returns to `running`.

A run can cycle through `waiting_for_input` any number of times.

### 5.5 Ending

The run ends when the session finishes (`completed`), the runner reports an error (`failed`), or the admin cancels (`cancelled`; the worker stops the container). The worker uploads the transcript, publishes `task.result`, and deletes the run directory. Mirrors are kept.

A cancelled run that was `waiting_for_input` also frees its directory. There is no automatic expiry of waiting runs in version one; the admin panel lists them.

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
runs              id, task_id, state, container_id, runtime_session_id, directory, started_at, ended_at, error
info_requests     id, run_id, question, context, created_at, answered_at, answer, answer_attachments (json)
events            id, task_id, run_id, type, payload (json), published_at
transcript_chunks id, run_id, seq, payload (json)
artifacts         id, run_id, name, content_type, object_key
admin_user        id, username, password_hash
workers           id, hostname, started_at, last_seen_at, concurrency, docker_ok, docker_detail,
                  images (json), runtimes (json), version
project_verifications  id, project_id, state, created_at, started_at, ended_at, error,
                  steps (json), worker_id, commit, diagnosis
provider_tests    id, provider_id, state, created_at, started_at, ended_at, detail, error,
                  fingerprint, worker_id
```

The project-side tables above were replaced on 2026-11-07 by config repositories (`docs/config-repo.md` §8); §12 "Config repositories" has the current model.

Secrets and provider credentials are encrypted with a key from the Exradius environment. They are decrypted only at container start and never returned by the admin API.

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
- Containers run with no privileges, a read-only root filesystem except `/work`, `/tmp` and the state mount, and outbound network allowed (agents need git remotes and MCP endpoints).
- The runtime bundle is mounted read-only and is world-readable and executable, so the unprivileged run user (uid 1000 by default) runs it whatever users the project image defines. The project image cannot change the runtime, and the runtime does not depend on anything in the image except glibc and `/bin/sh`. Setup commands run without the bundle.
- The internal run API accepts only the per-run token, which is revoked when the run ends.

## 10. Repository layout and stack

Single repository, two packages:

```
exradius/
  backend/            Python 3.14, uv, FastAPI, SQLAlchemy + Alembic, aio-pika, docker SDK
    exradius/
      api/            admin API + internal run API
      worker/         broker consumer, run preparation, container control
      runner/         run container entrypoint (inside the runtime bundle), exradius MCP server
      runtimes/       Runtime protocol, ClaudeCodeRuntime
      models/, schemas/, messaging/
    tests/
  frontend/           React + TypeScript + Vite
  deploy/
    docker-compose.yml               api, worker, frontend, postgres, rabbitmq; builds the images below
    backend.Dockerfile               exradius-backend: api and worker (no Node, no Agent SDK)
    runtime-claude-code.Dockerfile   exradius-runtime-claude-code: the Claude Code runtime bundle
    runtime-claude-code/             the bundle's launcher script
    project-default.Dockerfile       exradius-project-default: image for projects without one
    Makefile                         make bundle-claude-code: the bundle directory alone
  docs/
```

## 11. Deferred

Recorded so they are not re-debated:

- run limits (turns, tokens, time) and automatic expiry of waiting runs;
- retries and multiple runs per task;
- persistent per-project cache directories to speed up setup commands;
- per-project API keys;
- roles and multiple admin users;
- external broker support beyond a connection string;
- multi-host workers.

## 12. Implementation notes

Details the design left open, and the simplest choice made for each in version one.

**Layout and images**

- The package lives at `backend/exradius/` as in §10. Shared modules sit next to the listed folders: `config.py` (`EXRADIUS_*` settings), `crypto.py`, `states.py` (the §5 state machine), `resolve.py` (secrets and connections), `storage.py`, `db.py` and `cli.py`.
- Three images (`docs/runtime-bundles.md`). `exradius-backend` runs the api and worker; it has git and the Docker SDK, but no Node and no Agent SDK (the `runner` dependency group is not installed). `exradius-runtime-claude-code` holds only the Claude Code bundle. `exradius-project-default` is the run container image of projects that name none. Compose builds the last two through build-only services (`scale: 0`). `EXRADIUS_RUNTIME_IMAGE_CLAUDE_CODE` and `EXRADIUS_PROJECT_DEFAULT_IMAGE` override their tags.
- A stdio connection in a run needs its command in the workstation image; the default image has neither `npx` nor `uvx`, which the config check reports as a warning.
- The object store is the filesystem by default (`EXRADIUS_OBJECT_STORE=filesystem`): objects live at `<EXRADIUS_DATA_DIR>/objects/<bucket>/<key>`, written atomically (temporary file, then rename). Keys are validated before use: no empty, `.` or `..` segments, no leading `/`, backslash or NUL, and the resolved path must stay inside the bucket directory, so a key or a symlink cannot escape it. The API server and the worker both mount the data directory. `EXRADIUS_OBJECT_STORE=s3` selects any S3 API through the `EXRADIUS_S3_*` variables instead. Compose ships no object store service, because MinIO no longer publishes community server images.
- Download URLs in `task.result` are signed with an HMAC-SHA256 of the URL path, keyed by a value derived from `EXRADIUS_SECRET_KEY`. `GET /api/artifacts/{id}/content` and `GET /api/runs/{run_id}/transcript` accept a valid `sig` or the admin session. A signature is bound to its path and does not expire in version one. The transcript endpoint serves the stored JSON lines file once the run has ended, and assembles it from the stored chunks while the run is still going. Behind a reverse proxy that adds a path prefix, include the prefix in `EXRADIUS_PUBLIC_API_URL`; the signature covers only the API path.

**Data model additions** (beyond §6)

- `runs`:
  - `token_hash`: sha256 of the current run token.
  - `reported_status`: the outcome the runner reported.
  - `summary` and `output`: the result fields.
  - `resume_request_ids`: the answers delivered by the current resume.
  - `worker_id`: the worker driving the run through preparing, running or resuming (crash recovery).
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
- Worker concurrency is `EXRADIUS_WORKER_CONCURRENCY`. It is both the prefetch count of the task consumer and the number of run slots. A task message is persisted and acknowledged only when a slot is free, so the broker holds the backlog. Answers and control messages use a separate channel.
- Base64 attachments are uploaded to the object store at intake, before the ack. URL attachments are downloaded while the run is preparing.
- The worker is the only publisher of outbound events. Each event is first stored in `events`, then published with AMQP `message_id` = `events.id`. If publishing fails, the row keeps `published_at = NULL` and the run continues. Version one does not republish.
- Event timestamps are UTC with second precision (`2026-11-05T19:12:03Z`).
- `result.output` is the JSON value passed to `submit_result`, or `null`. It is not wrapped in an object. `summary` is the final assistant message (the SDK `ResultMessage.result`).
- Admin cancel travels from the API to the workers over an internal durable queue, `exradius.control`. That queue is not part of the public contract in §4. Any worker can handle it.

**Run lifecycle**

- Every transition locks the run row (`SELECT … FOR UPDATE OF runs`), updates both run and task, and records the status event in the same transaction.
- Finalisation happens exactly once, guarded by `runs.ended_at IS NULL`. It stores the transcript under the key `runs/<run_id>/transcript.jsonl`, publishes `task.result`, revokes the token and deletes the run directory. Cancel, failure and completion all go through it, so a cancel racing with a container exit produces one result.
- The runner never changes state. It reports its outcome to the internal API. The worker makes the transition after the runner container exits, and then publishes any pending `task.info_request` events. These are published within seconds of the call, because the runner interrupts the session right away.
- A run waits on its first outstanding question. All questions answered by the time it resumes are delivered together in one message. An answer that arrives before the suspended container has exited is picked up as soon as the container exits.
- Crash recovery runs when a worker starts, before it consumes any message. Each worker process gets a fresh id, which it stamps on the runs it drives through `preparing`, `running` and `resuming`. On Postgres it holds a session-level advisory lock on that id for as long as it lives, so other workers can tell whether it is still alive. Recovery has three steps:
  1. *Reconciliation.* Every run in `preparing`, `running` or `resuming` whose worker is no longer alive is claimed under the run row lock. Claiming stamps the new worker's id, so two restarting workers never recover the same run, and runs of live workers are left alone. If a runner container labelled with the run's id still exists, the worker re-attaches: it records the container (moving a `preparing` or `resuming` run to `running` if the old worker died before recording it) and settles the container's exit through the normal path. If the container is gone but the runner had already reported an outcome, that outcome is applied. Otherwise any leftover setup containers are removed and the run is failed with "worker restarted while the run was <state>", then finalised as usual (directory removed, `task.result` published). Settling checks that the run is still `running` with the same container, so two workers waiting on one container act on its exit once.
  2. *Orphan sweep.* Every directory under `<data_dir>/runs/` that does not belong to a run in `preparing`, `running`, `waiting_for_input` or `resuming` is removed and logged. The directories are listed before the database is queried, so a run that starts meanwhile is never swept.
  3. *Resumable runs.* Waiting runs whose answer arrived while no worker could act on it are resumed.

  Waiting runs are otherwise left alone. They keep their directory, and §5.5's rule of no automatic expiry still holds. Without Postgres (SQLite in tests), a worker assumes it is the only one.
- Setup commands (`workstation.setup`) run one container each, from the workstation image, with `/work` mounted and the workstation environment (the config's `env` and the exported secrets, see "Config repositories"). A non-zero exit fails the run, and the tail of the command's output becomes the error. Setup commands do not run again on resume.

**Run containers and directories**

- `state/` is mounted at `/exradius/state`. `ClaudeCodeRuntime` sets `CLAUDE_CONFIG_DIR=/exradius/state/claude`, so session files survive suspend/resume. The working directory is always `/work`, which keeps session paths stable across containers.
- Containers run as `1000:1000` with these settings:
  - a read-only root filesystem;
  - `cap_drop: ALL` and `no-new-privileges`;
  - `HOME=/home/runner` and `/tmp`, bind-mounted from the run's `home/` and `tmp/` directories (exec allowed, like the tmpfs mounts they replaced), so what a run writes takes disk, not memory. Setup, run and resume containers and Verify's setup containers all mount the same ones. Only containers without a run directory (the provider probe) and the delivery check (which mounts `/work` read-only and needs neither) get tmpfs for both. A run directory from before they were on disk gets empty ones when its container starts;
  - membership of the network named by `EXRADIUS_RUN_NETWORK` (`exradius-runs`), so they reach the API at `EXRADIUS_RUN_API_URL` (`http://exradius-api:8000`). Only the API is on that network; see "Networks" in §12.

  The worker runs as root and chowns `project/`, `state/`, `home/` and `tmp/` to the run user.
- Removing a run directory copes with whatever the run left: read-only files and directories (sealed attachments, Go's module cache, `chmod -w` build output), unreadable directories, deep `node_modules` trees, sockets and FIFOs. A plain `rmtree` runs first; if it fails, the worker gives the owner `rwx` on every directory (walking by directory descriptor, never following symlinks) and removes the rest.
- Attachments are written root-owned with mode `0444`. Their directory is also bind-mounted read-only over `/work/.exradius/attachments`. Answer attachments go to `.exradius/attachments/<request_id>/`.
- The worker sees its data directory at `EXRADIUS_DATA_DIR`. Bind mounts are created by the host's Docker daemon, so `EXRADIUS_HOST_DATA_DIR` gives the same directory as the host sees it. The compose file mounts both at the same host path.
- Mirrors are created with `git clone --mirror`, refreshed with `git fetch --prune` and serialised with a file lock per mirror. Clones use `git clone --local --branch <branch>`, after which `origin` is reset to the real remote URL. A repository's `credential` is used for the mirror fetch as an HTTP Basic `extraHeader` (a bare token becomes `x-access-token:<token>`), and for pushing only through the credential helper of `push: true` (Agent identity below). It is never written into the clone. A mirror's `HEAD` follows the remote's default branch at every fetch, so a repository without `branch` clones that one.
- The run token is a fresh random token for each container start, stored hashed and cleared when that container exits. The runner removes it from its own environment before starting the SDK, so the agent's tools do not inherit it.

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
- Each SDK message is stored as one transcript chunk, a JSON object with a `type` field. Sequence numbers continue across containers of the same run.

**Configuration resolution**

- Every `${secret:NAME}` resolves from the secret store the config names (`secret_store`); there are no project or global secrets, overrides or fallbacks. `exradius/resolve.py` substitutes at container start (worker) and for the run spec (API); a missing key raises `MissingSecret` ("missing secret NAME in store S").
- The connection name `exradius` is reserved for the built-in server.
- The model credential comes only from a provider (`docs/providers.md`).

**Admin**

- The admin user is created at API start from `EXRADIUS_ADMIN_USERNAME` and `EXRADIUS_ADMIN_PASSWORD`, only when no admin user exists. The variable never changes an existing password. `POST /api/auth/password` (`current_password`, `new_password` of at least 8 characters) changes it and answers 204, or 400 if the current password is wrong.
- Passwords are hashed with scrypt.
- The session is a signed cookie (`exradius_session`).
- Secret store values and git host credentials are encrypted with Fernet using `EXRADIUS_SECRET_KEY`. The API accepts and replaces their values but never returns them.

**Onboarding additions** (`docs/onboarding.md` §4)

- **Heartbeats.** Every 15 s each worker upserts its row in `workers`: hostname, concurrency, version, whether its Docker daemon answers, its runtime bundles (`runtimes: [{name, version, ready, image, error}]`, see Runtime bundles below), and the tags of every image on its daemon except Exradius' own backend and bundle images (label `exradius.role`). A worker deletes its row when it shuts down cleanly. `GET /api/system/status` and `GET /api/system/images` list only workers seen in the last 60 s. The table is only for reporting: crash recovery still uses the advisory-lock presence.
- **System status.**
  - The database check is `SELECT 1`.
  - The broker check is the API's own connection, which is shared with publishing.
  - `provider` reports the default provider and the number of providers (see Providers below). The object store and the Anthropic key are no longer reported.
  - `data_dir` is the API's `EXRADIUS_DATA_DIR`, for the Settings page.
- **Integration info.** Username, vhost and whether a password is set come from `EXRADIUS_BROKER_URL`; the password is never returned. Host and port come from `EXRADIUS_PUBLIC_BROKER_HOST` and `EXRADIUS_PUBLIC_BROKER_PORT`, which compose sets to `localhost` and the AMQP port, because the API's own broker URL names the compose-internal host `rabbitmq`.
- **Verification.**
  - `POST /api/projects/{id}/verify` stores a `pending` row and publishes `{"action": "verify"}` on `exradius.control`. If the broker cannot be reached, it answers 503 and marks the row failed.
  - The worker that takes the message claims the row (`worker_id`) before acknowledging it. The job then occupies one of the worker's run slots.
  - The first step is "Read config" (commit, file and branch, errors, warnings, missing keys); it fails when the project is not runnable, and nothing else runs. Once it passes, the worker records the rest of the plan as `pending` steps: "Build workstation" and "Build service <name>" for each Dockerfile in the config, create the directory, for each repository fetch the mirror and clone, prepare the attachments folder, start each service, run each setup command, then the cleanups ("Stop services", "Remove directory"). Steps therefore appear after the verification started. Each step goes to `running` and then `passed` or `failed`, with ISO timestamps and a log tail of up to 4000 characters (a build's log ends with `built <tag> in <s>s`, or reads `<tag> is up to date (context unchanged)`). After a failure the remaining steps are `skipped`, but the cleanups always run.
  - The work happens in `runs/verify-<id>`, with setup containers labelled with that name. The orphan sweep keeps such a directory while its verification is `pending` or `running`.
  - At startup, verifications claimed by a worker that is no longer alive are failed and their directory and containers removed.
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
- **Injection.** At every container start (first start and each resume), the worker resolves the agent's provider or the default, decrypts its credential and adds the runtime's mapping: `ANTHROPIC_API_KEY` for `api_key`, `CLAUDE_CODE_OAUTH_TOKEN` for `oauth_token`. Project variables and secrets named like any provider variable of the runtime (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`) are dropped with a warning, so the run gets only its provider's credential. Setup command containers get no provider. Intake rejects a task with `no provider configured` when the agent names none and there is no default, and with `unknown provider 'x'` when it names a slug no provider has. A run whose provider has disappeared by container start fails with the same message.
- **Tests.** `POST /api/providers/{id}/test` for `api_key` calls `GET https://api.anthropic.com/v1/models` from the API container (20-second timeout) and answers 200 `{ok, detail, error}`, with up to five model ids in `detail`. For `oauth_token` it stores a `pending` row in `provider_tests`, publishes `{"action": "test_provider", "test_id"}` on `exradius.control` and answers 202 `{test_id}` (503, with the row failed, if the broker is down). The worker claims the row before acknowledging and, in a run slot, starts a container from the default project image with the runtime bundle mounted, only the provider's variables and no run mounts, running `exradius-runner --probe claude_code`. That is a one-turn Claude Code session with no tools, sent "Reply with OK", with a 180-second limit. The probe prints one `EXRADIUS_PROBE {ok, detail, error}` line, which the worker records on the test. `GET /api/providers/{id}/tests/{test_id}` reports it. Both kinds of test store `last_test`, but only if the provider's credential and settings are unchanged since the test began (`fingerprint`). Tests held by a worker that died are failed at startup.
- **Migration.** At API start, when no provider exists, `EXRADIUS_ANTHROPIC_API_KEY` becomes the default provider "Anthropic (migrated)" (`api_key`), and a warning is logged. After that it is not read. (The migration from a global secret `ANTHROPIC_API_KEY` went with the secrets table.)

**Runtime bundles** (`docs/runtime-bundles.md`)

- **The bundle.** `deploy/runtime-claude-code.Dockerfile` builds `/opt/exradius/runtime` in a Debian stage and copies it into a `scratch` image labelled `exradius.role=runtime-bundle`, `exradius.runtime=claude_code` and `exradius.version`:
  - `python/`: a relocatable CPython 3.14.8 from python-build-standalone (installed by uv), which needs glibc 2.17 or later. The `exradius` package goes in without its server dependencies, plus the `runner` dependency group: the Agent SDK, `httpx` and `pydantic`. Compiled wheels are limited to `manylinux_2_28`, so project images need glibc 2.28 or later. The SDK's own copy of the CLI is deleted.
  - `bin/claude`: native Claude Code 2.1.318, downloaded from `downloads.claude.ai/claude-code-releases` (the installer's source) and checked against the release manifest's SHA-256 for `linux-x64` or `linux-arm64`. That is the version Agent SDK 0.2.178 bundles; the SDK pin and this pin move together.
  - `bin/exradius-runner`: a `/bin/sh` launcher with no `PATH` lookups. It sets `EXRADIUS_RUNTIME_DIR` to its own bundle directory, then runs `python/bin/python3 -I -B -m exradius.runner.main`. `-I` keeps the project image's Python, `PYTHON*` variables, user site and `/work` out of the runner. The launcher sets no `DISABLE_AUTOUPDATER`, because that would reach the agent (see Isolation). The CLI is started by path from a read-only bundle, so an update could not take effect anyway.
  - `VERSION`: `exradius <version>`, `runtime claude_code`, `claude-code <version>`.
  - `ClaudeCodeRuntime` passes `$EXRADIUS_RUNTIME_DIR/bin/claude` as the SDK's `cli_path`. Outside a bundle (tests, development) it leaves `cli_path` unset.
  - The packaging is python-build-standalone plus a launcher rather than PyInstaller: nothing has to be frozen, and the SDK and `mcp` load exactly as in development. `make -C deploy bundle-claude-code` exports the directory to `deploy/build/runtime-claude-code/`.
- **Extraction.** The worker inspects the bundle image named by `EXRADIUS_RUNTIME_IMAGE_<RUNTIME>`:
  - It does this at start, at every container start and at every heartbeat. Once a build is extracted this is one image inspect.
  - Each bundle build gets a volume `exradius-runtime-<runtime>-<version>-<first 12 hex digits of the image id>`, labelled with the runtime, version and image id. The image id is in the name so that rebuilding the same version never changes a volume that running containers use.
  - To fill a new volume, the worker creates (but does not start) a container from the bundle image with the volume mounted over `/opt/exradius/runtime`; Docker copies the image's content into an empty volume at that moment.
  - It then reads `VERSION` through that container. The volume is ready if `VERSION` names the image's version.
  - After startup, the worker removes bundle volumes of other builds, skipping any that a container still uses (Docker refuses to remove them).
  - A missing bundle image does not stop the worker: the heartbeat reports the runtime as not ready, with the error, and runs fail with `runtime claude_code is not available: …`.
- **Run containers.** The bundle volume is mounted read-only at `EXRADIUS_RUNTIME_MOUNT` (`/opt/exradius/runtime`) in run containers and provider probes, but not in setup containers.
  - Every container gets `GIT_CONFIG_COUNT=1`, `GIT_CONFIG_KEY_0=safe.directory` and `GIT_CONFIG_VALUE_0=*`. This command-line level git config replaces the system setting the old runner image carried. Without it, git in a stock image rejects the checkout on Docker Desktop, where the bind-mounted `/work` does not appear owned by the run user.
  - Projects that still name an image built `FROM exradius-runner` keep working: it is a glibc image with git.
- **Isolation** (runtime-bundles.md §5, `exradius/runner/environment.py`). The runner's first act is `isolate()`:
  - It removes `EXRADIUS_RUNTIME_DIR` (the launcher's) and `EXRADIUS_RUN_TOKEN` from its process, and keeps a copy of everything else as the container's environment.
  - It then removes `PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `VIRTUAL_ENV`, `NODE_OPTIONS` and `NODE_PATH` from its own process only.
  - `RunSpec.env` is that copy. `ClaudeCodeRuntime` passes it, plus `CLAUDE_CONFIG_DIR`, as the SDK's `env`. The SDK starts the CLI with the runner's cleaned environment overlaid by `env`, so the CLI, and the agent's shell and stdio MCP servers, get exactly the container's environment (the project's variables and secrets, and the provider credential the worker put there) plus `CLAUDE_CONFIG_DIR`.
  - The only other variables are the SDK's own markers (`CLAUDE_CODE_ENTRYPOINT`, `CLAUDE_AGENT_SDK_VERSION`, `CLAUDE_CODE_SDK_READS_SESSION_STATE`, `PWD`).
  - The bundle is never on `PATH`, so `python`, `node`, `cargo` or `mvn` are the project image's.
  - `tests/test_runner_environment.py` checks this through the real SDK transport against a fake CLI that records its environment. A Docker test checks that a project `PYTHONPATH` holding an `exradius` package, `sitecustomize.py` and `json.py` does not affect the bundle's runner.

**Agent identity** (`docs/agent-identity.md`, `exradius/worker/git_identity.py`)

- **Model.**
  - The identity is the config's `agents.<name>.identity`: `name` defaults to the agent's name, `email` to `<agent>@<project slug>.exradius.local`.
  - Both are validated as git idents: no angle brackets, no control characters, and an `x@y` email.
  - `repositories[].push` (default false) needs a `credential`; a file that would push without one is a config error.
- **At every run container start** (first start and each resume) the worker adds:
  - `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GIT_COMMITTER_NAME`, `GIT_COMMITTER_EMAIL`, `EXRADIUS_AGENT` (slug) and `EXRADIUS_TASK_ID`;
  - `GIT_CONFIG_GLOBAL=<state>/git/config`. These override project variables of the same names.
  - Setup containers and the provider probe get none of this: they have no agent or task, and the probe has no state mount.
- **Global git config in the state mount.**
  - The run's global config lives at `<state>/git/config`, not `$HOME/.gitconfig` (the home also persists, but belongs to the agent and its tools), named by `GIT_CONFIG_GLOBAL` (git 2.32 or later; Ubuntu 22.04 and Debian bookworm images have it). With older git, only the identity variables apply.
  - The worker creates `config` once with just `[include] path = exradius.gitconfig`, and rewrites `exradius.gitconfig` at every start. So the agent's own `git config --global` changes survive resume, and the managed settings follow the current agent and repositories.
  - `exradius.gitconfig` sets `user.name`, `user.email`, `core.hooksPath=<state>/git-hooks`, `push.autoSetupRemote=true`, `init.defaultBranch=main` and the credential helpers. All values are quoted.
- **Commit trailers.** `<state>/git-hooks/prepare-commit-msg` runs `git interpret-trailers --if-exists doNothing` for `Exradius-Task: $EXRADIUS_TASK_ID` and `Exradius-Agent: $EXRADIUS_AGENT`, so amending or rebasing never duplicates them. Because `core.hooksPath` replaces the hooks directory, a repository's own hooks (and any husky-style setup) do not run in v1.
- **Push.**
  - For each repository (in file order, index N) with `push`, a credential and an `http(s)` URL, the token goes into `EXRADIUS_GIT_CREDENTIAL_<N>`.
  - The username is the part before `:` of a `user:token` secret, otherwise `x-access-token`, as for mirrors. Only the token goes into the variable.
  - `exradius.gitconfig` gets `credential.<scheme://host>.useHttpPath=true`, so two repositories on one host can carry different credentials. Each repository URL gets an empty `helper` (which drops helpers from the image's or system config) followed by `!f() { test "$1" = get || exit 0; echo "username=…"; echo "password=$EXRADIUS_GIT_CREDENTIAL_N"; }; f`.
  - The secret is only in the environment; neither the state mount nor the clone's `.git/config` contains it.
  - SSH remotes get no helper. Without `push` there is no helper, and a push fails for lack of credentials. Mirror fetches still use `http.extraHeader` in the worker.
  - `tests/test_git_identity.py` commits in a prepared run directory with real git, and pushes to a `git http-backend` behind Basic auth: the push succeeds with the helper and fails without it.

**Environment section, services and model** (`docs/environment-and-services.md`)

- **Data model.** Model, notes and services now come from the config file (`agents.<name>.model`, `notes`, `services`; see "Config repositories"); only `provider_tests.model` (the model a worker-run provider test uses) is a column. There is no image probe: the design dropped the "Installed" line, so the agent finds its tools itself.
- **Environment section** (`exradius/environment.py`). `config_environment_text` renders the documented layout from the config: the workstation ("built from the image x", or "built from <dockerfile>" for a built one, the path from the repository root), its repositories in file order ("the default branch" when the file gives no `branch`), the fixed "Not available" line, its services in file order with their settings (a built service shows as `(built from <dockerfile>)`), the fixed closing paragraph and the notes (stripped; left out when empty). Repository URLs lose `user:password@`, the query and the fragment; scp-like `git@host:path` URLs are kept as written. "pushing allowed" means what the worker actually configures: `push`, a credential and an http(s) URL. With no repositories the second line reads "The working directory contains no repositories."
  - Service settings (`service_settings`): each env entry as `KEY=value`, plain values first, then those that use secrets, each group in stored order. A `${secret:NAME}` shows as `$NAME`, because the workstation exports every secret its services' settings use under its own name (`resolve.exported_secrets`); a name that is not a valid variable name (it has `.` or `-`) is not exported and shows as `<a secret you do not have>`. Secret values never appear. When any setting names a variable, the list ends with "A value written as $NAME is the value of your environment variable NAME."
  - The API builds the section for each run spec (`RunSpecResponse.environment_text`, so it is rebuilt at every container start, resume included) and for the resolved agent view (`ResolvedAgentOut.environment_text`, with `image` and `model`). `build_system_prompt` joins rules, environment and role with blank lines and skips empty parts.
- **Model.** `agents.<name>.model` in the config; absent is the runtime's default. Ids are validated loosely (`^[A-Za-z0-9][A-Za-z0-9._:@/\[\]-]*$`), so any id the provider accepts works. The run spec carries `model`; `ClaudeCodeRuntime` passes it as `ClaudeAgentOptions.model` and to the provider probe. The model catalog is `providers.MODELS` (anthropic: `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-haiku-5-5`, each with a one-line description), returned as `models: [{id, description}]` on each provider kind of `GET /api/system/runtimes` and on `ProviderOut`.
  - `POST /api/providers/{id}/test` takes an optional body `{model}`. For `api_key` it calls `GET /v1/models/{model}` (free; the id or alias must exist for the key) instead of listing models. For `oauth_token` the worker's probe runs `exradius-runner --probe claude_code --model <id>`. A test with a model is stored as `last_test` only when it passes, since a wrong id says nothing against the credential.
- **Secret references** in `env` and in services' `env` resolve from the store at container start, for run and setup containers alike. A missing key fails the run in `preparing` (in Verify, "Read config" already fails).
- **Services in a run** (`ContainerManager.create_run_network`, `start_service`, `remove_run_services`).
  - In `preparing`, after cloning and attachments and before setup commands, the worker creates the bridge network `exradius-run-<run_id>` (labels `exradius.run_id`, `exradius.kind=network`). It then starts each service in order on that network, with the alias and hostname `name`, labels `exradius.run_id`, `exradius.kind=service`, `exradius.service=<name>`, `cap_drop ALL` plus `CHOWN SETUID SETGID FOWNER DAC_OVERRIDE NET_BIND_SERVICE`, `no-new-privileges`, no mounts and no published ports. The env is decrypted at that moment. The image's own user, root filesystem and entrypoint are kept, and `containers.run` pulls a missing image.
  - Readiness is polled every second for 120 s, counted from container start, so a pull does not count. `{"command"}` runs `docker exec` in the service until it exits 0. `{"tcp": p}` runs `cat /proc/net/tcp /proc/net/tcp6` in the service and waits for a LISTEN socket on `p`; the worker is not on the run network, so it cannot connect itself, and this needs only `cat` in the image. `null` waits for `Health.Status=healthy` when the image has a HEALTHCHECK, otherwise for one second of "running". A service that exits or times out fails the run with `service <name> (<image>) …`, the last check's output and the container's last 40 log lines.
  - Setup and run containers are created on `EXRADIUS_RUN_NETWORK` and the run network together, through `networking_config` at create (Docker API 1.44+), so neither network is missing when the command starts. Runs of projects without services get no network of their own.
- **Networks** (fix after BANK-204, 2026-11-07). Runs used to join the stack network `exradius`, where the compose service `postgres` carries the alias `postgres`; Docker resolved a run's `postgres` to Exradius's own database instead of the run's service. Now:
  - `exradius` (compose `default`): api, worker, frontend, postgres, rabbitmq. No run container joins it.
  - `exradius-runs` (compose `runs`; not internal, runs need outbound access): the API, with the alias `exradius-api`, and every run, setup and provider-probe container. The worker is not on it; it talks to the Docker daemon, not to runs.
  - `exradius-run-<run_id>`: the run's services, its setup containers and its run container, only when the project has services.
  - The delivery check has `network_mode=none`.
  - Settings: `run_network` defaults to `exradius-runs` and `run_api_url` to `http://exradius-api:8000`; the compose file sets both for the worker. `run_network=None` uses Docker's bridge (the Docker integration tests do so, as they need no API). Runs on `exradius-runs` can still resolve each other's containers by name; they are separate from the stack, not from each other.
  - `waiting_for_input` leaves services running. A resume calls `start_service` again for each service, which keeps a running container and recreates a missing one (for example after a daemon restart), then joins the network. `finalize` removes the services with their anonymous volumes (`remove(v=True)`; images such as postgres declare `VOLUME`s) and the network, after publishing the result and before deleting the directory.
  - `reconcile` no longer removes `service` containers of runs it re-attaches or settles. `sweep_orphans` first removes services and run networks whose label is neither an unfinished run nor an open verification.
- **Verify.** After "Read config", the builds and "Prepare attachments folder" come "Start service <name>" steps (the first creates the network; the log reads `<image> is ready after <s>s (<check>); host name <name>`). Setup commands then join the network, and before "Remove directory" a cleanup step "Stop services" always runs when the project has services. A dead worker's verification is cleaned up with its services and network.
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
- **Reading** (`exradius/config_repo.py`, `exradius/project_state.py`). The mirror is `<data_dir>/config-repos/<project id>.git`, fetched under the same file lock as code mirrors with the credential of the URL's host (`git_credentials`; a token is sent as `x-access-token:<token>`, `basic` as given; local paths and `file://` have no host). Without `config_branch`, the branch is the remote's default (the mirror's `HEAD`, which follows the remote at every fetch). The file is `config_path`, or `exradius.yaml`, then `.exradius/exradius.yaml`. A repository, branch or file that cannot be read raises `ConfigReadError`; callers turn it into a snapshot with that error and no commit. Reads happen in `POST /api/projects`, re-read and PATCH (API), and at intake, in `preparing` and in Verify (worker); each stores the snapshot on the project.
  - Health (`project_state.assess`): errors are the file's plus a slug change ("project.slug: the file says 'x', but this project was added as 'y'; …"; the row keeps its slug). Missing keys are the required keys the store lacks (all of them when it does not exist). The reason, in order: `config error: <first error>`, `secret store S does not exist`, `missing secret A in store S` / `missing secrets A, B in store S`. A file that names a store but references no secret does not need the store.
- **Intake** reads the config afresh (not the cache), so an agent added to the file is known to the next task. Rejections: `unknown project 'x'`, the health reason, `unknown agent 'x' in project 'p' (it defines a, b)`, `no provider configured` / `unknown provider 'x'`, and the existing ones. `POST /api/tasks` still checks nothing itself; the panel sees the rejection as a `rejected` status event on the task's stream.
- **Preparing** reads the config again, fails the run with the health reason or "agent 'x' is no longer defined in project 'p'", records `config_commit`, `config` and `agent_snapshot`, builds the images, then clones, writes attachments, starts the services and runs `workstation.setup`, all from that config. Container start (first start and each resume) takes model, provider, runtime, identity and connections from `runs.config`; the run spec carries the role as `agent_prompt`.
- **Secrets in the workstation.** The workstation environment is the config's `env` with secrets substituted, plus every store key that the services' settings reference, under its own name, so the environment section's `$DB_PASSWORD` is true. Repository credentials and connection values are used by reference only and never exported. Provider variable names in `env` are dropped as before.
- **Builds** (`exradius/worker/builds.py`). One job per Dockerfile (`workstation`, then services in file order). The tag is `exradius/<project slug>/<name>:<first 12 hex digits>` of the context's git tree hash at the run's commit; when the Dockerfile lies outside the context, of the SHA-1 of `<tree hash>:<Dockerfile blob hash>`, so editing that Dockerfile rebuilds too (the design hashed the context only). The context is `git archive` of that tree, sent to the Docker daemon as a tar (`api.build(custom_context=True)`); a Dockerfile outside the context is added to the tar as `.exradius.Dockerfile`. A file lock `<data_dir>/locks/build-<project id>.lock` serialises a project's builds across workers; an image that exists with the tag is not rebuilt and gets no `image_builds` row. Build output (steps, pull status lines without progress bars, errors) goes to `log_tail` (4000 characters). A failed build fails the run in `preparing` with "building <name> from <dockerfile> failed:" and the tail. Built images carry the label `exradius.build=<name>`; nothing removes old tags yet.
- **Catalog** (`exradius/catalog.py`): github, gitlab, postgres, slack, sentry, linear, jira, fetch, playwright, filesystem, each with transport, command and args or URL, `requires`, and config keys (`key`, `label`, `required`, `target` env|header|arg, `name`, `format`, `default`). `expand` gives the `mcpServers`-style definition; `GET /api/catalog` lists the entries. Optional keys with a default: gitlab `api_url` (`https://gitlab.com/api/v4`), filesystem `path` (`/work`).
- **API** (§11). `POST /api/projects {config_url, config_branch?, config_path?}` answers 201 with the project, 422 with `detail: [errors]` when nothing can be read or the file names no valid `project.slug`, and 409 when the slug exists. `ProjectOut`: `id, slug, name, description, config_url, config_branch, config_path, commit, read_at, branch, path, health (ok|config_errors|missing_secrets), runnable, reason, errors, warnings, store, store_id, store_exists, required_keys [{name, present}], missing_keys, agents [slug], config (parsed, references as written), raw (the file's text), created_at`. Warnings include agents naming a provider that does not exist. `GET` answers from the cache; `POST …/reread` and `PATCH {config_branch, config_path}` (blank or `null` = default) read again. `DELETE` answers 409 while the project has tasks and removes the config mirror. `GET /api/projects/{id}/agents/{slug}/resolved` (409 when the config has errors): `project_id, commit, agent, name, role, model, runtime, provider, resolved_provider {id, slug, name, kind, auth_method} | null, provider_error, identity {name, email}, connections [{name, catalog, transport, command, args, url, env, headers, secrets}], image, environment_text`, with secrets as `$NAME`. `GET/POST /api/git-credentials`, `DELETE /api/git-credentials/{id}` (host lowercased and unique, 409 on a second one; values never returned). Stores: `GET/POST /api/stores` and `GET/DELETE /api/stores/{id}` (`StoreOut {id, name, kind, created_at, keys, used_by}`; delete is 409 while a project's latest read names it), `GET /api/stores/{id}/keys` (`[{key, updated_at}]`), `PUT /api/stores/{id}/keys/{key} {value}`, `DELETE /api/stores/{id}/keys/{key}`. `RunOut` gains `config_commit` and `agent_snapshot`. Removed: repositories, env vars, secrets, connections (and their test), agents, attachments of agents, services and the repository check; "test connection" has no replacement yet, so `mcp` left the server dependencies.

**Verification without clicks** (`docs/setup-and-verify.md` §1, implemented 2026-11-07)

- Migration `0002` adds `project_verifications.commit` (set by "Read config", also when the file has errors) and `diagnosis` (written by `_finish` for a failure from `exradius/worker/diagnose.py`: substring and regex rules over the first failed step's name, command and log tail, first match wins, `null` when nothing matches).
- `POST /api/projects` asks for a verification once the file was read (with or without validation errors); re-read and `PATCH` ask for one when `snapshot_commit` changed to a commit (the PATCH case is not in the design: it is a re-read with another branch or path). At most one verification per project is `pending` or `running`: a request then returns that one (the project row is locked while checking), and the manual Verify answers with its id. When the broker cannot be reached, the automatic request leaves the new row failed with `broker: …` and the add or re-read still succeeds; the manual Verify answers 503 as before.
- `ProjectOut` gains `verification` (the latest one's `id, state, commit, ended_at, diagnosis`) and `freshness` (`current`, `stale`, `failed`, `running`, `never`; passed at another commit than `snapshot_commit` is `stale`, so are rows from before `0002`). `VerificationOut` gains `commit` and `diagnosis`. Nothing is gated on freshness.

**The run environment** (`docs/run-environment.md` §3-§8, implemented 2026-11-07; the principle is §14)

- **Git at the command-line level** (`worker/git_identity.py`). `prepare_git` returns the
  identity, the credential variables and `GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n`:
  `safe.directory=*`, `core.hooksPath=<state>/git-hooks`, `credential.<host>.useHttpPath`,
  and per push remote an empty `credential.<url>.helper` (drops every helper read before
  the command line) followed by ours. The container gets them after every other variable,
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
- **Memory** (`worker/memory.py`, `MemoryGate` on `RunManager.memory`, shared by runs and
  verifications). The budget is `EXRADIUS_RUN_MEMORY_BUDGET` (a Docker size) or the
  daemon's `MemTotal` less 1 GiB, set at worker start; a daemon that does not answer leaves
  it unknown and nothing waits. Intake returns the reservation from the config it read
  (`ProjectConfig.memory_reservation()`); `execute` waits inside the concurrency slot, so
  the broker keeps the backlog, but the task message was already acknowledged by intake
  (the design said unacknowledged: intake must persist the task before it knows the size).
  While it waits the run stays `queued` with `runs.waiting_for = 'memory'` and the
  worker's id (migration `0003`), and no status event is published; `task.status` is
  unchanged. `TaskSummaryOut` and `RunOut` carry `waiting_for`, and the panel's task row
  says "waiting for memory". A restarted worker takes over queued runs with
  `waiting_for` whose worker is dead (`waiting_runs`, in `recover`). A waiter rechecks
  every 10 s whether its run is still queued, so a cancelled run stops waiting. A resume
  reserves the same amount (the run stays `waiting_for_input` meanwhile); a re-attached
  container takes its reservation without waiting. More than the whole budget fails the
  run at once ("the project declares 12g of memory, more than this worker's budget for
  runs (7g, EXRADIUS_RUN_MEMORY_BUDGET)"). While a run waits for an answer its services
  keep running but nothing is reserved.
  - Declared memory is `mem_limit` and an equal `memswap_limit` on the setup, run and
    resume containers and on services; undeclared sets neither. Exit 137 under a declared
    limit becomes "the run exceeded its declared memory (6g) and was killed (exit 137)",
    "setup command '…' exceeded its declared memory (6g) …", or for a service that dies
    before it is ready "service <name> (<image>) exceeded its declared memory (512m): …".
  - Verify adds "Reserve memory (6.5g)" as its first step after "Read config" when anything
    is declared, held until the verification ends; its log is "reserved 6.5g of the
    worker's 14g". `diagnose.py` names a declared limit that was exceeded and a reservation
    larger than the budget.
- **Caches** (`worker/caches.py`). `<data_dir>/caches/<slug>/<n>-<sanitised path>` (index
  in the list, the path's characters outside `[A-Za-z0-9._-]` as `_`), created for the run
  user and bind-mounted into setup, run and resume containers and Verify's setup steps. A
  cache below the home gets its mount point created in the run's home directory for the
  run user first: Docker would create the missing parents as root and `~/.cache` would no
  longer be writable. `ProjectOut.cache_bytes` (allocated blocks, hard links once) is
  computed on `GET /api/projects/{id}` only, not on the list; `null` without a cache
  directory. The walk took 4 s for Plane's 1.2 GB (75,000 files) where the data directory
  is a Docker Desktop file share, so the API keeps a size for a minute and then returns it
  while a background walk refreshes it (`caches.SizeMemo`); only the first view of a
  project waits. `DELETE /api/projects/{id}` removes the project's caches.
- **Service `command`** is the container's `command`; the entrypoint is the image's.
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
> true inside a run, is enforced at the level of the kernel, Docker or the command line,
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

Where each thing Exradius sets inside a run lives now (the audit of
`docs/run-environment.md` §2.1, after that batch):

| What | Level |
|---|---|
| uid 1000 as `runner`, home `/home/runner` | passwd and group files bind-mounted |
| read-only root, no capabilities, no-new-privileges | Docker |
| networks (`exradius-runs` + the run's own) | Docker |
| `/work`, home, `/tmp`, state, attachments, caches | bind mounts |
| declared memory | cgroup limit (Docker) |
| `safe.directory`, `core.hooksPath`, push credential helpers | git command line (`GIT_CONFIG_*`) |
| author and committer identity | `GIT_AUTHOR_*`/`GIT_COMMITTER_*`, plus the managed global config |
| `push.autoSetupRemote`, `init.defaultBranch` | managed global config (conveniences a project may change) |
| Claude Code settings the runtime needs | `--settings` (only managed settings rank higher; §12) |

A new requirement inside a run follows the same rule: find the level a project's own
tooling cannot reach, or record the gap in §12.
