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
   - `attachments` are written to `project/.exradius/attachments/`.
2. For each repository of the project: refreshes the per-project bare mirror at `/var/lib/exradius/mirrors/<project>/<repo>.git` with `git fetch`, then clones from the mirror into `project/<repo-folder>/` with `git clone --local` (hardlinked objects, fast) and checks out the configured branch.
3. Runs the project's setup commands with `project/` as working directory, inside the run image (so toolchains match). Non-zero exit fails the run.

Nothing is shared between concurrent runs except the read-only mirrors. Any number of agents can work on the same project at once.

### 5.2 Running

The worker starts a container from the project's image (default: the `exradius-runner` base image with Python 3.14, Node, git and the Claude Agent SDK) with:

- `project/` mounted at `/work`, the working directory;
- `state/` mounted at the runtime's state location;
- environment: the project's variables, the project's secrets (decrypted at this moment only), the global Anthropic API key, `EXRADIUS_RUN_TOKEN` and `EXRADIUS_API_URL`;
- the runner as entrypoint, receiving the run id.

The runner fetches the run spec from the internal API (prompt, connections, task instructions, context, attachment list), assembles the Claude Agent SDK options and starts the session. The prompt layout is:

```
<agent prompt>

# Task
<instructions>

# Context
<context json, pretty-printed>

# Attachments
- .exradius/attachments/screenshot.png (image/png)
```

The prompt never mentions workspaces or Exradius internals beyond the `exradius` tools below. The agent simply starts in a directory that contains the project's repositories.

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
repositories      id, project_id, name (folder), url, branch, auth_secret_id
env_vars          id, project_id, key, value
secrets           id, project_id (nullable = global), key, value_encrypted
connections       id, slug, name, transport (stdio|http), command, args[], url, env (json, may reference secrets by name)
agents            id, slug, name, prompt, runtime (enum), created_at
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
```

Secrets are encrypted with a key from the Exradius environment. They are decrypted only at container start and never returned by the admin API.

## 7. Runtime interface

```python
class Runtime(Protocol):
    name: str

    async def start(self, spec: RunSpec, events: EventSink) -> RunOutcome: ...
    async def resume(self, spec: RunSpec, session_id: str, message: str,
                     events: EventSink) -> RunOutcome: ...

@dataclass
class RunSpec:
    run_id: str
    working_dir: Path
    state_dir: Path
    prompt: str
    connections: list[ConnectionSpec]   # includes the built-in exradius server
    env: dict[str, str]

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
- The internal run API accepts only the per-run token, which is revoked when the run ends.

## 10. Repository layout and stack

Single repository, two packages:

```
exradius/
  backend/            Python 3.14, uv, FastAPI, SQLAlchemy + Alembic, aio-pika, docker SDK
    exradius/
      api/            admin API + internal run API
      worker/         broker consumer, run preparation, container control
      runner/         container entrypoint, SDK session, exradius MCP server
      runtimes/       Runtime protocol, ClaudeCodeRuntime
      models/, schemas/, messaging/
    tests/
  frontend/           React + TypeScript + Vite
  deploy/
    docker-compose.yml   api, worker, frontend, postgres, rabbitmq
    runner.Dockerfile    exradius-runner base image
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
- One image, `exradius-runner`, serves three roles. It is the run container base image, and the api and worker services run from it with a different command. The API needs Node to start stdio MCP servers for "test connection", and the worker needs git.
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
- `connections.headers`: for http transport.
- `events.created_at`.
- `events.task_id` is not a foreign key, because rejected tasks are not stored in `tasks` (see below).
- JSON columns use JSONB on Postgres. `setup_commands` and `args` are JSON lists rather than SQL arrays.

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
- Setup commands run one container each, from the project image, with `/work` mounted and the project's variables and secrets. A non-zero exit fails the run, and the tail of the command's output becomes the error. Setup commands do not run again on resume.

**Run containers and directories**

- `state/` is mounted at `/exradius/state`. `ClaudeCodeRuntime` sets `CLAUDE_CONFIG_DIR=/exradius/state/claude`, so session files survive suspend/resume. The working directory is always `/work`, which keeps session paths stable across containers.
- Containers run as `1000:1000` with these settings:
  - a read-only root filesystem;
  - `cap_drop: ALL` and `no-new-privileges`;
  - tmpfs for `/tmp` and for `HOME=/home/runner`;
  - membership of the network named by `EXRADIUS_RUN_NETWORK`, so they reach the API at `EXRADIUS_RUN_API_URL`.

  The worker runs as root and chowns `project/` and `state/` to the run user.
- Attachments are written root-owned with mode `0444`. Their directory is also bind-mounted read-only over `/work/.exradius/attachments`. Answer attachments go to `.exradius/attachments/<request_id>/`.
- The worker sees its data directory at `EXRADIUS_DATA_DIR`. Bind mounts are created by the host's Docker daemon, so `EXRADIUS_HOST_DATA_DIR` gives the same directory as the host sees it. The compose file mounts both at the same host path.
- Mirrors are created with `git clone --mirror`, refreshed with `git fetch --prune` and serialised with a file lock per mirror. Clones use `git clone --local --branch <branch>`, after which `origin` is reset to the real remote URL. A repository's `auth_secret_id` is used only for the mirror fetch, as an HTTP Basic `extraHeader` (a bare token becomes `x-access-token:<token>`). It is never written into the clone, so an agent that needs to push must get credentials through the project's secrets.
- The run token is a fresh random token for each container start, stored hashed and cleared when that container exits. The runner removes it from its own environment before starting the SDK, so the agent's tools do not inherit it.

**Prompt and runtime**

- `# Context` is omitted when `context` is null, and `# Attachments` when there are none.
- The resume message reads "Answer to your question: <question>", then the answer, then the paths of any answer attachments.
- `ClaudeCodeRuntime` uses these SDK options:
  - the `claude_code` system prompt preset;
  - `permission_mode="bypassPermissions"` (capability equals the tools attached);
  - `strict_mcp_config=True`.

  It interrupts the session once `request_information` has been called, and resumes with the SDK `resume` option.
- The built-in `exradius` server is registered with `alwaysLoad: true`, so its tools are always in the prompt and never deferred behind Claude Code's tool search.
- Each SDK message is stored as one transcript chunk, a JSON object with a `type` field. Sequence numbers continue across containers of the same run.

**Configuration resolution**

- Connection `env` and `headers` values reference secrets with `${secret:NAME}`. A reference resolves to the project's secret first, then to the global one.
- `project_agents.secret_overrides` maps a referenced name to another secret name.
- `project_agents.connection_overrides` maps a connection slug to fields that replace `command`, `args` and `url`, or that merge into `env` and `headers`.
- The connection slug `exradius` is reserved for the built-in server.
- Only project-scoped secrets are exported into the container environment. Global secrets are used only by reference.
- The global Anthropic key is the global secret `ANTHROPIC_API_KEY` if one exists. Otherwise it comes from `EXRADIUS_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY` in the worker's environment.
- "Test connection" runs in the API container with a 60-second timeout. It can take `project_id` and `agent_id` to apply that project's secrets and overrides.

**Admin**

- The admin user is created at API start from `EXRADIUS_ADMIN_USERNAME` and `EXRADIUS_ADMIN_PASSWORD`. Its password is reset if that value changes.
- Passwords are hashed with scrypt.
- The session is a signed cookie (`exradius_session`).
- Secrets are encrypted with Fernet using `EXRADIUS_SECRET_KEY`. The API accepts and replaces their values but never returns them.
