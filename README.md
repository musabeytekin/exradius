<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img src="docs/assets/logo-light.svg" alt="Exradius logo" width="112">
  </picture>
</p>

<h1 align="center">Exradius</h1>

<p align="center">
  <strong>Run AI coding agents on your own codebases, in isolated workstations on your own Kubernetes.</strong>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#configuring-a-project">Configuring a project</a> ·
  <a href="#sending-tasks">Sending tasks</a> ·
  <a href="#deploying">Deploying</a> ·
  <a href="#documentation">Documentation</a>
</p>

<p align="center">
  <img alt="Status: alpha" src="https://img.shields.io/badge/status-alpha-orange">
  <a href="LICENSE"><img alt="License: Apache 2.0" src="https://img.shields.io/badge/license-Apache%202.0-blue"></a>
  <img alt="Runs on Kubernetes" src="https://img.shields.io/badge/runs%20on-Kubernetes-326CE5?logo=kubernetes&logoColor=white">
  <img alt="Python 3.14" src="https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white">
  <img alt="Go" src="https://img.shields.io/badge/go-operator%20%26%20proxy-00ADD8?logo=go&logoColor=white">
</p>

---

Exradius is a self-hosted harness for AI coding agents. Hand it a task from Jira, GitHub,
your own tooling or the command line. It brings up a fully provisioned workstation for the
project in one hardened Kubernetes pod, with the repositories, the toolchain, and services
such as Postgres or Redis. It runs a Claude Code agent in that pod and reports back:
status changes, questions for a human, and a result with a branch or a pull request.

Exradius only executes. Tracking work, discussing it and reviewing the result stay in the
tools your team already uses; small bridges connect those tools to Exradius over a message
queue.

[![A ticket in a tracker is assigned to the Exradius agent](docs/media/assign-ticket.gif)](docs/media/assign-ticket.mp4)

*Assigning a ticket to the agent starts a task.*

## Features

- **Projects as code.** A project is a git repository with an `exradius.yaml`. It lists:
  - the repositories;
  - the workstation image and its setup commands;
  - the services;
  - the environment;
  - the MCP connections;
  - the agents.

  It is reviewed in pull requests like any other code, validated strictly, and every run
  records the commit it used.
- **Real workstations.** Every run gets its own pod:
  - the project's services run as sidecars on the pod's `localhost`;
  - the setup commands run before the agent starts;
  - caches persist per project.

  Any glibc image with bash and git can be the workstation. Dockerfiles are built in the
  cluster with rootless BuildKit.
- **No credentials inside the sandbox.** Every request a run makes leaves through an egress
  proxy, which adds the model key, repository tokens and connection headers on the way out.
  In the agent phase, only an allowlist is reachable. Everything else is refused, and the
  task page shows each host the run reached and each host it was refused.
- **Hardened by default.** Run pods have:
  - a non-root user;
  - a read-only root filesystem;
  - no capabilities;
  - user namespaces;
  - NetworkPolicy;
  - no Kubernetes credentials.

  gVisor is available as an opt-in.
- **Questions mid-run.** When a ticket is not enough, the agent asks. The pod waits, and the
  same session resumes when the answer arrives.
- **Automatic agent routing.** Leave the agent out of a task and Exradius picks one of the
  project's agents from their roles and tools.
- **The agent authors; the harness never acts for it.** Exradius never commits, pushes or
  opens pull requests on its own. Agents push under their own git identity, and every
  commit carries `Exradius-Task` and `Exradius-Agent` trailers. Approving and merging stay
  with people.
- **One chart, any cluster.** The same Helm chart installs on kind on a laptop, on a
  self-hosted cluster, or on EKS, GKE and AKS, for which Terraform modules are included.
  Only the values file changes.

## How it works

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/flow-dark.svg">
    <img src="docs/assets/flow-light.svg" alt="What happens to a ticket assigned to the agent: Exradius checks which project it belongs to, whether the project is ready and which agent should take it, answering with the reason when it cannot go on; the agent gets a workstation of its own, asks on the ticket when information is missing, and delivers a branch and pull request or a patch; the result comes back on the ticket." width="100%">
  </picture>
</p>

1. **Intake.** The worker reads the project's config afresh and checks that every secret it
   references exists. If anything is missing, it rejects the task with the reason.
