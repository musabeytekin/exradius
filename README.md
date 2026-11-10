# Exradius

**A self-hosted harness that runs AI coding agents on a company's own codebases.**
A ticket arrives on a queue. Exradius brings up a fully provisioned, isolated workstation
for the project (its repositories, toolchain and services such as Postgres or RabbitMQ),
runs a Claude Code agent in it, and publishes what happened: status changes, questions for a
human, and a result with a branch or pull request.

Exradius only executes. It has no ticket model and no chat integration: whatever tracker a
company uses publishes tasks on RabbitMQ and consumes the events. It runs on Kubernetes,
locally on kind or in a cloud cluster, and is administered from a web panel.

[![A ticket in a tracker is assigned to the Exradius agent](docs/media/assign-ticket.gif)](docs/media/assign-ticket.mp4)

*Assigning a ticket to the agent: the tracker's integration turns it into an Exradius task
([video, 6 s](docs/media/assign-ticket.mp4)).*

## Seen working

Agents run by Exradius opened these pull requests on forks of four open-source codebases,
each project set up from its own config repository:

| Project | Stack | Pull request |
|---|---|---|
| [Plane](https://github.com/makeplane/plane) | TypeScript/React monorepo and a Django API; Postgres, Valkey, RabbitMQ, MinIO | [Register estimate routes in the public API](https://github.com/musabeytekin/plane/pull/1) |
| [Saleor](https://github.com/saleor/saleor) | Django GraphQL API and the React dashboard, two repositories; Postgres, Valkey | [Trim whitespace around email on registration and login](https://github.com/musabeytekin/saleor/pull/1) |
| [Outline](https://github.com/outline/outline) | TypeScript, Node and React, Yarn 4 | [Reject a past expiry when creating API keys](https://github.com/musabeytekin/outline/pull/1) |
| [Bank of Anthos](https://github.com/GoogleCloudPlatform/bank-of-anthos) | Java and Python microservices; Postgres | [Label transactions as deposit or payment](https://github.com/musabeytekin/bank-of-anthos/pull/1) |

These tickets were written for the trials. A graded evaluation on real upstream bugs is the
next step (see [Status](#status)).

## How a task runs

```mermaid
flowchart LR
    tracker["Tracker or<br/>integration layer"] -- task --> tasks[("exradius.tasks")]
    tasks --> worker["Worker"]
    worker -- reads at branch head --> config["Config repository<br/>exradius.yaml"]
    worker -- creates a Run --> operator["Operator"]
    operator -- creates the pod --> run["Run pod<br/>clones, setup, workstation + Claude Code"]
    run -- pushes branch, opens PR --> git["Git host"]
    worker -- status, questions, result --> events[("exradius.events")]
    events --> tracker
    tracker -- answers --> answers[("exradius.answers")]
    answers --> worker
```

1. **Intake.** The worker reads the project's `exradius.yaml` afresh, rejects the task with
   a reason if the config has errors or the secret store lacks a key it references, and
   stores the task before acknowledging the message.
2. **Preparing.** It creates a `Run` object, and the operator turns it into one pod on a
   fresh workspace volume: images the config builds are built first, the repositories are
   cloned from the project's mirrors, the services (Postgres, Redis, ...) start next to the
   agent on the pod's `localhost`, then the setup commands run (`pnpm install`,
   `uv sync`, ...) in the workstation image with the project's caches.
3. **Running.** The agent works in `/work`, where every folder is a git clone. Its system
   prompt holds Exradius's operating rules and a generated description of the environment:
   image, repositories with branch and push rights, services with their settings, the
   project's notes.
4. **Questions.** When the ticket is not enough, the agent calls `request_information`.
   The session stops, the question goes out as an event, and the pod waits; an answer on
   `exradius.answers` resumes the same session in the same pod.
5. **Ending.** The agent pushes a branch named after the task (or attaches a patch when it
   may not push) and submits its result. The runner warns about any work left unpushed; the
   worker uploads the transcript, publishes `task.result` and deletes the Run, which
   removes the pod and its workspace.

Several runs of one project can go at once, each in its own pod and workspace.

## Design decisions

- **The agent authors; the harness never acts for it.** The worker never commits, pushes,
  merges or opens pull requests. The agent pushes through a per-repository route that adds
  the credential, under its own git identity, and every commit carries `Exradius-Task` and `Exradius-Agent` trailers.
  Approving and deploying stay with people.
- **A project is a git repository.** `exradius.yaml` declares repositories, workstation,
  services, environment, MCP connections and agents. It is reviewed in pull requests like
  code. The panel only manages what cannot live in git: secret stores, model providers and
  one credential per git host. The schema is strict, so unknown keys are errors, not
  warnings, and each run records the config commit it used.
- **Enforce, don't configure.** Whatever Exradius needs inside a run is enforced at kernel,
  Kubernetes or git command-line level, never through a setting a project can override. For
  example, Plane's husky hooks set a repository-local `core.hooksPath` that silently dropped
  the task trailers. The hooks path now comes from git's command-line config, which outranks
  every config file, and a dispatcher runs the project's own hooks after Exradius's
  ([docs/run-environment.md](docs/run-environment.md)).
- **Hardened, isolated runs.** One pod per run: an unprivileged user, read-only root
  filesystem, all capabilities dropped, no Kubernetes credentials, and a declared memory as
  the pod's request and limit, so the scheduler places it where it fits. A run's services
  are sidecars of its own pod, reached by name on its `localhost`, so a project service
  called `postgres` can never resolve to Exradius's database or another run's.
- **No credentials in the run, and an allowlist for the agent.** Every request of a run goes
  through Exradius's egress proxy; NetworkPolicy refuses everything else. Setup reaches any
  host; the agent reaches its model, its repositories, its connections and the hosts the
  config's `network.allow` names, and anything else is a `403` that names the host. The model
  key, the repository tokens and secret connection headers stay in the proxy, which adds them
  on the way out and refuses a push to a repository the config does not let it push to. The
  task page lists every host reached and refused
  ([docs/kubernetes.md](docs/kubernetes.md) §11).
- **Any image can be the workstation.** The agent runtime (a relocatable Python with the
  Claude Agent SDK and a pinned Claude Code binary) is a bundle mounted read-only into the
  container. A project's image needs only bash and git on glibc; `node:22`, `python:3.14`
  and `maven` images work unchanged.
- **Postgres is the source of truth.** Every state change locks the run row and writes its
  event in the same transaction; events are published after commit and retried until
  marked as published. Finalisation happens exactly once, and a restarted worker
  reconciles the runs it finds in flight.
- **Secrets appear only at run start.** Stores are encrypted at rest and write-only
  through the API. Prompts, the panel, events and logs show `$NAME` placeholders.
- **No special-case permissions.** An agent may fully use every MCP server it is given;
  what it does with them is a matter of its role, not of flags.

## A project, as code

Adapted from the Plane config repository, shortened, with a cache and a memory limit added:

```yaml
version: 1

project:
  slug: plane
  name: Plane

secret_store: plane               # every ${secret:NAME} resolves from this store

repositories:
  - name: plane
    url: https://github.com/musabeytekin/plane.git
    branch: preview
    credential: "${secret:GITHUB_TOKEN}"
    push: true

workstation:
  dockerfile: docker/workstation.Dockerfile   # node 22, pnpm, uv, Python 3.12
  setup:
    - cd plane && pnpm install --frozen-lockfile
    - cd plane/apps/api && uv sync --locked
  cache: [~/.local/share/pnpm/store]          # kept across runs

resources:
  memory: 6g

services:
  postgres:
    image: postgres:15.7-alpine
    env: {POSTGRES_USER: plane, POSTGRES_PASSWORD: plane, POSTGRES_DB: plane}
    ports: [5432]
    ready: {command: [pg_isready, -h, 127.0.0.1, -U, plane]}
  redis:
    image: valkey/valkey:7.2.11-alpine
    ports: [6379]
    ready: {tcp: 6379}

env:
  DATABASE_URL: postgresql://plane:plane@postgres:5432/plane
  REDIS_URL: redis://redis:6379/
  pnpm_config_store_dir: /home/runner/.local/share/pnpm/store   # keep pnpm's store in the cache

notes: |
  API tests: from apps/api, `uv run pytest plane/tests/unit`. Web checks: `pnpm check:lint`,
  `pnpm check:types`, vitest per package. Pull requests target `preview`.

connections:
  github:
    catalog: github
    config: {token: "${secret:GITHUB_TOKEN}"}

agents:
  developer:
    connections: [github]
    role: |
      You implement what the ticket asks, run the checks for what you touched, push a
      branch and open a pull request against preview.
  scout:
    role: You investigate questions about the codebase and report evidence. You never change code.
```

The full format is in [docs/config-repo.md](docs/config-repo.md).

### Writing the config: the setup skill

Working out a project's toolchain and services is where onboarding takes the most time,
so it is automated by a Claude Code plugin,
[exradius-skill](https://github.com/musabeytekin/exradius-skill). Run in the project's
checkout, it reads the code, writes `exradius.yaml`, a workstation Dockerfile when a stock
image is not enough, and a README for the admin. It checks all of it locally with Docker,
using the same container flags as Exradius, then creates the config repository. Plane was
set up this way and passed Exradius's Verify on the first try.

```bash
claude plugin marketplace add musabeytekin/exradius-skill
claude plugin install exradius@exradius-skill
```

Then `/exradius:exradius-project` inside Claude Code. In the panel, Verify runs by itself
when a project is added and on every new config commit. It builds the image, starts the
services and runs setup; when that fails, it names the cause.

## Running it

Exradius runs on Kubernetes (since 0.2.0; [docs/kubernetes.md](docs/kubernetes.md)). The
same Helm chart installs it on a local kind cluster and, with another values file, on a
cloud cluster. It runs everything v0.1.0 ran: workstation and service images or
Dockerfiles, services, declared memory and per-project caches
([docs/kubernetes-plan.md](docs/kubernetes-plan.md) has the phases still to come). v0.1.0
is the last Docker Compose version.

### Locally, on kind

You need Docker, [kind](https://kind.sigs.k8s.io), kubectl, Helm and
[Tilt](https://tilt.dev) (`brew install kind kubectl helm tilt-dev/tap/tilt`), about 4 GB
of free memory for Docker, and a model provider's credential (an Anthropic API key or a
Claude Code OAuth token).

```bash
make -C deploy/kind dev-up     # local registry, kind cluster "exradius", operators, Secrets
tilt up                        # build every image, install the chart, live-update the backend
```

`dev-up` installs CloudNativePG, the RabbitMQ Cluster Operator (and cert-manager, which it
needs), creates the namespace `exradius` and generates the Secrets `exradius-secret-key`,
`exradius-admin` and `exradius-objectstore`. It prints how to read the admin password:

```bash
kubectl --context kind-exradius -n exradius get secret exradius-admin -o jsonpath='{.data.password}' | base64 -d; echo
```

Tilt forwards, on ports that leave room for a v0.1.0 install beside it:

- Panel: http://localhost:3100
- API: http://localhost:8100 (OpenAPI at `/docs`)
- RabbitMQ: `localhost:5673`, its UI at http://localhost:15673
- Tilt's own UI: http://localhost:10350

`tilt down` uninstalls the chart; `make -C deploy/kind dev-down` deletes the cluster and the
registry. Without Tilt, `make -C deploy/kind images install` builds and pushes the images
with their version tag and installs the chart with `charts/exradius/values-local.yaml`.

To watch runs: `kubectl --context kind-exradius -n exradius-runs get runs,pods -w`.

### On a cluster

Prerequisites, which the chart does not install: a storage class for the run workspaces,
nodes with containerd 2.1 or later (image volumes), a network plugin that enforces
NetworkPolicy, on Ubuntu 26.04 nodes `kernel.apparmor_restrict_unprivileged_unconfined=0`
(docs/kubernetes.md §16), and either CloudNativePG and the RabbitMQ operator or an external Postgres
and RabbitMQ. Run pods use user namespaces by default (`runs.userNamespaces`), which needs
containerd 2, runc 1.2 or later and a Linux 6.3+ kernel on the nodes. Optional hardening, each
with a node requirement: `runs.runtimeClassName: gvisor` (runsc and a RuntimeClass on the
nodes; turn user namespaces off with it), and build pods at Pod Security `baseline`
(`builds.seccompProfile` with `deploy/seccomp/buildkit.json` on every node, plus
`builds.appArmorProfile` with `deploy/apparmor/exradius-buildkit` on AppArmor nodes). A registry build pods push to on a private address needs an
`egress.allowPrivate` entry; a corporate proxy goes in `egress.upstreamProxy`. Create the Secrets the values name
(`secretKey.existingSecret`, `admin.existingSecret`, the database, broker and object store
credentials), then:

```bash
kubectl create namespace exradius
kubectl label namespace exradius pod-security.kubernetes.io/enforce=restricted
helm install exradius charts/exradius -n exradius -f my-values.yaml
```

`charts/exradius/values.yaml` documents every key. Values files for EKS, GKE and AKS come
with phase 6.

### In the panel

1. Add a model provider (Providers).
2. Add a credential for the host of your config repository (Settings).
3. Create the secret store your config names (Stores).
4. Add the project by its config repository URL (Projects). The Secrets tab lists the keys
   the config references and which are still missing; Verify runs once they are filled.

Tasks can then be sent from the panel's Tasks page or from the command line.

## Sending a task

A task names a project and an agent by slug:

```json
{
  "version": 1,
  "task_id": "PLANE-214",
  "project": "plane",
  "agent": "developer",
  "instructions": "Estimates created through the public API return 404. Fix it.",
  "context": {"reporter": "support"},
  "correlation": {"ticket": "PLANE-214"}
}
```

Events go to the `exradius.events` topic exchange: `task.status` on every state change
(`queued`, `preparing`, `running`, `waiting_for_input`, `resuming`, then `completed`,
`failed` or `cancelled`), `task.info_request` when the agent asks something, and
`task.result` with the summary, the structured output, signed links to artifacts and the
transcript. `correlation` comes back unchanged on every event. The task ID must be new:
reusing one, even of a failed task, gets the task rejected.

The backend ships a CLI for trying this from a shell:

```bash
cd backend
export EXRADIUS_BROKER_URL=amqp://<user>:<password>@localhost:5673/   # the exradius-rabbitmq-default-user Secret
uv run exradius events --task-id PLANE-214 --until-result      # in one terminal
uv run exradius publish-task --task-id PLANE-214 --project plane --agent developer \
  --instructions "Estimates created through the public API return 404. Fix it."
uv run exradius answer --task-id PLANE-214 --request-id <id> --answer "Yes, v1 only."
```

The message contracts are in [docs/architecture.md](docs/architecture.md) §4.

## Development

```bash
cd backend && uv sync && uv run pytest      # 500+ tests: SQLite, a fake Kubernetes API, local bare git repositories
make -C operator test                       # the operator: unit tests and envtest (a real API server)
cd frontend && npm install && npm run dev   # http://localhost:5173; EXRADIUS_API_URL=http://localhost:8100 for the kind install
cd frontend && npm run lint && npm test && npm run build
```

Under `tilt up`, a change to a Python file is synced into the api and restarts it in place;
the worker and the runtime bundle are rebuilt, because run pods mount the bundle.
`make -C deploy/kind e2e PROJECT=<slug>` publishes one task to the kind install and waits
for its result. `tests/test_integration.py` also exercises a real RabbitMQ
(`EXRADIUS_TEST_BROKER_URL`).

**Stack.** Backend: Python 3.14, FastAPI, SQLAlchemy and Alembic on Postgres, aio-pika on
RabbitMQ, httpx for the Kubernetes API, the Claude Agent SDK and MCP. Operator: Go,
controller-runtime. Install: Helm, kind and Tilt locally. Panel: React 19, TypeScript,
Vite, Tailwind CSS 4, Radix, TanStack Router and Query.

## Repository layout

```
backend/exradius/
  api/          admin API (port 8000) and the internal API for run pods (port 8001)
  worker/       intake, Run objects and settling, Verify, provider probes
  runner/       the process in the run pod: prepare, clone, files sidecar, the agent's
                session, the built-in exradius MCP server, the delivery check
  runtimes/     the runtime interface and its Claude Code implementation
  models/       SQLAlchemy models and Alembic migrations
  schemas/      broker messages, API bodies and the exradius.yaml schema
  messaging/    RabbitMQ topology and publishing
backend/tests/
operator/       Go operator: the Run and Build CRDs, the pod builders, status mapping, the controllers
charts/exradius/  the Helm chart
frontend/src/   the admin panel
deploy/         Dockerfiles (backend, operator, Claude Code runtime bundle, default project
                image) and kind/ (the local cluster)
Tiltfile        the local developer loop
docs/           design notes
```

## Design notes

| Document | Covers |
|---|---|
| [architecture.md](docs/architecture.md) | the system, message contracts, run lifecycle, state machine, implementation notes, operating rules |
| [kubernetes.md](docs/kubernetes.md) | Exradius on Kubernetes: the Run resource, pods, the operator, the chart, the local loop |
| [kubernetes-plan.md](docs/kubernetes-plan.md) | the phases of the move to Kubernetes |
| [config-repo.md](docs/config-repo.md) | projects as config repositories and the `exradius.yaml` format |
| [run-environment.md](docs/run-environment.md) | what is enforced inside a run, and how: hooks, credentials, memory, caches |
| [runtime-bundles.md](docs/runtime-bundles.md) | the relocatable agent runtime, mounted into run pods |
| [environment-and-services.md](docs/environment-and-services.md) | services (sidecars of the run's pod) and the environment section of the prompt |
| [agent-identity.md](docs/agent-identity.md) | agents' git identities, trailers and push credentials |
| [setup-and-verify.md](docs/setup-and-verify.md) | Verify with diagnosis, and the setup skill |
| [providers.md](docs/providers.md) | model providers and credential tests |
| [onboarding.md](docs/onboarding.md) | the admin panel |

## Status

Exradius is working software that has been exercised on the four codebases above. Its
scope today: one company per installation, a single admin user, and Claude Code as the
only runtime.

Not done yet:

- **A graded evaluation.** Every trial so far used tickets written for it. Next is a
  repeatable evaluation on real upstream bugs: the agent gets the original issue text,
  and the upstream fix's tests, which the agent never sees, are run on its branch.
- **Egress control.** Outbound network is open during runs; an allowlist for the agent
  phase is planned.
- **Corporate networks.** There is no support yet for a corporate proxy, CA bundle or
  package mirrors at installation level.
- **Usage and cost.** Neither is recorded per run.
