# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Exradius is a self-hosted, execution-only harness for AI agents. Tasks arrive on a RabbitMQ
queue naming a `project` and an `agent` slug; a worker reads the project's config repository,
clones its code repositories, starts its dependency services, runs one hardened container per
task through the Claude Agent SDK, and publishes status/question/result events back on a topic
exchange. Managing tickets, chat or CI is the job of an integration layer outside this repo.

The design lives in `docs/` and is the source of truth when code and prose disagree:
`architecture.md` (system, message contracts, run lifecycle, state machine, §12 implementation
notes, §13 operating rules), `config-repo.md` (the current project model), `environment-and-services.md`,
`runtime-bundles.md`, `providers.md`, `agent-identity.md`, `onboarding.md` (panel),
`integration-api.md` (hook, directory, bridge files, bridge status), `agent-routing.md`. These are
internal design notes: Exradius ships no user documentation, and the panel shows state, not help.
The README's "Configuring a project and an agent" section predates the config-repo model
(there are no `/api/agents`, `/api/projects/{id}/repositories` or `/api/secrets` routes any more).

## Commands

Backend (Python 3.14, uv):

```bash
cd backend && uv sync                       # installs the dev group (runner deps, pytest, aiosqlite)
cd backend && uv run pytest                 # unit tests: SQLite, in-memory object store, local bare git repos
cd backend && uv run pytest tests/test_intake.py -k rejects   # one file / one test
cd backend && uv run exradius generate-key  # a Fernet EXRADIUS_SECRET_KEY
cd backend && uv run alembic upgrade head   # migrations (the API container does this at start)
```

`tests/test_integration.py` skips unless `EXRADIUS_TEST_BROKER_URL` is set (RabbitMQ) and/or
Docker is reachable with `exradius-project-default:0.1.0` and `exradius-runtime-claude-code:0.1.0`
built. There is no linter configured for the backend.

Frontend (Node 24, Vite):

```bash
cd frontend && npm install
cd frontend && npm run dev      # http://localhost:5173, proxies /api to $EXRADIUS_API_URL or localhost:8000
cd frontend && npm run lint     # eslint, including react-hooks rules
cd frontend && npm test         # vitest (jsdom); single file: npx vitest run src/lib/projectConfig.test.ts
cd frontend && npm run build    # tsc -b, then vite build
```

Stack (Docker Compose v2; `deploy/.env` from `deploy/.env.example`, needs `EXRADIUS_SECRET_KEY`,
`EXRADIUS_ADMIN_PASSWORD`, absolute `EXRADIUS_DATA_DIR`):

```bash
docker compose -f deploy/docker-compose.yml up -d --build
docker compose -f deploy/docker-compose.yml up -d --build api worker   # after backend changes
docker compose -f deploy/docker-compose.yml build project-default runtime-claude-code
make -C deploy bundle-claude-code   # the runtime bundle directory alone, in deploy/build/
```

Panel http://localhost:3000, API http://localhost:8000 (`/docs`), RabbitMQ UI :15672.
Publishing a task from a shell: `uv run exradius publish-task --project <slug> --agent <slug>
--task-id <id> --instructions ...` and `uv run exradius events --task-id <id> --until-result`
(broker from `EXRADIUS_BROKER_URL`). A task id that was already used, even by a failed task,
is rejected as a duplicate.

## Architecture

**Config-repo model (docs/config-repo.md).** A project is a git repository holding
`exradius.yaml` (or `.exradius/exradius.yaml`): slug, repositories, workstation image or
Dockerfile, services, env, notes, connections, agents, and the name of one `secret_store`.
Nothing about a project is edited in the panel. The admin owns only: secret stores and their
keys, model providers, and one git credential per host for reading config repositories.
`${secret:NAME}` references are allowed only in repository credentials, service env, `env`,
and connection config/env/headers; the set of names referenced is the project's required keys.
The schema is strict pydantic with `extra="forbid"` in `schemas/project_config.py`.

- `config_repo.py` mirrors the repo, reads the file at the branch head and returns a
  `ProjectConfigSnapshot` (commit, config or errors, warnings, required_keys, store).
- `project_state.py` wraps it with the host credential, the store, and the runnable check;
  the API caches the latest snapshot on `projects.snapshot` for the panel only.
- The worker never trusts that cache: intake and `preparing` read the file afresh and
  record `runs.config_commit`. Tasks for a project with errors or missing keys are rejected
  with the reason.
- `resolve.py` turns the config into run inputs; secrets are decrypted only there, at run
  start. Everything shown to the panel, the agent's environment section, events and logs
  uses `$NAME` placeholders.
- `catalog.py` holds the MCP connection catalog (github, postgres, slack, ...); custom
  connections use the `mcpServers` shape.

**Backend package `backend/exradius/`.**

- `api/`: FastAPI. `/api/*` is the admin API (session cookie, single admin user):
  `config_routes.py` (projects, stores, git credentials, catalog, verification),
  `provider_routes.py`, `task_routes.py` (tasks, events, transcripts, signed downloads,
  per-task SSE stream), `system_routes.py`. `internal.py` is `/internal/runs/{run_id}/*`,
  authenticated by the per-run token that only the runner holds.
