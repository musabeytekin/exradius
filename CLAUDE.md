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
loop) and `kubernetes-plan.md` (the phases: 1 runs, 2 image builds, 3 services, memory,
caches and Verify parity with v0.1.0; 4 to 6 egress, isolation, clouds), `config-repo.md` (the current project
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
cd backend && uv run exradius rotate-key    # re-encrypt every *_encrypted column with the primary key (kubernetes.md §10.1)
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

Egress proxy (Go, standard library only; `egress/`):

```bash
make -C egress test          # go test -race: forward and reverse proxy, real git clone/push through it, pod watch
```

Builder credential helper (Go, standard library only; `builder/`, in `deploy/builder.Dockerfile`):

```bash
make -C builder test         # docker-credential-exradius-gcr and -acr against fake metadata, Entra ID and ACR
```

Clouds (Terraform; nothing here needs an account; `apply` costs money and is the user's):

```bash
cd deploy/terraform/aws && terraform init -backend=false && terraform validate
cd deploy/terraform/aws && terraform test     # plan and apply against mocked providers (also gcp, azure)
cd deploy/terraform/aws && tflint --init && tflint
deploy/terraform/bootstrap.sh aws             # after a real apply: kubeconfig, Traefik, Secrets
make -C deploy push REGISTRY=<registry> PLATFORM=linux/arm64   # every image to a cloud registry
```

`tests/test_cloud_storage.py` runs the GCS and Azure stores against fake-gcs-server and
Azurite in Docker when `EXRADIUS_TEST_GCS_ENDPOINT` and `EXRADIUS_TEST_AZURITE_ENDPOINT` are
set (the commands are in its docstring).

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
make -C deploy/kind dev-vault   # Vault in dev mode (local only) for stores of kind vault: auth role, policy, secret/exradius/demo
tilt up -- --vault              # the same install plus deploy/kind/values-vault.yaml (make ... install VAULT=1 without Tilt)
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

Bridges and the setup skill (each its own Python project; `bridges/jira` depends on `bridges/kit` by path):

```bash
cd bridges/kit && uv run pytest              # the kit (tests/test_broker.py needs a real RabbitMQ URL)
cd bridges/jira && uv run pytest             # the Jira bridge; bridges/github likewise
cd bridges/jira && docker build --build-context kit=../kit -t exradius-jira .
claude plugin validate . && claude plugin validate skill   # the marketplace (repo root) and the plugin
```

## Architecture

**Config-repo model (docs/config-repo.md).** A project is a git repository holding
`exradius.yaml` (or `.exradius/exradius.yaml`): slug, repositories, workstation image or
Dockerfile, services, env, notes, connections, agents, and the name of one `secret_store`.
Nothing about a project is edited in the panel. The admin owns only: secret stores (their
keys, the projects allowed to use each, and where each is kept), model providers, and one
git credential per host for reading config repositories.
`${secret:NAME}` references are allowed only in repository credentials, service env, `env`,
and connection config/env/headers; the set of names referenced is the project's required keys.
The schema is strict pydantic with `extra="forbid"` in `schemas/project_config.py`.

- `config_repo.py` mirrors the repo (in the pod's `EXRADIUS_CACHE_DIR`, an `emptyDir`),
  reads the file at the branch head and returns a `ProjectConfigSnapshot` (commit, config or
  errors, warnings, required_keys, store).
- `project_state.py` wraps it with the host credential, the store, and the runnable check;
  the API caches the latest snapshot on `projects.snapshot` for the panel only.
- Secret stores (config-repo.md §7): a store hands its keys and values only to the projects
  the admin allowed for it (`secret_store_projects`); `project_state.store_values(session,
  backends, project_id, config)` is the one way to resolve values and refuses any other.
  Every change to a store goes to the append-only `secret_store_changes` (never a value).
  `secret_backends/` reads a store through its backend (`keys()`, `values(names)`, only the
  names the config references): `internal` (encrypted rows in Postgres) or `vault` (one
  HashiCorp Vault per install, KV v2, Kubernetes auth with the pod's ServiceAccount, values
  never cached). Settings per kind are strict pydantic. `StoreUnreachable` is not a missing
  key: its reason is `secret store S is unreachable: ...`, after about ten seconds of retries
  in intake and `preparing`.
- The worker never trusts that cache: intake and `preparing` read the file afresh and
  record `runs.config_commit`. Tasks for a project with errors, a store that does not allow
  it, an unreachable store or missing keys are rejected with the reason. `capabilities.py` is the hook for keys a version accepts before it can
  run them (empty since phase 3); its reason would be the health reason too.
- `worker/builds.py` turns each Dockerfile into a `Build` object (create-if-absent, tag by
  tree hash, context tar in the object store, served by `/internal/builds/{id}/context`)
  and waits on it in `preparing` and in Verify's "Build workstation" step.
- Services are native sidecars of the run's pod on its `localhost` (`hostAliases`);
  `worker/registry.py` reads an image's `HEALTHCHECK` from its registry for a service
  without `ready`. Each project has a PVC `exradius-project-<slug>` for git mirrors and
  `workstation.cache` (`worker/caches.py` names them); the worker deletes it with the
  project.
- `resolve.py` turns the config into run inputs; secrets are read from the store (and
  decrypted, for an internal one) only there, when the Run and its per-run Secret are created (the project's own secret variables) or for the
  egress proxy's policy (`api/egress.py`: the provider, repository and HTTP connection
  credentials, which never reach a pod). Everything shown to the panel, the agent's
  environment section, events and logs uses `$NAME` placeholders.
- `network.py` is what the agent phase may reach (`network.allow`, the install's
  `egress.allow`, the connections' catalog `hosts`): the proxy's policy and the environment
  section's network line come from it.
- `catalog.py` holds the MCP connection catalog (github, gitlab, slack, ...); custom
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
  (cancel, verify, test_provider, run_reported, project_deleted) with concurrency = prefetch count, watches the
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
  locally), GCS or Azure Blob (httpx clients), signing in with the pod's cloud identity
  (`pod_identity.py`; boto3's chain for S3); the filesystem and in-memory stores are for
  tests. `messaging/`: queue/exchange
  names and the publisher. `git.py`: git helpers shared by the api, worker and runner.
- `cli.py`: the `exradius` CLI (publish-task, answer, events, generate-key, migrate).

**Operator `operator/`.** Go, controller-runtime. `api/v1alpha1` holds the `Run` and
`Build` CRDs (`exradius.dev`); `internal/podspec` builds the PVC and the pod from a Run
(prepare, clone-<n>, setup-<n>, the files sidecar, the workstation with the bundle as an
image volume, or copied by a first `bundle` container with `runs.bundleMount: copy`) and the
build pod from a Build (`build.go`: context fetch, rootless BuildKit from `exradius-builder`,
the cloud credential helper for the image's registry),
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
Claude Code binary, plus the git hooks dispatcher; no Node), `exradius-project-default`,
`exradius-builder` (pinned rootless BuildKit with the ECR, Artifact Registry and ACR credential
helpers). Run
pods mount the bundle image as an image volume at `/opt/exradius/runtime`, so a project's
workstation can be any glibc image with bash and git (no Alpine). The operator writes the
pod's security: uid 1000, `/etc/passwd` from the `exradius-run-identity` ConfigMap (`runner`,
home `/home/runner`), read-only root, all capabilities dropped, no ServiceAccount token, no
service links. `/work`, the home, `/tmp` and the state directory are subPaths of one
workspace PVC per Run. Runs live in the `exradius-runs` namespace (Pod Security `baseline`),
the stack in `exradius` (`restricted`), builds in `exradius-builds` (`baseline` with the
node-installed `charts/exradius/files/profiles/buildkit.json`, and `charts/exradius/files/profiles/exradius-buildkit` on
AppArmor nodes, via `builds.seccompProfile`/`appArmorProfile`; `privileged` without them:
rootless BuildKit creates a user namespace and mounts in it; kubernetes.md §12, §27). Values
`nodeSetup` installs both profiles on every node with a privileged DaemonSet.

**Isolation (kubernetes.md §7.8, §27; phase 5).** `runs.userNamespaces` (`hostUsers: false`,
on by default) and `runs.runtimeClassName` (`gvisor`, an opt-in that excludes user namespaces:
`make -C deploy/kind dev-gvisor` locally) are install values the operator writes into every run
pod. The run token is never a variable: `prepare`
and `files` read it from the Secret as a file, `prepare` hands a copy to the workstation in
an in-memory volume, and the runner deletes it once read and makes itself non-dumpable
(`runner/environment.py`), so the agent, the same uid, cannot read it. A Run ends when its
workstation does (`internal/status`), not when its sidecars have stopped.

**Egress (kubernetes.md §10, §11; phase 4).** Every request of a run or build pod goes through
`exradius-egress` (`egress/`, Go, a Deployment in `exradius`): a forward proxy on 3128
(`HTTP(S)_PROXY`, decided by host name on `CONNECT`) and an injecting reverse proxy on 3129
(`/anthropic`, `/git/<host><path>`, `/mcp/<connection>`, `/phase`). A pod's address is its
identity (a pod watch); setup reaches any public host, the agent phase only its model,
repositories, connections and the allow lists; private addresses need
`egress.allowPrivate`. Its policies come from the api's `/internal/egress/runs/{run}`
(TokenReview of its ServiceAccount token) and its records go to `egress_records`. No pod holds
the model key (`ANTHROPIC_BASE_URL` + `placeholder`), a repository credential (git `insteadOf`
routes) or an HTTP connection's secret header. The operator writes the proxy variables,
`YARN_HTTP(S)_PROXY` and `JAVA_TOOL_OPTIONS` (with Maven's
`aether.connector.http.useSystemProperties`); the runner waits until every proxy pod sees the
agent phase (`runner/egress.py`). NetworkPolicies (chart): run pods reach DNS, the proxy and the
api's 8001 only.

**Frontend `frontend/src/`.** React 19 + TypeScript, TanStack Router (`router.tsx`) and
Query (`lib/queries.ts`), Tailwind 4, Radix primitives, `lib/api.ts` as the only API client.
Pages: Overview, Projects (detail tabs config / secrets / verify / tasks), Stores, Providers,
Tasks, Settings. The panel talks to `/api` on the same origin; nginx (in the chart) or the Vite
proxy (dev) forwards it. Forms offer known values and only ask for free text when the
information exists nowhere else.

**Bridges and the skill.** `bridges/` holds the integration layer that Exradius itself
leaves outside its core: `kit/` (the bridge kit library: directory fetch, routing hand-off,
task publishing, events, bridge status, over the integration API and the broker),
`jira/` (Jira Cloud on the kit) and `github/` (GitHub issues; written before the kit and
agent routing, so it still uses agent labels). They speak only the public contracts
(`docs/integration-api.md`) and import nothing from `backend/`. Their design notes are in
each one's `docs/`. `skill/` is the setup skill (a Claude Code plugin); the repository
root's `.claude-plugin/marketplace.json` publishes it, so it installs with
`claude plugin marketplace add musabeytekin/exradius`. Its `scripts/exradius_check.py`
holds a standard-library port of the config schema, the catalog and the agent phase's
host list; `backend/tests/test_skill_check.py` fails when they drift, so a change to
`schemas/project_config.py`, `catalog.py` or `network.py` is followed by one there
(setup-and-verify.md §2.7).

## Conventions worth knowing

- Secrets and provider credentials are write-only through the API and encrypted at rest
  with `EXRADIUS_SECRET_KEY` (`crypto.py`; `EXRADIUS_SECRET_KEY_PREVIOUS` only decrypts). Never
  log or return their values. An encrypted column's name ends in `_encrypted`: `rotate-key`
  finds them by that suffix. A Vault store's values never touch Postgres, and a store's
  settings hold no credential.
- The worker does the harness's job only: it does not commit, push, open pull requests or
  otherwise act on behalf of the agent. Delivery problems become warnings in the result.
- Unknown keys in `exradius.yaml` are errors, not warnings; keep validators strict.
- A tool in a run that ignores `HTTPS_PROXY` cannot reach the network (NetworkPolicy): give it
  its own proxy setting at the operator level, as `YARN_HTTPS_PROXY` and Maven's resolver flag
  are, rather than asking each project to configure it.
- Tests build config repositories as local bare git repos (`tests/conftest.py`:
  `make_config_repo`, `commit_config`, `configured`); follow that pattern rather than mocking
  the git layer.
- Commit messages are short imperative subjects without a scope prefix (see `git log`).

**Clouds (kubernetes.md §16, §28; phase 6).** `deploy/terraform/{aws,gcp,azure}` make the
cluster (system pool, tainted run pool, Arm), registries (builds apart from the Exradius
images), bucket, managed Postgres (Amazon MQ on AWS, the RabbitMQ operator elsewhere), the
cloud identities of the api, the worker and build pods, a static ingress address and
`values-generated.yaml`; `bootstrap.sh` adds the kubeconfig, Traefik and the Secrets. Only
the values differ from kind: `values-eks.yaml`, `values-gke.yaml`, `values-aks.yaml`, then
the generated file. The chart's `ingress` routes every path to the panel's nginx;
`publicApiUrl` is its address. Values `identity`, `stack`, `objectStore.gcs|azure`,
`builds.credentialHelper`, `runs.bundleMount` and `nodeSetup` are what the clouds use. Each
module's optional `vault` variable points the install at a Vault the organisation already
runs (Exradius never installs one): it adds the `vault` block to `values-generated.yaml`,
`bootstrap.sh` makes the CA ConfigMap from its `ca_file`, and the output
`vault_kubernetes_auth` tells Vault's admin how to configure Kubernetes auth for the cluster
(kubernetes.md §28).

