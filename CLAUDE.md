# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Exradius is a self-hosted, execution-only harness for AI agents, running on Kubernetes. Tasks
arrive on a RabbitMQ queue naming a `project` and an `agent` slug; a worker reads the
project's config repository and creates a `Run` object; an operator turns it into one
hardened pod (clones, setup, the agent through the Claude Agent SDK); the worker publishes
status/question/result events back on a topic exchange. Managing tickets, chat or CI is the
job of an integration layer outside this repo.

The design lives in `docs/` and is the source of truth when code and prose disagree:
`architecture.md` (system, message contracts, run lifecycle, state machine, §12 implementation
notes, §13 operating rules), `kubernetes.md` (the Run resource, pods, operator, chart, local
loop) and `kubernetes-plan.md` (the phases: phase 1 runs image-based projects without
services; builds and services come in phases 2 and 3), `config-repo.md` (the current project
model), `environment-and-services.md`,
`runtime-bundles.md`, `providers.md`, `agent-identity.md`, `onboarding.md` (panel),
`integration-api.md` (hook, directory, bridge files, bridge status), `agent-routing.md`. These are
internal design notes: Exradius ships no user documentation, and the panel shows state, not help.
The README's "Configuring a project and an agent" section predates the config-repo model
(there are no `/api/agents`, `/api/projects/{id}/repositories` or `/api/secrets` routes any more).

## Commands

Backend (Python 3.14, uv):

```bash
cd backend && uv sync                       # installs the dev group (runner deps, pytest, aiosqlite)
cd backend && uv run pytest                 # unit tests: SQLite, in-memory object store, a fake Kubernetes API, local bare git repos
cd backend && uv run pytest tests/test_intake.py -k rejects   # one file / one test
cd backend && uv run exradius generate-key  # a Fernet EXRADIUS_SECRET_KEY
cd backend && uv run exradius migrate       # alembic upgrade head under an advisory lock (the pods' init containers run it)
```

`tests/fakes.py` has `FakeCluster` (the Kubernetes API with a stand-in operator and runner);
run-lifecycle tests use it. `tests/test_integration.py` skips unless
`EXRADIUS_TEST_BROKER_URL` is set (RabbitMQ). There is no linter configured for the backend.

Operator (Go, controller-runtime; `go.work` at the root):

```bash
make -C operator test        # unit tests (pod builder goldens, status mapping) and envtest
make -C operator manifests   # regenerate the CRD into charts/exradius/files/crds/
make -C operator generate    # deepcopy
```

Frontend (Node 24, Vite):

```bash
cd frontend && npm install
cd frontend && npm run dev      # http://localhost:5173, proxies /api to $EXRADIUS_API_URL or localhost:8000
cd frontend && npm run lint     # eslint, including react-hooks rules
cd frontend && npm test         # vitest (jsdom); single file: npx vitest run src/lib/projectConfig.test.ts
cd frontend && npm run build    # tsc -b, then vite build
```

Local cluster (kind, kubectl, helm, tilt):

```bash
make -C deploy/kind dev-up      # registry localhost:5001, kind cluster "exradius", CloudNativePG, RabbitMQ operator, Secrets
tilt up                         # build every image, install charts/exradius with values-local.yaml, live-update Python
tilt down                       # uninstall (the exradius namespace and its Secrets stay)
make -C deploy/kind dev-down    # delete the cluster and the registry
make -C deploy/kind images install   # the same without Tilt, images tagged with the version
make -C deploy/kind e2e PROJECT=<slug> [AGENT=<slug>]   # publish one task, wait for its result
kubectl --context kind-exradius -n exradius-runs get runs,pods -w
kubectl --context kind-exradius -n exradius get secret exradius-admin -o jsonpath='{.data.password}' | base64 -d
```

Panel http://localhost:3100, API http://localhost:8100 (`/docs`), RabbitMQ `localhost:5673`
and its UI :15673 (Tilt's port-forwards; they avoid a v0.1.0 compose install's ports).
Publishing a task from a shell: `uv run exradius publish-task --project <slug> --agent <slug>
--task-id <id> --instructions ...` and `uv run exradius events --task-id <id> --until-result`
(broker from `EXRADIUS_BROKER_URL`, credentials in the `exradius-rabbitmq-default-user`
Secret). A task id that was already used, even by a failed task, is rejected as a duplicate.