- `worker/`: `main.py` consumes `exradius.tasks`, `exradius.answers` and `exradius.control`
  (cancel, verify, test_provider) with concurrency = prefetch count. `intake.py` accepts or
  rejects; `runs.py` (`RunManager`) orchestrates preparing → running → waiting/resuming →
  finalize; `prepare.py` clones repositories into `runs/<run_id>/project` (the container's `/work`); `builds.py` builds
  Dockerfile images tagged `exradius/<project>/<name>:<tree hash>` under a per-project lock;
  `containers.py` starts services on the per-run network `exradius-run-<run_id>` and the
  workstation container; `delivery.py` adds report-only warnings when work was left unpushed
  (the worker never commits or pushes); `verify.py` runs the panel's Verify steps.
- `states.py` is the task state machine; `lifecycle.py` performs transitions. Postgres is
  the source of truth: every state change locks the run row, stores an `events` row in the
  same transaction, and the event is published after commit (`published_at` marks success,
  so unpublished events survive restarts). Finalize happens exactly once, guarded by
  `runs.ended_at`.
- `runner/`: the process inside the run container (`exradius-runner <run_id>`). It fetches
  the run spec from the internal API, hosts the built-in `exradius` MCP server
  (`request_information`, `submit_result`, attachments) in `tools.py`, builds the prompts in
  `prompt.py`, and streams transcript events back. `runner/environment.py` strips the
  runner's own interpreter variables and the run token so the agent sees only the project's
  environment.
- `runtimes/`: the `Runtime` protocol (`base.py`: `RunSpec`, `ConnectionSpec`, `RunOutcome`)
  and `claude_code.py`, the only implementation. The runtime writes Claude Code settings
  into `CLAUDE_CONFIG_DIR` (for example `includeCoAuthoredBy: false`).
- `models/`: SQLAlchemy models plus Alembic migrations. The chain was reset to a single
  `0001_initial_schema.py` with the config-repo model; no older data is migrated.
- `storage.py`: object store for transcripts, artifacts and attachments (filesystem under
  `<data_dir>/objects/`, or S3). `messaging/`: queue/exchange names and the publisher.
- `cli.py`: the `exradius` CLI (publish-task, answer, events, generate-key).

**Prompt layout (architecture §13, prompt.py).** System prompt = Claude Code's preset, then
the fixed Exradius operating rules, then a generated `# Your environment` section from the
snapshot (`environment.py`: image, repositories with branch and push, services with non-secret
settings, notes), then the agent's optional `role`. The user message is `# Task <task id>`,
context and attachments only. Tickets never mention Exradius; agents learn how they run from
the rules alone (folders are git clones, branch name starts with the task id, push to origin
or attach a patch).

**Run isolation (runtime-bundles.md).** Three images: `exradius-backend` (api + worker, no
Agent SDK), `exradius-runtime-claude-code` (a relocatable Python with the Agent SDK and a
pinned native Claude Code binary; no Node), `exradius-project-default`. The worker copies
the bundle into a Docker volume and mounts it read-only at `/opt/exradius/runtime`, so a
project's workstation can be any glibc image with bash and git (no Alpine). Run containers
are uid 1000 with a generated `/etc/passwd` entry (`runner`, home `/home/runner`), read-only
rootfs, all capabilities dropped; the run home, `/tmp` and `/work` are bind mounts under
`runs/<run_id>/` on disk (not tmpfs: package installs exhausted RAM). Runs join only the
`exradius-runs` network (where the API is aliased `exradius-api`) plus their own service
network, never the stack network, so a service named `postgres` cannot resolve to Exradius's
own database.

**Frontend `frontend/src/`.** React 19 + TypeScript, TanStack Router (`router.tsx`) and
Query (`lib/queries.ts`), Tailwind 4, Radix primitives, `lib/api.ts` as the only API client.
Pages: Overview, Projects (detail tabs config / secrets / verify / tasks), Stores, Providers,
Tasks, Settings. The panel talks to `/api` on the same origin; nginx (compose) or the Vite
proxy (dev) forwards it. Forms offer known values and only ask for free text when the
information exists nowhere else.

## Conventions worth knowing

- Secrets and provider credentials are write-only through the API and encrypted at rest
  with `EXRADIUS_SECRET_KEY` (`crypto.py`). Never log or return their values.
- The worker does the harness's job only: it does not commit, push, open pull requests or
  otherwise act on behalf of the agent. Delivery problems become warnings in the result.
- Unknown keys in `exradius.yaml` are errors, not warnings; keep validators strict.
- Tests build config repositories as local bare git repos (`tests/conftest.py`:
  `make_config_repo`, `commit_config`, `configured`); follow that pattern rather than mocking
  the git layer.
- Commit messages are short imperative subjects without a scope prefix (see `git log`).