2. **Preparing.** The worker creates a `Run` object, and the operator turns it into a pod:
   - images the config builds are built first;
   - the repositories are cloned from the project's mirrors;
   - the services start;
   - the setup commands run (`pnpm install`, `uv sync`, ...).
3. **Running.** The agent works in `/work`, where every folder is a git clone. Its prompt
   describes the environment: the image, the repositories with their branch and push
   rights, the services, the project's notes.
4. **Ending.** The agent pushes a branch named after the task, or attaches a patch when it
   may not push, and submits its result. Exradius publishes the result with the
   transcript, then deletes the pod and its workspace.

Several runs of one project can go at once, each in its own pod.

## Quick start

Three steps: start Exradius on your machine, describe your project with the setup skill,
then add it in the panel and send a task.

### 1. Start Exradius

Exradius runs locally on [kind](https://kind.sigs.k8s.io) with [Tilt](https://tilt.dev).
You need:
- Docker, with at least 4 GB of memory; 8 GB or more for larger projects;
- kind, kubectl, Helm and Tilt:

  ```bash
  brew install kind kubectl helm tilt-dev/tap/tilt
  ```
- a model credential: an Anthropic API key, or a Claude Code OAuth token.

```bash
git clone https://github.com/musabeytekin/exradius.git && cd exradius
make -C deploy/kind dev-up     # local registry, kind cluster, operators, Secrets
tilt up                        # build the images, install the chart, live-reload the backend
```

The first `tilt up` builds every image, which takes a while; Tilt's own page
(http://localhost:10350) shows when everything is up. Read the generated admin password:

```bash
kubectl --context kind-exradius -n exradius get secret exradius-admin -o jsonpath='{.data.password}' | base64 -d; echo
```

Open the panel at http://localhost:3100 and sign in as `admin`.

### 2. Describe your project

A project is a git repository with an `exradius.yaml`. The setup skill, a
[Claude Code](https://claude.com/claude-code) plugin, writes it for you. It needs Docker,
git and Python 3.8 or later on your machine, and the `gh` CLI if it should create the
repository. Install it once:

```bash
claude plugin marketplace add musabeytekin/exradius
claude plugin install exradius@exradius
```

Then open Claude Code in a checkout of the code agents should work on, and run:

```
/exradius:exradius-project
```

The skill:
- reads the code: languages and tool versions, services, the checks as CI runs them, how
  the application is started;
- asks the few things it cannot read, in one round, such as whether agents may push;
- writes `exradius.yaml`, a workstation Dockerfile when a stock image is not enough, and a
  README that lists every secret the project needs and where to get it;
- proves it locally with Docker under a run's conditions: the setup commands, your own
  test and lint commands, and the start of the application;
- creates the config repository `exradius-<slug>`, private, once you have seen the files
  and agreed.

It ends with what the next step needs: the repository's URL and the keys of its secret
store. To write the file by hand instead, see [Configuring a project](#configuring-a-project).

### 3. Add the project and send a task

In the panel:

1. **Providers**: add a model provider with your model credential.
2. **Settings**, "Config repository access": add a credential for the git host, when the
   config repository is private. For GitHub, a fine-grained token that can read it.
3. **Projects**, **Add project**: paste the config repository's URL.
4. The project's **Secrets** tab, when the config names a secret store: **Create store**
   creates it and allows the project to use it. Fill the keys the config repository's
   README lists.

Once the store has every key the config references, **Verify** runs by itself: it builds
the workstation, starts the services and runs setup, and if anything fails, it names the
cause. When it has passed, send a task with **Send a task** on the **Tasks** page, or from
a shell (this needs [uv](https://docs.astral.sh/uv/)):

```bash
make -C deploy/kind e2e PROJECT=<slug>     # sends one task and waits for its result
```

Tilt also forwards the API to http://localhost:8100 (OpenAPI at `/docs`) and RabbitMQ to
`localhost:5673` (UI at http://localhost:15673). Stop with Ctrl-C. `tilt down` uninstalls
the chart, and `make -C deploy/kind dev-down` deletes the cluster.

## Configuring a project

A project lives in its own git repository. A shortened example, for
[Plane](https://github.com/makeplane/plane):

```yaml
version: 1

project:
  slug: plane
  name: Plane

secret_store: plane               # every ${secret:NAME} resolves from this store

repositories:
  - name: plane
    url: https://github.com/acme/plane.git
    branch: preview
    credential: "${secret:GITHUB_TOKEN}"
    push: true

workstation:
  dockerfile: docker/workstation.Dockerfile
  setup:
    - cd plane && pnpm install --frozen-lockfile
    - cd plane/apps/api && uv sync --locked
  cache: [~/.local/share/pnpm/store]

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

env:
  DATABASE_URL: postgresql://plane:plane@postgres:5432/plane
  REDIS_URL: redis://redis:6379/

network:
  allow: [registry.npmjs.org, pypi.org, files.pythonhosted.org]

connections:
  github:
    catalog: github
    config: {token: "${secret:GITHUB_TOKEN}"}

agents:
  developer:
    connections: [github]
    role: You implement what the ticket asks, run the checks, and open a pull request.
  scout:
    role: You investigate questions about the codebase and report evidence. You never change code.
```

The full format is in [docs/config-repo.md](docs/config-repo.md).

### The setup skill

The [setup skill](skill/) of the quick start writes this file from your code and keeps it
current. Run `/exradius:exradius-project` again in the same checkout when the code has
changed (a new service, a tool version, a repository): it compares the config with the
code, proposes the edits and checks them. Give it a failed Verify step or task with its
log, and it finds whether the cause is the config, the store or the installation,
reproduces it locally and fixes what is the config's.

Its local check also runs without Claude:

```bash
skill/skills/exradius-project/scripts/check.sh --repo myapp=../myapp \
  --run 'cd myapp && npm test' ../exradius-myapp
```

## Sending tasks

A task is a JSON message on the `exradius.tasks` queue. It names a project and, optionally,
an agent:

```json
{
  "version": 1,
  "task_id": "PLANE-214",
  "project": "plane",
  "agent": "developer",
  "instructions": "Estimates created through the public API return 404. Fix it.",
  "correlation": {"ticket": "PLANE-214"}
}
```

Events come back on the `exradius.events` topic exchange, each carrying `correlation`
unchanged:

| Event | When |
|---|---|
| `task.status` | on every state change: `queued`, `preparing`, `running`, `waiting_for_input`, `resuming`, then `completed`, `failed` or `cancelled` |
| `task.info_request` | when the agent asks a question |
| `task.result` | at the end, with the summary, the structured output and signed links to the artifacts and the transcript |

Answers go to `exradius.answers`. The contracts are in
[docs/architecture.md](docs/architecture.md) §4.

From a shell, with the backend's CLI:

```bash
cd backend
secret() { kubectl --context kind-exradius -n exradius get secret exradius-rabbitmq-default-user -o jsonpath="{.data.$1}" | base64 -d; }
export EXRADIUS_BROKER_URL="amqp://$(secret username):$(secret password)@localhost:5673/"
uv run exradius publish-task --task-id PLANE-214 --project plane \
  --instructions "Estimates created through the public API return 404. Fix it."
uv run exradius events --task-id PLANE-214 --until-result
```

### Bridges

Bridges connect trackers to Exradius:

| Bridge | What it does |
|---|---|
| [`bridges/jira`](bridges/jira) | Assigning an issue to the bot account starts a task. Questions and results arrive as comments; mention the bot to answer. |
| [`bridges/github`](bridges/github) | A GitHub App that starts tasks from issue labels and answers in comments. |
| [`bridges/kit`](bridges/kit) | The library both are meant to build on, for writing a bridge for another tracker. |

Bridges use only the public contracts in
[docs/integration-api.md](docs/integration-api.md). They run as separate containers.

## Deploying

### On a Kubernetes cluster

The chart expects:
- nodes with containerd 2.1 or later, for image volumes; otherwise set
  `runs.bundleMount: copy`;
- for user namespaces, which are on by default: containerd 2, runc 1.2 or later and Linux
  6.3 or later;
- a network plugin that enforces NetworkPolicy, and a default storage class;
- PostgreSQL and RabbitMQ, from CloudNativePG and the RabbitMQ operator or as external
  services;
- on Ubuntu 26.04 nodes, `kernel.apparmor_restrict_unprivileged_unconfined=0`.

```bash
kubectl create namespace exradius
kubectl label namespace exradius pod-security.kubernetes.io/enforce=restricted
helm install exradius charts/exradius -n exradius -f my-values.yaml
```

[`charts/exradius/values.yaml`](charts/exradius/values.yaml) documents every key: the
ingress, a corporate upstream proxy and CA bundle, gVisor, and the build profiles.

### In a cloud

[`deploy/terraform`](deploy/terraform) has modules for AWS, GCP and Azure. Each creates:
- a cluster with system and run node pools;
- registries, a bucket and managed PostgreSQL;
- cloud identities, so that no cloud key is stored in a Secret;
- a static address for the panel;
- a generated values file.

```bash
cd deploy/terraform/gcp
terraform init && terraform apply -var project=<id> -var 'allowed_cidrs=["<your address>/32"]'
cd ../../.. && deploy/terraform/bootstrap.sh gcp     # kubeconfig, ingress controller, Secrets
$(terraform -chdir=deploy/terraform/gcp output -raw push_command)   # push the images
helm upgrade --install exradius charts/exradius -n exradius \
  -f charts/exradius/values-gke.yaml -f deploy/terraform/gcp/values-generated.yaml
```

A small installation costs roughly $9–16 a day, depending on the cloud. Run
`terraform destroy` when you are done.

## Documentation

The design notes in [`docs/`](docs) describe how Exradius works and why:

| Document | Covers |
|---|---|
| [architecture.md](docs/architecture.md) | the system, message contracts, run lifecycle and state machine |
| [kubernetes.md](docs/kubernetes.md) | the Run resource, pods, the operator, the egress proxy, isolation, the chart |
| [config-repo.md](docs/config-repo.md) | projects as config repositories and the `exradius.yaml` format |
| [environment-and-services.md](docs/environment-and-services.md) | services and the environment section of the agent's prompt |
| [run-environment.md](docs/run-environment.md) | what is enforced inside a run: hooks, credentials, memory, caches |
| [agent-routing.md](docs/agent-routing.md) | how Exradius picks an agent when a task names none |
| [integration-api.md](docs/integration-api.md) | the API bridges use: directory, config hooks, bridge status |
| [agent-identity.md](docs/agent-identity.md) | agents' git identities, trailers and push rights |
| [runtime-bundles.md](docs/runtime-bundles.md) | the relocatable agent runtime mounted into run pods |
| [setup-and-verify.md](docs/setup-and-verify.md) | Verify, its diagnoses, and the setup skill |
| [providers.md](docs/providers.md) | model providers and credential tests |

## Development

```bash
cd backend && uv sync && uv run pytest      # Python: api, worker, runner
make -C operator test                       # Go: the operator, with envtest
make -C egress test                         # Go: the egress proxy
cd frontend && npm install && npm run dev   # the panel at http://localhost:5173
cd bridges/jira && uv run pytest            # likewise bridges/kit and bridges/github
```

Under `tilt up`, a change to a Python file is synced into the api and restarts it in place.
`make -C deploy/kind e2e PROJECT=<slug>` sends one task to the local install and waits for
its result.

```
backend/      the api, the worker and the runner (Python, FastAPI, SQLAlchemy, aio-pika)
operator/     the Kubernetes operator for Run and Build objects (Go, controller-runtime)
egress/       the egress proxy (Go, standard library)
builder/      credential helpers for in-cluster image builds
frontend/     the admin panel (React, TypeScript, Vite, Tailwind)
charts/       the Helm chart
deploy/       Dockerfiles, the kind setup, Terraform for the clouds
bridges/      the bridge kit and the Jira and GitHub bridges
skill/        the setup skill (a Claude Code plugin)
docs/         design notes
```

## Status

Exradius is alpha software. It has been run end to end, on kind and on a self-hosted k3s
cluster, against [Plane](https://github.com/makeplane/plane),
[Saleor](https://github.com/saleor/saleor), [Outline](https://github.com/outline/outline)
and [Bank of Anthos](https://github.com/GoogleCloudPlatform/bank-of-anthos).

Known limits:

- **Clouds.** The Terraform modules are tested offline, but none has yet been applied to a
  real account.
- **DNS.** Run pods can still resolve names through the cluster's DNS, which can leak data.
  A DNS policy is designed but not built.
- **Scope.** Each installation serves one organization with a single admin user. Claude
  Code is the only agent runtime.
- **Bridges.** The GitHub bridge predates the bridge kit and agent routing.
- **Measurement.** Usage and cost are not yet recorded per run, and there is no graded
  evaluation on real upstream bugs yet.

## License

Apache License 2.0; see [LICENSE](LICENSE). The license covers this repository's code. The
Claude Code binary that the runtime bundle image downloads at build time is Anthropic's,
under Anthropic's own terms.