## Architecture

**Config-repo model (docs/config-repo.md).** A project is a git repository holding
`exradius.yaml` (or `.exradius/exradius.yaml`): slug, repositories, workstation image or
Dockerfile, services, env, notes, connections, agents, and the name of one `secret_store`.
Nothing about a project is edited in the panel. The admin owns only: secret stores and their
keys, model providers, and one git credential per host for reading config repositories.
`${secret:NAME}` references are allowed only in repository credentials, service env, `env`,
and connection config/env/headers; the set of names referenced is the project's required keys.
The schema is strict pydantic with `extra="forbid"` in `schemas/project_config.py`.

- `config_repo.py` mirrors the repo (in the pod's `EXRADIUS_CACHE_DIR`, an `emptyDir`),
  reads the file at the branch head and returns a `ProjectConfigSnapshot` (commit, config or
  errors, warnings, required_keys, store).
- `project_state.py` wraps it with the host credential, the store, and the runnable check;
  the API caches the latest snapshot on `projects.snapshot` for the panel only.
- The worker never trusts that cache: intake and `preparing` read the file afresh and
  record `runs.config_commit`. Tasks for a project with errors or missing keys are rejected
  with the reason. `capabilities.py` says what this version cannot run yet (`services`);
  that is the health reason too.
- `worker/builds.py` turns each Dockerfile into a `Build` object (create-if-absent, tag by
  tree hash, context tar in the object store, served by `/internal/builds/{id}/context`)
  and waits on it in `preparing` and in Verify's "Build workstation" step.
- `resolve.py` turns the config into run inputs; secrets are decrypted only there, when the
  Run and its per-run Secret are created. Everything shown to the panel, the agent's environment section, events and logs
  uses `$NAME` placeholders.
- `catalog.py` holds the MCP connection catalog (github, postgres, slack, ...); custom
  connections use the `mcpServers` shape.

**Backend package `backend/exradius/`.**

- `api/`: FastAPI. One process serves two ports: 8000 for `/api/*`, the admin API (session
  cookie, single admin user): `config_routes.py` (projects, stores, git credentials, catalog,
  verification), `provider_routes.py`, `task_routes.py` (tasks, events, transcripts, signed
  downloads, per-task SSE stream), `system_routes.py`; and 8001 for `internal.py`,
  `/internal/runs/{run_id}/*` (spec, transcript, info requests, result, outcome, `/reported`,
  the `/answers` long-poll, attachments), `/internal/verifications/{id}/*` and
  `/internal/probes/{id}/*`, each authenticated by its Run's token. `PortSplit` keeps each port
  to its routes.
- `worker/`: `main.py` consumes `exradius.tasks`, `exradius.answers` and `exradius.control`
  (cancel, verify, test_provider, run_reported) with concurrency = prefetch count, watches the
  Runs and sweeps every 15 s. `intake.py` accepts or rejects; `runs.py` (`RunManager`) reads
  the config, creates the Run and its per-run Secret (`run_objects.py` builds them:
  secret-free spec, secret values by key), `settle()`s runs from Run status and the runner's
  reports, cancels via `spec.cancel`, recovers by listing Runs, and finalizes by deleting the
  Run; `kube.py` is the Kubernetes client (httpx, in-cluster token); `verify.py` and
  `provider_test.py` create verify and probe Runs; `git_identity.py` computes the agent's git
  variables; `heartbeat.py` reports runtimes and the operator's Lease.
- `states.py` is the task state machine; `lifecycle.py` performs transitions. Postgres is
  the source of truth: every state change locks the run row, stores an `events` row in the
  same transaction, and the event is published after commit (`published_at` marks success,
  so unpublished events survive restarts). Finalize happens exactly once, guarded by
  `runs.ended_at`. A Run object holds only the live execution.
- `runner/`: the process inside the run pod (`exradius-runner`): `--prepare` (workspace and
  task attachments), `--clone`, `--files` (the answer attachments sidecar), `<run_id>` (the
  session: fetches the run spec, hosts the built-in `exradius` MCP server
  (`request_information`, `submit_result`, attachments) in `tools.py`, builds the prompts in
  `prompt.py`, streams the transcript, waits for answers in the pod and resumes there, runs the
  delivery check), `--verify` and `--probe`. `runner/environment.py` strips the runner's own
  interpreter variables and the run token so the agent sees only the project's environment.
  `gitconfig.py` holds the hooks dispatcher (built into the bundle) and writes the git config.
- `runtimes/`: the `Runtime` protocol (`base.py`: `RunSpec`, `ConnectionSpec`, `RunOutcome`)
  and `claude_code.py`, the only implementation. The runtime writes Claude Code settings
  into `CLAUDE_CONFIG_DIR` (for example `includeCoAuthoredBy: false`).
- `models/`: SQLAlchemy models plus Alembic migrations (`0006` is the move to Kubernetes).
- `storage.py`: object store for transcripts, artifacts and attachments: S3 (SeaweedFS
  locally); the filesystem and in-memory stores are for tests. `messaging/`: queue/exchange
  names and the publisher. `git.py`: git helpers shared by the api, worker and runner.
- `cli.py`: the `exradius` CLI (publish-task, answer, events, generate-key, migrate).

**Operator `operator/`.** Go, controller-runtime. `api/v1alpha1` holds the `Run` and
`Build` CRDs (`exradius.dev`); `internal/podspec` builds the PVC and the pod from a Run
(prepare, clone-<n>, setup-<n>, the files sidecar, the workstation with the bundle as an
image volume) and the build pod from a Build (`build.go`: context fetch, rootless BuildKit),
and writes the security context itself; `internal/status` maps pods onto Run and Build
status (steps, log tails, pull failures, cancel, pod lost, podGeneration, digest);
`internal/controller` reconciles both. The Run spec is the contract with
`worker/run_objects.py`, the Build spec with `worker/builds.py`: change both and the CRD in
one commit.

**Prompt layout (architecture §13, prompt.py).** System prompt = Claude Code's preset, then
the fixed Exradius operating rules, then a generated `# Your environment` section from the
snapshot (`environment.py`: image, repositories with branch and push, services with non-secret
settings, notes), then the agent's optional `role`. The user message is `# Task <task id>`,
context and attachments only. Tickets never mention Exradius; agents learn how they run from
the rules alone (folders are git clones, branch name starts with the task id, push to origin
or attach a patch).

**Run isolation (kubernetes.md §7, runtime-bundles.md).** Images: `exradius-backend` (api
and worker; no Agent SDK, no Docker SDK; uid 1000), `exradius-operator`, `exradius-frontend`,
`exradius-runtime-claude-code` (a relocatable Python with the Agent SDK and a pinned native
Claude Code binary, plus the git hooks dispatcher; no Node), `exradius-project-default`. Run
pods mount the bundle image as an image volume at `/opt/exradius/runtime`, so a project's
workstation can be any glibc image with bash and git (no Alpine). The operator writes the
pod's security: uid 1000, `/etc/passwd` from the `exradius-run-identity` ConfigMap (`runner`,
home `/home/runner`), read-only root, all capabilities dropped, no ServiceAccount token, no
service links. `/work`, the home, `/tmp` and the state directory are subPaths of one
workspace PVC per Run. Runs live in the `exradius-runs` namespace (Pod Security `baseline`),
the stack in `exradius` (`restricted`), builds in `exradius-builds` (`privileged`: rootless
BuildKit needs seccomp `Unconfined`; kubernetes.md §24). NetworkPolicy and the egress proxy come in phase 4.

**Frontend `frontend/src/`.** React 19 + TypeScript, TanStack Router (`router.tsx`) and
Query (`lib/queries.ts`), Tailwind 4, Radix primitives, `lib/api.ts` as the only API client.
Pages: Overview, Projects (detail tabs config / secrets / verify / tasks), Stores, Providers,
Tasks, Settings. The panel talks to `/api` on the same origin; nginx (in the chart) or the Vite
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
