# Exradius on Kubernetes

Status: agreed design, 2026-11-08; details settled by phase 1 folded in the same day (§23),
and by phase 2 on 2026-11-09 (§24). Supersedes the Docker-based execution of `architecture.md`
(§3, §5, §9, §10, the run-container parts of §12), `runtime-bundles.md` §2.2, the services
lifecycle and networks of `environment-and-services.md` §2, and the memory budget and cache
mounts of `run-environment.md` §4 and §5. The phases are in `kubernetes-plan.md`. The last
Docker-based version is tag `v0.1.0` (924344b).

## 1. Why

The worker holds the host's Docker socket and the Fernet master key in one process, next to
untrusted agent code. It needs an absolute host data directory and ties Exradius to one Docker
host. Docker-in-Docker was rejected.

The principle: **an application must not manage infrastructure.** Exradius declares what a task
needs, and Kubernetes creates and places the containers, the way a Deployment's pods are
created by its ReplicaSet controller and not by the user. The same Helm chart runs on a local
kind cluster and on EKS, GKE or AKS; only the values file changes.

The run-environment principle (architecture §14) carries over with one word changed: what
Exradius needs to stay true inside a run is enforced at the level of the kernel,
**Kubernetes** or the command line, never through a setting a project, a tool or the agent can
reset.

## 2. Kubernetes terms used here

| Term | Meaning here |
|---|---|
| Pod | One or more containers scheduled together on one node, sharing a network namespace (one IP, one `localhost`) and volumes. A run is one pod. |
| Init container | A container that runs to completion before the pod's main containers start, in order. |
| Native sidecar | An init container with `restartPolicy: Always`: it starts in order and keeps running next to the main containers (GA since 1.33). Kubernetes waits for its startup probe before starting the next init container. |
| Startup probe | A check (a command, or a TCP connect) the kubelet repeats until it succeeds; until then the container counts as not started. |
| Deployment | Keeps N identical pods running, replacing failed ones. |
| CRD, custom resource | A CustomResourceDefinition adds an object type to the Kubernetes API (`Run`, `Build`). Objects are stored in etcd and watched like built-in ones. |
| Operator, controller | A process that watches objects and makes the cluster match them (here: a `Run` becomes a pod). |
| ownerReference | A link from an object to its owner; deleting the owner garbage-collects what it owns. |
| PVC | PersistentVolumeClaim: a request for a disk. Access mode `ReadWriteOnce` (RWO) means pods on one node; `ReadWriteMany` (RWX) means pods on any node and needs a network filesystem. |
| Secret, ConfigMap | Key-value objects mounted into pods as files or env; Secrets for sensitive values. |
| ServiceAccount, RBAC | A pod's identity in the Kubernetes API, and the roles that say what it may do there. |
| NetworkPolicy | Firewall rules between pods, enforced by the network plugin. |
| RuntimeClass | Selects the container runtime for a pod, for example gVisor or Kata. |
| User namespace | `hostUsers: false`: uid 0 inside the pod is an unprivileged uid on the node (GA in 1.36). |
| Image volume | Mounts the content of an OCI image read-only into a pod (GA in 1.36; needs containerd 2.1+ or CRI-O). |
| Pod Security Admission | Namespace labels (`privileged`, `baseline`, `restricted`) that refuse pods asking for more than the level allows. |

## 3. Components and their privileges

```
 integration layer ──▶ RabbitMQ ◀──────────────────────────────┐
                          │ tasks, answers, control            │ events
                          ▼                                    │
 Frontend ──▶ api (Python) ◀── internal port ──┐        worker (Python) ──▶ Postgres
              │    ▲                           │          │  master key
              │    │ /internal/egress          │          │  writes Run, Build, per-run Secret
              ▼    │                           │          ▼
          Postgres │                           │   ┌─────────────── Kubernetes API ───────────────┐
                   │                           │   │  Run, Build objects        (exradius-runs,   │
                   │                           │   │  pods, PVCs, Secrets        exradius-builds) │
                   │                           │   └──────────────────▲──────────────────────────┘
          egress proxy (Go) ◀── all run egress ┤                      │ watches Runs, creates pods
           allowlist, credentials, audit       │               operator (Go)
                   │                           │
                   ▼                           │
                internet                 run pod: clone, services (sidecars), setup, workstation + runner
```

| Component | Kind | Holds | May do in Kubernetes | May not |
|---|---|---|---|---|
| `api` | Deployment, `exradius-backend` image | master key, Postgres | create TokenReviews (to authenticate the egress proxy) | touch Runs, pods or Secrets |
| `worker` | Deployment, same image | master key, Postgres, broker | in `exradius-runs`: create, patch, delete and watch `Run`; create, patch and delete Secrets (no get or list); list and delete PVCs. In `exradius-builds`: create, get, watch and delete `Build`; create, patch and delete Secrets (the Build's token). Read the operator's Lease | create pods; read Secrets |
| `operator` | Deployment, Go (controller-runtime) | nothing secret | in `exradius-runs`: create, watch and delete pods and PVCs; in `exradius-builds`: create, watch and delete pods. In both: read `pods/log`; update `runs/status` or `builds/status`; write Events. Its leader-election Lease | read Secrets; reach Postgres or the broker |
| `egress` | Deployment, Go | decrypted credentials of active runs, in memory only | list and watch pods in `exradius-runs` | anything else |
| run pod | Pod per Run | the per-run Secret (§10) | nothing: `automountServiceAccountToken: false`, and NetworkPolicy blocks the API server | reach anything but the proxy, the api's internal port and DNS |
| build pod | Pod per Build | its Build's token (for the context); registry push identity from phase 6 | nothing: no ServiceAccount token | |
| `frontend` | Deployment, nginx | nothing | nothing | |

- **Why three Python and Go parts.** The worker keeps today's job: intake, the state machine,
  events, finalize. It loses Docker and gains a Kubernetes client. A compromised worker can
  only ask for Runs, and a Run cannot express privileges, because the operator writes the
  security context itself (§7.8). A compromised operator can run pods in `exradius-runs`, and
  so read the per-run Secrets there, but never the master key or the stores, which live in
  `exradius` and in Postgres.
- **Why Go for the operator.** controller-runtime (kubebuilder) is the standard for
  controllers. It gives envtest (a real API server and etcd in tests), informers, leader
  election and status conditions. The api, the worker and the runner stay Python.
- The api serves two ports from one process: 8000 (admin, integration, hooks, downloads) and
  8001 (`/internal/*` only). Run pods and build pods reach 8001 and nothing else.
- The worker defaults to one replica. A second one is safe: transitions take the run-row lock
  as today, so a duplicate Run event is a no-op.

### 3.1 Namespaces

| Namespace | Holds | Pod Security Admission |
|---|---|---|
| `exradius` | api, worker, frontend, operator, egress; locally also Postgres, RabbitMQ and SeaweedFS | `restricted` |
| `exradius-runs` | `Run` objects, run pods, per-run Secrets, workspace PVCs, project volumes | `baseline` enforced, `restricted` warned (services run as their image's user, often root) |
| `exradius-builds` | `Build` objects, build pods, their token Secrets | `privileged`: rootless BuildKit needs seccomp `Unconfined`, which `baseline` refuses (checked in phase 2, §24); phase 5 may try a `Localhost` profile |

The names come from values. One install per cluster is the supported shape.

## 4. Truth and writers

Postgres stays the source of truth. A `Run` holds only the live execution and is deleted at
finalize.

| What | Lives in | Written by |
|---|---|---|
| tasks, the state machine, events and outbox, info requests, results, transcripts | Postgres | worker (transitions, events); api internal routes (what the runner reports); api admin routes |
| `Run` spec | Kubernetes | worker: create; patch for cancel or a pod restart; delete at finalize |
| `Run` status | Kubernetes | operator only |
| per-run Secret | Kubernetes | worker, with an ownerReference to its Run |
| run pod, workspace PVC | Kubernetes | operator, owned by the Run |
| project volume PVC | Kubernetes | operator creates it on the project's first Run; worker deletes it with the project |
| `Build` spec and status | Kubernetes | worker (spec), operator (status) |
| `image_builds` rows | Postgres | worker, from Build status |
| egress records | Postgres | api, from the proxy |

- The runner never changes state, as today. It reports through the internal API. The worker
  makes transitions when the Run's status changes or when the api tells it that the runner
  reported (§8).
- Every transition still locks the run row, updates run and task, and stores its event in the
  same transaction; publishing after commit is unchanged.
- **Crash recovery gets simpler.** Run objects survive a worker restart, so any worker can
  settle any run; it no longer has to tell from an advisory lock whether the previous worker is
  alive. At start the worker lists the Runs and compares them with Postgres:
  - an unfinished run with a Run: keep watching;
  - an unfinished run without one: failed, "the run's Kubernetes object disappeared";
  - a Run without an unfinished run: deleted.
- A Run deleted by hand (`kubectl delete run`) fails its task the same way.

## 5. The `Run` resource

Group `exradius.dev`, version `v1alpha1`. The group is a domain name. `exradius.dev` is a
placeholder until a domain the project controls is chosen, and changing it later means a new
CRD. The spec is a normalised, **secret-free** description, written by the worker from the
config of the run's commit. The operator never parses `exradius.yaml`; the schema stays in
Python.

```yaml
apiVersion: exradius.dev/v1alpha1
kind: Run
metadata:
  name: run-3f2c0d9e-...            # run-<run uuid>
  namespace: exradius-runs
  labels: {exradius.dev/project: saleor, exradius.dev/purpose: task}
  annotations: {exradius.dev/task-id: SAL-101}   # task ids do not fit label syntax
spec:
  purpose: task                     # task | verify | probe
  project: saleor
  secretName: run-3f2c0d9e-...      # the per-run Secret (§10)
  runtime:
    name: claude_code
    image: kind-registry:5000/exradius-runtime-claude-code:0.2.0
  workstation:
    image: kind-registry:5000/exradius/saleor/workstation:1a2b3c4d5e6f
    setup: ["uv sync"]
    memory: 6Gi                     # resources.memory, absent when undeclared
    env:                            # file order; secret values by key only
      - {name: DJANGO_SETTINGS_MODULE, value: saleor.settings}
      - {name: DATABASE_URL, secretKey: env.DATABASE_URL}
    caches: [/home/runner/.cache/uv]
  repositories:
    - {folder: saleor, url: "https://github.com/acme/saleor.git", branch: main, push: true}
  services:
    - name: db
      image: postgres:16
      command: null
      env: [{name: POSTGRES_PASSWORD, secretKey: service.db.POSTGRES_PASSWORD}]
      ports: [5432]
      ready: {exec: [pg_isready, -U, app]}        # or {tcp: 5432}, or null
      memory: 512Mi
  git:                               # computed by git_identity.py, non-secret
    env: [{name: GIT_AUTHOR_NAME, value: Developer}, ...]
  network:                           # phase 4: what the proxy allows in the agent phase
    allow: [registry.npmjs.org, pypi.org]
  cancel: false
  podGeneration: 0                   # bumped by the worker to recreate a lost pod (§8.3)
status:
  phase: Preparing                   # Pending | Preparing | Running | Succeeded | Failed
  podName: run-3f2c0d9e-...-0
  nodeName: kind-control-plane
  conditions:
    - {type: Scheduled, status: "False", reason: Unschedulable, message: "0/1 nodes: insufficient memory"}
  steps:                             # one per init container, in order
    - {name: "Clone saleor (main)", state: passed, startedAt: ..., finishedAt: ..., log: "..."}
    - {name: "Start service db", state: running}
  workstation: {state: terminated, exitCode: 137, reason: OOMKilled}
  observedGeneration: 3
```

- **Lifecycle.** The worker creates the per-run Secret and the Run together, and the Secret
  gets an ownerReference to the Run, so the Run is created first. The operator creates the
  workspace PVC and the pod, both owned by the Run, and mirrors the pod's state into status. At
  finalize the worker deletes the Run, and garbage collection removes the pod, the PVC and the
  Secret. A PVC goes only after its pod is gone (the `pvc-protection` finalizer).
- **No finalizer on Run.** Cleanup is ownerReferences only, so a stuck operator never blocks a
  deletion.
- **Printer columns:** `kubectl get runs -n exradius-runs` shows project, task, purpose, phase
  and age.
- **Status mapping**, done by the operator:
  - pod not scheduled: `Pending`, with the scheduler's reason;
  - init containers running: `Preparing`, with steps;
  - workstation started: `Running`;
  - pod `Succeeded` or `Failed`: the same phase, with the failing container, its reason
    (`OOMKilled`, `Error`, `ImagePullBackOff`, `DeadlineExceeded`, `NodeLost`, `Evicted`) and a
    log tail of up to 4000 characters.
- **Failures the operator decides itself:**
  - An image that cannot be pulled after three attempts fails the Run with the registry's
    message.
  - A service not started within 120 s of its container start, or restarted before it was
    ready, fails the Run. Kubernetes would otherwise restart a failing sidecar forever.
  - A Run that cannot be scheduled waits without a deadline (no run limits).
- **Log tails.** The operator reads `pods/log` of each step container when it ends, and of a
  failed service, and stores the last 4000 characters on the step. Termination messages are not
  used, because the kubelet divides 12 KiB among all of a pod's containers.

## 6. Placement and resources

- `resources.memory` becomes request = limit on the workstation container and on the setup
  init containers. A service's `memory` becomes request = limit on its sidecar. A container
  over its limit is killed by its own cgroup, and `OOMKilled` becomes today's diagnosis:
  "exceeded its declared memory (6g)".
- Undeclared memory gets the values default request (`runs.defaults.memoryRequest`, for
  example 1 Gi) and no limit, as today.
- CPU: a request from values and no CPU limit, because limits throttle builds and tests.
- `memory.py`, `EXRADIUS_RUN_MEMORY_BUDGET` and the "Reserve memory" step go. The scheduler
  places pods by their requests. A Run that does not fit waits in `Pending`, and the panel's
  task row says "waiting for cluster capacity" (`runs.waiting_for = 'capacity'`) where it said
  "waiting for memory". The task state stays `preparing`, and no status event is published.
- `worker.concurrency` (today `EXRADIUS_WORKER_CONCURRENCY`) still caps the Runs in flight per
  worker, so RabbitMQ keeps the backlog. Kueue (quota-based admission) can come later on top.
- Values `runs.nodeSelector` and `runs.tolerations` put runs on their own node pool. That is
  recommended in clouds, so runs never share a node with the stack.

## 7. From `exradius.yaml` to a pod

The worker turns the config into a Run spec (§5); the operator turns a Run spec into this pod:

| Order | Container | Image | Does | Mounts |
|---|---|---|---|---|
| init 1 | `prepare` | workstation + bundle | creates the workspace directories on first start, writes the task's attachments (fetched from the internal API) read-only, writes the marker `state/steps/prepare.done` | workspace PVC (whole) |
| init 2..n | `clone-<n>` (n: the repository's index) | workstation + bundle | refreshes the mirror on the project volume, `git clone --local`, checks out the branch, resets `origin`; writes `state/steps/clone-<n>.done` | workspace `work`, project volume `mirrors` |
| sidecar | `service-<name>` | the service's image | the service, with its startup probe | none |
| init | `setup-<n>` | workstation | `/bin/sh -c <command>`, as today | workspace, caches, identity files |
| sidecar | `files` | workstation + bundle | writes answer attachments as they arrive (§8.2) | workspace `attachments` |
| main | `workstation` | workstation + bundle | `/opt/exradius/runtime/bin/exradius-runner <run_id>` | workspace, caches, identity files, `attachments` read-only |

Each init container (except `files`) and each service is one step in status, so Verify and
task runs report progress the same way. `restartPolicy: Never` at pod level; sidecars
restart by their own policy.

- Container names are DNS labels, so a clone is `clone-<n>` and a setup command `setup-<n>`;
  the step's name carries the folder or the command ("Clone saleor (main)", "Setup: uv
  sync").
- A step container whose marker exists in `state/steps/` exits at once: a recreated pod
  (§8.3) skips `prepare` and the clones, and runs setup again.
- `files` runs from the workstation image with the bundle mounted, like `prepare`: the bundle
  image is `FROM scratch` and has no libc of its own.

### 7.1 The workspace

One PVC per Run (`runs.workspace.size`, default 50 Gi; `runs.workspace.storageClass`;
`volumeBindingMode: WaitForFirstConsumer`, so a cloud disk is created in the pod's zone). It
replaces `runs/<run_id>/`, with the same reasons as today for keeping home and `/tmp` on disk:

| subPath | Mounted at | Was |
|---|---|---|
| `work` | `/work` | `project/` |
| `work/.exradius/attachments` | same path, read-only in the workstation | the read-only bind mount |
| `home` | `/home/runner` | `home/` |
| `tmp` | `/tmp` (mode 1777) | `tmp/` |
| `state` | `/exradius/state` | `state/` (session files, git config, transcript offset) |

The pod has `fsGroup: <run gid>` with `fsGroupChangePolicy: OnRootMismatch`, so the volume is
writable by the run user without a recursive chown on every start. Removing a run directory
with read-only files, deep trees and sockets is no longer Exradius code: the volume is deleted.

### 7.2 The runtime bundle

The bundle image (`exradius-runtime-claude-code:<version>`) is mounted as an **image volume**
at `/opt/exradius/runtime`, read-only, in `prepare`, `clone-*`, `files` and `workstation`. This
replaces extraction into Docker volumes (`bundles.py`). A rebuilt bundle is a new image digest,
so a running pod never sees it change.

- Image volumes need containerd 2.1 or later on the nodes. Kind has it.
- For a cloud node image without it, values `runs.bundleMount: copy` turns the mount into an
  init container that copies the bundle into an `emptyDir`. Slower, and the same everywhere
  else.
- The git hooks dispatcher (architecture §12, "Git at the command-line level") moves from the
  state mount into the bundle, at `/opt/exradius/runtime/git-hooks`. It is read-only there, so
  the agent can no longer edit it. `core.hooksPath` points there through `GIT_CONFIG_*`.
- The runner writes `<state>/git/exradius.gitconfig` at every start, which the worker used to
  do.

### 7.3 The run user

- uid and gid come from `runs.user` (default `1000:1000`).
- The generated `/etc/passwd` and `/etc/group` (runtime-bundles §2.4) come from one ConfigMap
  in `exradius-runs`, created by the chart. Each is mounted with `subPath`, read-only, over the
  image's file in `prepare`, `clone-*`, `setup-*` and `workstation`. Services keep their
  image's users.
- `HOME=/home/runner`, as today.

### 7.4 Services as native sidecars

Each service is an init container with `restartPolicy: Always`, in file order, so Kubernetes
starts them one by one and holds `setup-*` and the workstation until each is started, as the
worker does today.

- **Readiness to startup probe:**

  | `ready` | Startup probe |
  |---|---|
  | `{command: [...]}` | `exec` with that command |
  | `{tcp: p}` | `tcpSocket` on `p` |
  | `null` | the image's `HEALTHCHECK` command when it has one, else none (started when its process starts) |

  - Kubernetes ignores Docker's `HEALTHCHECK`. The worker reads the image config from the
    registry and maps it, so `null` keeps today's meaning. When the config cannot be read, the
    service counts as started.
  - Probes run every second, with a deadline of 120 s from container start enforced by the
    operator (§5).
- **Host names.** A pod has one network namespace: services listen on `localhost`. The pod gets
  `hostAliases` mapping every service name to `127.0.0.1`, so `postgres:5432` still resolves.
  This is kubelet-managed `/etc/hosts`, which a project cannot change.
- **Ports are shared** between all services and the workstation, as on a developer's machine
  where compose publishes them on `localhost`.
  - The config check gains: ports unique across services (today they are unique only within
    one service), and `localhost` a reserved service name.
  - An undeclared port two services both listen on fails in Verify.
- `command`, env (non-secret inline, secret by Secret key), the image's own entrypoint and user:
  as today.
- **Security context**, the same capability set as today:
  - capabilities: drop `ALL`, add `CHOWN SETUID SETGID FOWNER DAC_OVERRIDE NET_BIND_SERVICE`;
  - `allowPrivilegeEscalation: false`;
  - seccomp `RuntimeDefault`;
  - writable root filesystem;
  - no mounts. What an image declares as `VOLUME` goes to the container's writable layer.
- **While a run waits**, services keep running, because the pod keeps running (§8.2).
- `environment.py`'s services paragraph stays true: services are "reachable by host name from
  this container".

### 7.5 Git: mirrors, clone, push

- **Mirrors** live on the project volume (§7.6) at `mirrors/<repo>.git`. They are refreshed
  under a file lock by the first `clone-*` container that needs them, as `sync_mirror` does
  today. The code moves from `worker/prepare.py` into the runner package, which runs in the pod
  with the workstation's `git`.
- The clone is `git clone --local` from the mirror. The mirror and the clone are on different
  volumes, so objects are copied rather than hardlinked; that is still local.
- Without a project volume, each clone fetches straight from the remote.
- **Credentials:**
  - Phases 1–3: the repository's credential comes from the per-run Secret as `http.extraHeader`
    for the fetch, as for mirrors today. Push uses the credential helper at the git
    command-line level, as today.
  - From phase 4: no credential is in the pod. A command-line `url.<proxy>.insteadOf` sends the
    repository's URL to the egress proxy, which adds the credential (§11).

### 7.6 The project volume: mirrors and caches

One PVC per project (`exradius-project-<slug>`), created by the operator on the project's first
Run and not owned by any Run. The worker deletes it when the project is deleted.

| Values | Behaviour |
|---|---|
| `runs.projectVolume.accessMode: ReadWriteOnce` | single-node clusters (local kind). |
| `ReadWriteMany` with `storageClass` | EFS, Filestore, Azure Files or NFS. Every run of the project mounts it on any node. A network filesystem can be slow for stores of many small files (pnpm), which Verify timings show. |
| `enabled: false` | no mirrors (direct clones) and no caches (each `workstation.cache` path is an empty directory on the workspace). |

- Each `workstation.cache` entry is the subPath `caches/<n>-<sanitised path>`, mounted at its
  path in `setup-*` and `workstation`. The naming is the one in `caches.py`.
- The panel's cache size (`ProjectOut.cache_bytes`) goes: no Exradius process mounts the volume
  any more.
- Trust is unchanged: a cache and a mirror are per project, the boundary the repository already
  has.

### 7.7 Setup commands

One init container per `workstation.setup` entry, from the workstation image, with the project
environment and the declared memory, `/bin/sh -c <command>`, working directory `/work`. A
non-zero exit fails the pod, and the step's log tail becomes the run's error, as today. Setup
does not run again when the session resumes in the same pod. It does run when a lost pod is
recreated (§8.3), because the services then start empty.

### 7.8 The workstation container and the pod's security

The operator writes these and the Run spec cannot change them:

- `runAsUser`/`runAsGroup` from values, `runAsNonRoot: true`;
- `readOnlyRootFilesystem: true`;
- `allowPrivilegeEscalation: false`, capabilities drop `ALL`, seccomp `RuntimeDefault`;
- `automountServiceAccountToken: false`;
- `enableServiceLinks: false`, so Kubernetes injects no `*_SERVICE_HOST` variables for the
  namespace's Services. The kubelet still adds the API server's own `KUBERNETES_SERVICE_*` and
  `KUBERNETES_PORT*` to every container; the runner drops them from the agent's environment,
  so it stays the project's;
- `supplementalGroupsPolicy: Strict`, so the run user gets only the run gid and `fsGroup`,
  not the groups the image's `/etc/group` gives its own user of that uid (Ubuntu's uid 1000
  is in `sudo` and `adm`);
- `terminationGracePeriodSeconds: 30` (cancel, §8.4);
- `runtimeClassName` from values (`runs.runtimeClassName`), empty for the cluster default;
- `hostUsers: false` when `runs.userNamespaces: true` (phase 5). With it, a service's root is an
  unprivileged uid on the node.

The workstation's env is: the project's `env`, the exported service secrets
(`resolve.exported_secrets`, as today), `GIT_*` from the Run spec, `EXRADIUS_RUN_TOKEN`,
`EXRADIUS_API_URL` (the internal port), the provider variables until phase 4, and the proxy
variables from phase 4. The runner's `isolate()` is unchanged.

## 8. The run lifecycle on Kubernetes

The state machine (`states.py`) and its events are unchanged. What drives each transition:

| Transition | Driven by |
|---|---|
| intake → `queued` | as today (worker) |
| `queued` → `preparing` | worker, when it starts the run: reads the config, computes images, creates Builds when needed (§12), then the Run and its Secret |
| `preparing` → `running` | Run status `Running` (the workstation container started) |
| `running` → `waiting_for_input` | the runner reports waiting; the api stores it and sends `{"action": "run_reported"}` on `exradius.control`; the worker transitions and publishes the pending `task.info_request` events |
| `waiting_for_input` → `resuming` | an answer arrives (worker), as today |
| `resuming` → `running` | the runner reports that it resumed (same path as waiting) |
| → `completed` / `failed` | Run status `Succeeded` or `Failed`, plus the outcome the runner reported, as `_settle` does with container exits today |
| → `cancelled` | admin cancel (§8.4) |

The control message is a nudge. A worker sweep every 15 s also picks up runs whose reported
status is ahead of their state, so a lost message only delays.

### 8.1 Preparing

1. Read the config afresh and record `config_commit`, `config` and `agent_snapshot`, as today.
2. Resolve images. For each Dockerfile, the tag by tree hash, and a Build if no succeeded Build
   exists for it (§12). Waiting for builds happens here.
3. Download URL attachments into the object store; base64 ones are already there from intake.
4. Create the Run, then the per-run Secret owned by it (§10). The run token is generated now
   and lives as long as the Run: it stays valid after a cancel until finalize, so a cancelled
   runner can still send its delivery check.
5. The operator creates the PVC and the pod. Step progress shows in Run status. A failed step
   fails the run with that step's log tail ("service db (postgres:16) …", "setup command '…'
   failed …", the clone error).

### 8.2 Waiting and resuming: the pod keeps running

- `request_information` records the question and interrupts the session, as today. The
  runner then reports `waiting` and **stays alive**. It long-polls
  `GET /internal/runs/{id}/answers?wait=30` until the run is `resuming`, then resumes the SDK
  session in-process with the delivered answers (the same `runtime.resume` call, the same
  resume message).
- Services, the workspace and the runner's container all stay, so nothing is lost while
  waiting.
- The pod's requests stay reserved while it waits; that is the accepted cost. There is still
  no expiry of waiting runs, and the panel lists them.
- **Answer attachments** are written by the `files` sidecar. It runs as a uid other than the
  run user's, from the bundle, and polls the internal API. It writes
  `.exradius/attachments/<request_id>/` with mode 0444, and the runner resumes the session once
  they are there. The workstation mounts that directory read-only, so the agent cannot change
  them, as today.
- The transcript sequence continues in-process. The offset file in `state` covers a recreated
  pod.

### 8.3 A lost pod

- **While running** (node failure, eviction): the run fails with "the run's pod was lost:
  <reason>", as a worker restart fails it today.
- **While waiting:** nothing happens until the answer arrives. The worker then sees in Run
  status that the pod is gone and bumps `spec.podGeneration`. The operator creates a new pod on
  the same workspace PVC:
  - `prepare` and `clone-*` see `prepared` and skip;
  - services start empty, and `setup-*` runs again;
  - the runner starts in resume mode (`/spec` says `resume`), as a resume container does today.

  This is the only case where a resume restarts the services.

### 8.4 Cancel

The api publishes cancel on `exradius.control` as today. The worker sets `spec.cancel: true`,
and the operator deletes the pod with its 30-second grace period.

- On `SIGTERM` the runner interrupts the session, runs the delivery check (§8.5) and reports
  `cancelled`.
- Once the pod is gone, the worker finalizes as `cancelled`.
- A run still in `preparing` loses its pod at once.
- A waiting run's runner reports from its poll loop.

### 8.5 Ending

- **The delivery check moves into the runner.** After the session ends (completed, failed or
  cancelled), the runner runs today's `INSPECT_CLONES_SCRIPT` as a separate process, as the
  run user, with an empty environment and `GIT_CONFIG_NOSYSTEM`. It sends the warnings with
  its outcome.
  - The reason it never ran in the worker (a clone's `.git/config` can run commands) does not
    apply: in the pod it has no more rights than the agent.
  - A runner killed by its memory limit sends no warnings, as a failed check gives none today.
- **Finalize** is as today, exactly once, guarded by `runs.ended_at`:
  1. store the transcript;
  2. publish `task.result`;
  3. revoke the token;
  4. **delete the Run**, which replaces deleting the run directory, removing services and
     removing the network.

## 9. How the runner talks back

The runner reaches `http://exradius-api-internal.exradius:8001`. That is the api's second port,
the only one NetworkPolicy lets a run pod reach. It uses the per-run token, as today.

- Unchanged: `GET /spec`, `POST /session`, `/transcript`, `/info-requests`, `/result`,
  `/outcome` (gains `warnings`).
- New:
  - `POST /internal/runs/{id}/reported` (`waiting` or `resumed`; triggers the control nudge);
  - `GET /internal/runs/{id}/answers?wait=` (long-poll);
  - `GET /internal/runs/{id}/attachments/{attachment_id}` (for `prepare` and `files`).
- The token now lives per Run rather than per container start, and is revoked at finalize.
  `runs.token_hash` stays.
- Verify and probe Runs get their own token the same way (§13).
- Other internal routes on the same port:
  - `/internal/egress/...` for the proxy (§11), authenticated by the proxy's ServiceAccount
    token through a TokenReview with audience `exradius-api`;
  - `/internal/builds/{id}/context` for build pods (§12), authenticated by a per-Build token.

## 10. Secrets

The master key and the stores never leave `exradius` and Postgres. What a run needs is
decrypted by the worker at Run creation, or by the api when a component asks for it.

| Secret | Phases 1–3 | From phase 4 |
|---|---|---|
| model credential (provider) | per-run Secret → env (`ANTHROPIC_API_KEY` / `CLAUDE_CODE_OAUTH_TOKEN`), as today | **never in the pod.** `ANTHROPIC_BASE_URL` points at the proxy and the variable holds a placeholder; the proxy swaps in the provider's credential |
| repository credential (clone, push) | per-run Secret → `http.extraHeader` for clones, the command-line credential helper for push | **never in the pod.** `insteadOf` to the proxy, which injects it and refuses `git-receive-pack` for a repository without `push: true` |
| HTTP connection headers with `${secret:…}` | in `/spec`, as today | the runner points the connection at the proxy and drops those headers; the proxy adds them |
| stdio connection env (`GITHUB_TOKEN` for a local server, a postgres DSN) | in `/spec`, as today | the same. The server process needs it, so the agent can read it; a known gap, as today |
| `env` and service `env` values with secrets | per-run Secret, referenced by key | the same. Services need them at container start |
| run token | per-run Secret | the same |
| config repository host credential | api and worker only, as today | the same; build pods get their context from the api, never from git (§12) |

- The per-run Secret has an ownerReference to its Run and lives in etcd only for the run's
  lifetime. The install guidance is to enable encryption at rest for Secrets (KMS) where the
  cloud offers it.
- The operator has no Secret rights. A pod references Secret keys, and the kubelet resolves
  them.
- `${secret:NAME}` rules, `$NAME` placeholders in the environment section and the drop of
  provider-named variables in `env` are unchanged.

## 11. The egress proxy and NetworkPolicy

Designed here from `ideas.md` ("Exradius's own egress proxy"), which it replaces. It is a Go
binary, `exradius-egress`, a Deployment in `exradius`, with two listeners:

- **Forward proxy, :3128.** `HTTP_PROXY`/`HTTPS_PROXY` (and lower case) point at it.
  `NO_PROXY` covers `localhost`, `127.0.0.1`, the service names and the api's internal host.
  For `CONNECT` it decides on the host name and port, so nothing is decrypted. The operator
  also sets `JAVA_TOOL_OPTIONS` with `-Dhttp(s).proxyHost/Port` and `-Dhttp.nonProxyHosts`.
  Tools that read neither must be configured in the workstation image, for example Maven's
  `<proxies>` in `settings.xml`. Verify shows that, and the skill learns it.
- **Injecting reverse proxy, :3129.** Routes it forwards upstream over HTTPS with the real
  credential:
  - `/anthropic/` → the provider's endpoint;
  - `/git/<host>/<path>` → the repository;
  - `/mcp/<connection>/` → an HTTP connection's URL.

**Run identity is the source address.** The proxy watches the pods in `exradius-runs`. A pod IP
maps to its Run, so the pod's credentials and allowlist apply. The pod's init container states
give its phase: setup until the last `setup-*` has ended, agent after that.

- There is no proxy authentication, because Java does not send Basic credentials when
  tunnelling HTTPS.
- Run containers have no `NET_RAW`, so they cannot spoof another source address.
- A run's credentials and allowlist come from `GET /internal/egress/runs/{run_id}` on the api.
  They are cached in memory until the pod goes, and never written.

**Policy:**

| Phase | Allowed |
|---|---|
| setup: `prepare`, `clone-*`, services, `setup-*` | any host. Chained to `egress.upstreamProxy` when values name a corporate proxy |
| agent: the workstation, and the services from then on | the model route; the repositories' routes; the hosts of declared HTTP connections; catalog connections' hosts (`catalog.py` gains `hosts`, for example `api.githubcopilot.com`, `api.github.com`); the install-wide `egress.allow`; the config's new `network.allow` |

- `network.allow: [hosts]` is a new optional key in `exradius.yaml`: host names, with a leading
  `*.` for subdomains. It is validated strictly, like every key. It is where a project names
  its package registries (`registry.npmjs.org`, `pypi.org`, `files.pythonhosted.org`) and the
  hosts its custom stdio connections reach. The skill writes it for the package managers it
  finds.
- **The environment section** replaces "Outbound network access is available." with
  "Outbound network access is limited to: <hosts>." The list is generated from the same set.
- **Audit.** Every request (run, phase, host, port, route, method and path for reverse routes,
  allowed or refused, bytes, duration) is batched to the api and stored in a new
  `egress_records` table. The task page lists hosts reached and refused.
  - The proxy logs the refusal reason; the agent sees a `403` with a one-line reason, so it can
    say in its result what was blocked.
- **Known gap:** DNS queries go to the cluster DNS, so a run can still leak data through query
  names. Recorded, not addressed.

**NetworkPolicy** (phase 4, in the chart):

| Policy | Rule |
|---|---|
| run pods (`exradius.dev/run` label) | deny all ingress; egress only to kube-dns (53/UDP and TCP), the egress pods (3128, 3129) and the api pods on 8001 |
| build pods | egress to the api on 8001, to the registry, and through the proxy or directly as `builds.egress` says; no ingress |
| api | ingress on 8001 only from run, build and egress pods; 8000 from the frontend and the ingress controller |
| egress | ingress from run pods; egress anywhere (or to the upstream proxy) |

A run therefore cannot reach the Kubernetes API, the stack's Postgres, RabbitMQ, other runs or
the internet directly. Phase 4's checks prove each of these on kind, whose network plugin
(kindnet with kube-network-policies) enforces NetworkPolicy. Docker Desktop's kubeadm cluster
does not; it was checked on 2026-11-08.

## 12. Image builds and the registry

Builds are `Build` objects (`exradius.dev/v1alpha1`) run by single-use rootless BuildKit pods
in `exradius-builds`.

- **Tags are unchanged:** `exradius/<project>/<name>:<first 12 hex digits of the tree hash>`, or
  of `<tree hash>:<Dockerfile blob>` when the Dockerfile lies outside the context, under the
  registry prefix from values (`builds.registry`). The worker computes them from its
  config-repository mirror, as `builds.py` does.
- **The object name** is `build-<project>-<name>-<hash>`. The worker creates it if absent, and
  concurrent runs wait on the same object: creating it is atomic, which replaces the
  per-project file lock. A succeeded Build means the image exists. Builds are kept, as
  `image_builds` rows are; deleting one forces a rebuild. A failed Build is replaced by the
  next run or Verify that needs the image (deleted with a `uid` precondition, so a Build
  another caller just made is never deleted), because a failure may be transient; runs
  waiting on it when it failed all fail with its log. Registry garbage collection is out of
  scope, as cleaning old tags is today.
- **The context comes from the api, not from git.** The worker stores the `git archive` tar it
  builds today in the object store (`builds/<image build id>/context.tar`). The build pod's
  init container `context` downloads it from `/internal/builds/{id}/context` with the
  Build's token. Build pods therefore need no config-repository credential and no git.
- **Order of creation.** The worker creates the Build, then (only if it created it) stores
  the context, the `image_builds` row with the token's hash, and last the Secret with the
  token, owned by the Build. The pod cannot start before its Secret exists
  (`CreateContainerConfigError`, which the operator tolerates for 120 s as for runs), so a
  worker that dies in between leaves a Build that fails and is replaced.
- **The Build resource:**

  ```yaml
  apiVersion: exradius.dev/v1alpha1
  kind: Build
  metadata:
    name: build-saleor-workstation-1a2b3c4d5e6f
    namespace: exradius-builds
    labels: {exradius.dev/project: saleor, exradius.dev/image: workstation, exradius.dev/purpose: build}
  spec:
    id: <image build uuid>              # the internal API path of the context
    project: saleor
    name: workstation                    # or the service's name
    image: kind-registry:5000/exradius/saleor/workstation:1a2b3c4d5e6f
    cache: kind-registry:5000/exradius/saleor/workstation:buildcache   # absent: no cache
    dockerfile: docker/Dockerfile        # inside the context (.exradius.Dockerfile when outside)
    secretName: build-saleor-workstation-1a2b3c4d5e6f                  # key token
  status:
    phase: Succeeded                     # Pending | Running | Succeeded | Failed
    reason: ""                           # BuildFailed, ContextFailed, OOMKilled, ImagePullFailed, PodLost, ...
    message: ""                          # one line: "the build exited with 1"
    podName: build-saleor-workstation-1a2b3c4d5e6f
    startedAt: ...
    finishedAt: ...
    digest: sha256:...                   # the pushed manifest
    log: "..."                           # last 4000 characters, refreshed every 5 s while it runs
  ```

  Printer columns: project, name, phase, age (and the image with `-o wide`).
- **The build pod** (`operator/internal/podspec/build.go`), named like its Build and owned
  by it:
  - init `context`: the builder image, `wget` with the token in its environment, `tar -x`
    into an `emptyDir` at `/workspace/context`; restricted-shaped security context;
  - `build`: `buildctl-daemonless.sh build --frontend dockerfile.v0`, `--opt
    filename=<dockerfile>`, `--output type=image,name=<image>,push=true`, and with a cache
    `--export-cache type=registry,ref=<cache>,mode=max` and `--import-cache`.
    `BUILDKITD_FLAGS=--oci-worker-no-process-sandbox` (one build per pod, so `RUN` steps
    need no sandbox from the builder) and `--config` for the `exradius-buildkitd`
    ConfigMap, which lists the registries reached over HTTP (`builds.insecureRegistries`).
    Read-only root with `emptyDir`s for BuildKit's state, `/run/user/1000` and `/tmp`;
    `TMPDIR=/tmp`, `DOCKER_CONFIG=/tmp/docker`;
  - the digest: buildctl's `--metadata-file` is copied into the termination message (a few
    hundred bytes, which the 12 KiB budget allows) and the operator reads
    `containerimage.digest` from it;
  - no ServiceAccount token, no service links, `restartPolicy: Never`, `runAsUser` 1000
    (the image's user), resources from `builds.resources`, the runs' `nodeSelector` and
    `tolerations`.
- **Status and the pod's end.** The operator maps the pod onto status like a Run's (§5):
  scheduling, image pulls (three attempts), config errors, then the outcome of `context`
  and `build`. Once a final status (with the log) is stored, it deletes the pod: a
  succeeded Build costs nothing but the object. A pod deleted by hand while building fails
  the Build (`PodLost`). Status-only updates do not reconcile a Build again (its log
  changes), so the controller requeues itself every 5 s while a build runs.
- **Waiting.** The worker polls the Build every 2 s. A run waits in `preparing` with
  `runs.waiting_for = 'build'` ("building images" on the task row); a cancel stops its wait
  and the Build goes on for whoever else needs it. Verify's "Build workstation" step copies
  the log as it grows and ends with `built <image> in <s>s (<digest>)`, or says `<image> is
  up to date (context unchanged)` without a build pod. Each Build's outcome goes into its
  `image_builds` row (state, digest, log tail); the token's hash is cleared and its Secret
  deleted.
- **Pod security.** Rootless BuildKit creates a user namespace (rootlesskit), which seccomp
  `RuntimeDefault` refuses, and maps subordinate ids with the setuid `newuidmap`. So the
  `build` container has seccomp and AppArmor `Unconfined`, keeps the runtime's default
  capabilities (they bound what root has inside that namespace, which `apt-get` needs) and
  no `allowPrivilegeEscalation: false`; `exradius-builds` is `privileged`. What was checked
  is in §24. kaniko is not an option: it was archived in June 2025.

| | Local (kind) | EKS | GKE | AKS |
|---|---|---|---|---|
| registry | `registry:2` container `kind-registry` on kind's Docker network: `localhost:5001` from the host, `kind-registry:5000` from pods and nodes (containerd `hosts.toml`) | ECR | Artifact Registry | ACR |
| push identity of build pods | none (HTTP, `builds.insecureRegistries`) | EKS Pod Identity + `docker-credential-ecr-login` | Workload Identity + `docker-credential-gcr` | Workload Identity + an ACR token helper |
| pull by nodes | containerd mirror config | node role | node service account | kubelet identity with `AcrPull` |

- The builder image is the pinned upstream rootless BuildKit image
  (`builds.builderImage`, `moby/buildkit:v0.34.1-rootless` by digest). Phase 6 replaces it
  with an Exradius builder image that adds those credential helpers, and values name the one
  to use (`builds.credentialHelper`).
- `image:` references are pulled by the nodes. Private registries named in configs get
  `runs.imagePullSecrets` from values, which replaces the Docker daemon's own login.

## 13. Verify and provider tests

**Verify** stays worker-orchestrated with the same steps table, freshness, diagnosis and
one-open rule:

1. "Read config" (worker), as today.
2. "Build workstation" and "Build service <name>": Build objects (§12).
3. A Run with `purpose: verify`, the same pod as a task run with one difference: the main
   container is `exradius-runner --verify`. It lists each used connection's tools (today's
   `--list-tools`, agent-routing §3.1, now in the pod where `localhost` services answer),
   reports them through the internal API, and exits.

- Its steps are the Run's steps:
  - "Prepare workspace";
  - "Clone <repo> (<branch>)" (the mirror fetch is part of it);
  - "Start service <name>";
  - "Setup: <command>";
  - "List tools <name>".
- "Reserve memory", "Stop services" and "Remove directory" go: there is nothing to reserve or
  clean, because deleting the Run does it.
- `diagnose.py` gains rules for Kubernetes reasons: `ImagePullBackOff`/`ErrImagePull`,
  `Unschedulable` with the scheduler's message, `OOMKilled` under a declared limit, a startup
  probe deadline, and a proxy refusal in setup (none, setup is open) or in list-tools.
- **Verify after keys arrive** (run-environment §8): unchanged.

**Provider tests.** `api_key` tests stay in the api, which calls the provider. `oauth_token`
probes become a Run with `purpose: probe`:

- the default project image and the bundle, an `emptyDir` instead of a workspace, and
  `exradius-runner --probe claude_code [--model]`;
- the result goes through the internal API instead of a log line (`EXRADIUS_PROBE`);
- the credential comes the same way as a task run's (§10).

## 14. Stack services

| | Local (kind, `values-local.yaml`) | Clouds |
|---|---|---|
| Postgres | CloudNativePG operator (installed by the dev setup) + a one-instance `Cluster` created by the chart | RDS, Cloud SQL, Azure Database for PostgreSQL: a URL from a Secret named in values. CloudNativePG remains possible |
| RabbitMQ | RabbitMQ Cluster Operator + a one-replica `RabbitmqCluster` from the chart | Amazon MQ on AWS; the same operator in-cluster on GKE and AKS (no managed RabbitMQ there) |
| Object storage | SeaweedFS (S3 API, Apache 2.0) from the chart | S3, GCS, Azure Blob |

- **Object storage.** `storage.py` gains `gcs` and `azure` backends next to `s3`. Each signs in
  with the cloud's pod identity (IRSA or EKS Pod Identity, GKE Workload Identity, AKS Workload
  Identity), so no key is in values.
  - The `ObjectStore` protocol (put, get, ensure_bucket) is unchanged.
  - The filesystem backend stays for tests only.
  - MinIO is not used: its community edition is no longer maintained.
- **`EXRADIUS_DATA_DIR` goes.** Config-repository mirrors live in an `emptyDir` cache in the
  api and worker pods and are re-fetched after a restart. Runs, mirrors and caches live in
  PVCs; objects in the store.
- Broker URL, database URL, `EXRADIUS_SECRET_KEY` and the admin password come from Secrets that
  values name (`existingSecret`), never from values themselves. The operators' generated
  Secrets (CloudNativePG's `<cluster>-app`, RabbitMQ's default user) are used locally.

## 15. Observability and the panel

- **Durable record.** Transcripts, step logs, build logs, egress records, events: in Postgres
  and the object store, as today. Pod logs disappear with the Run. Collecting container logs
  (Loki, CloudWatch, Cloud Logging, Azure Monitor) is the installer's choice.
- **In the cluster:**
  - `kubectl get runs,builds -A` with printer columns;
  - Kubernetes Events on Runs from the operator (pod created, step failed, OOMKilled,
    unschedulable);
  - controller-runtime metrics on the operator's `/metrics`;
  - a `ServiceMonitor` in the chart when the Prometheus Operator is present (values).
  - api metrics are out of scope for now.
- **System status** (`GET /api/system/status`):
  - worker heartbeats stay, without `docker_ok`, `docker_detail` and `images`; `runtimes`
    becomes the configured bundle image per runtime;
  - an `operator` entry (alive when its leader Lease is fresh, read by the worker);
  - `data_dir` goes.
- `GET /api/system/images` goes: there is no daemon whose images could be listed.
- **Task page:** the run's phase from Run status ("waiting for cluster capacity", step
  progress during `preparing`) and the hosts reached and refused (§11).

## 16. The Helm chart and values

`charts/exradius/` installs everything Exradius owns:

- the api (Service ports 8000 and 8001), worker, frontend, operator and egress Deployments,
  with their ServiceAccounts and RBAC (§3);
- the three namespaces with their Pod Security labels;
- the run identity ConfigMap;
- NetworkPolicies (phase 4);
- an Ingress or Gateway for the panel and API (values);
- the CRDs;
- optionally the Postgres `Cluster`, the `RabbitmqCluster` and SeaweedFS.

- **CRDs** are generated by controller-gen from `operator/` and shipped as templates with
  `helm.sh/resource-policy: keep` behind `crds.install`. Helm never upgrades files in `crds/`,
  and a Run spec change must reach clusters on `helm upgrade`.
- **Prerequisites** are not part of the chart: CloudNativePG and the RabbitMQ operator when the
  in-cluster stack services are used; a CSI storage class; a network plugin that enforces
  NetworkPolicy; for gVisor, a RuntimeClass.

Values files: `values.yaml` (defaults), `values-local.yaml`, and from phase 6
`values-eks.yaml`, `values-gke.yaml`, `values-aks.yaml`. The main keys:

```yaml
publicApiUrl: https://exradius.example.com
secretKey: {existingSecret: exradius-secret-key}
admin: {username: admin, existingSecret: exradius-admin}
broker: {existingSecret: ..., publicHost: ..., publicPort: 5672}
worker: {replicas: 1, concurrency: 4}
runs:
  user: {uid: 1000, gid: 1000}
  defaults: {memoryRequest: 1Gi, cpuRequest: 500m}
  workspace: {storageClass: "", size: 50Gi}
  projectVolume: {enabled: true, accessMode: ReadWriteOnce, storageClass: "", size: 100Gi}
  bundleMount: image            # or copy
  runtimeClassName: ""          # gvisor, kata-..., or the default
  userNamespaces: false
  nodeSelector: {}
  tolerations: []
  imagePullSecrets: []
runtime: {claudeCode: {image: ...}}
projectDefaultImage: ...
egress: {upstreamProxy: "", allow: [], caBundle: {configMap: ""}}
builds: {registry: ..., builderImage: moby/buildkit:<pinned>-rootless, cache: true, insecureRegistries: [],
         resources: {}, credentialHelper: none}   # credentialHelper: phase 6
objectStore: {kind: s3, bucket: exradius, s3: {endpoint: ...}, inCluster: {seaweedfs: true}}
postgres: {existingSecret: ..., inCluster: {cloudnativepg: true, storage: 10Gi}}
rabbitmq: {existingSecret: ..., inCluster: {operator: true}}
ingress: {enabled: false, className: "", host: ""}
crds: {install: true}
```

Terraform (phase 6, `deploy/terraform/<cloud>/`) creates what the values point at:

- the cluster, with a system node pool and a tainted run node pool;
- the registry, the bucket, managed Postgres (and Amazon MQ on AWS), optional RWX storage;
- the workload identity bindings;
- a generated values file.

## 17. The local developer loop

- **Cluster:** kind, through its CLI (`brew install kind`), not Docker Desktop's built-in
  cluster. Checked on 2026-11-08: Docker Desktop's kubeadm cluster (runtime `docker://`)
  enforces no NetworkPolicy, refuses image volumes ("invalid volume specification"), and
  refuses `hostUsers: false` ("runtime does not support user namespaces"). Kind nodes use
  containerd 2.x, kind 0.24+ enforces NetworkPolicy, and kind 0.32 fixed `hostUsers: false` on
  1.36.
- **`deploy/kind/`:**
  - `cluster.yaml`: one node, `kindest/node` pinned to v1.36.x, containerd `hosts.toml` for the
    local registry;
  - `make dev-up`:
    1. starts `kind-registry`;
    2. creates the cluster `exradius` and connects the registry to kind's network;
    3. installs CloudNativePG and the RabbitMQ operator with Helm;
    4. creates the secret-key and admin Secrets from generated values;
  - `make dev-down` deletes both;
  - `make dev-gvisor` (phase 5) adds `runsc` to the node and a `gvisor` RuntimeClass. gVisor
    publishes arm64 builds; whether it works in a kind node on this Mac is phase 5's check.
- **Tilt** (`Tiltfile` at the root):
  - `tilt up` builds the images (backend, frontend, operator, egress, runtime bundle, project
    default; the builder from phase 6, until then the pinned upstream BuildKit image) and
    pushes them to `localhost:5001`;
  - installs the chart with `values-local.yaml`;
  - syncs changed Python files into the api and worker pods and restarts the process, with no
    image rebuild;
  - port-forwards the panel, API and RabbitMQ to `localhost:3100`, `8100`, `5673` and `15673`.
    These avoid the v0.1.0 compose stack's ports and 8080, which is taken.
  - `tilt down` uninstalls.
- **Frontend:** `npm run dev` with `EXRADIUS_API_URL=http://localhost:8100`.
- **Tests:**
  - `uv run pytest` is unchanged (SQLite, in-memory store, local bare repos); the worker's
    Kubernetes client gets a fake.
  - The operator: Go unit tests (Run spec to pod spec, against golden files) and envtest.
  - `tests/test_integration.py`'s Docker parts become `make e2e` on kind: publish a task with
    the CLI and wait for its result.
- **RAM:** Docker's VM has about 8 GB. The stack on kind takes roughly 1.5–2 GB before runs.
  Running the v0.1.0 compose stack, Docker Desktop's own cluster and Plane-sized runs at the
  same time will not fit. Stop what is not in use.

## 18. Repository layout

One repository, as today:

```
backend/            Python: api, worker, runner, runtimes (worker/ loses the Docker modules, gains kube.py)
operator/           Go module, kubebuilder layout: api/v1alpha1 (Run, Build), internal/controller, internal/podspec, cmd/
egress/             Go module: the egress proxy
charts/exradius/    the Helm chart; CRD templates generated from operator/
frontend/
deploy/             Dockerfiles for every image (backend, frontend, operator, egress, runtime bundle,
                    project default, builder); kind/; terraform/ (phase 6)
Tiltfile
docs/
```

Why not split:

- The `Run` and `Build` specs are the contract between the Python worker and the Go operator.
  A change to them touches both and the chart's CRDs, and should be one commit and one review.
- The chart, the CRDs and the images release together under one version and one tag.
- CI and the e2e test on kind need all of it.

The bridges (Jira, GitHub) stay separate. They depend only on the public message contract and
the integration API, which this change keeps. Split later only if the chart or the operator
gets users and a release cadence of its own.

Two Go modules rather than one, because the proxy shares no types with the operator and should
keep a small dependency set. A `go.work` file ties them together for development.

## 19. What stays, what goes

| Today | On Kubernetes |
|---|---|
| message contract (architecture §4), integration API, bridges | unchanged |
| config-repo model, strict schema | unchanged, plus `network.allow`, ports unique across services, `localhost` reserved |
| `intake.py`, `lifecycle.py`, `states.py`, events and outbox | unchanged |
| `runs.py` (`RunManager`) | reworked: creates Runs and Secrets, settles from Run status, recovers by listing Runs |
| `containers.py` | goes; its pod-level knowledge moves into the operator's pod builder |
| `builds.py` | tag computation and the context tar stay in the worker; building goes to Build objects |
| `prepare.py` | git mirror and clone functions move into the runner package (run in the pod); run directories go |
| `bundles.py` | goes (image volume) |
| `memory.py` | goes (requests, limits, the scheduler) |
| `caches.py` | path validation and naming stay; mounts by the operator; size measurement goes |
| `delivery.py` | the script moves into the runner |
| `git_identity.py` | stays (Run spec env); hooks dispatcher moves into the bundle |
| `verify.py`, `provider_test.py` | orchestration stays; steps from Build and Run status |
| `heartbeat.py`, `presence.py` | heartbeat without Docker; run ownership no longer needs presence |
| `diagnose.py` | stays, gains Kubernetes reasons |
| runner, prompts, `exradius` MCP tools, runtimes | stay; the runner waits in-process and gains `prepare`, `clone`, `--verify`, the files sidecar mode and the delivery check |
| `api/internal.py` | stays, on port 8001, with the new routes of §9 |
| `storage.py` | gains `gcs` and `azure` |
| panel | unchanged, except system status and the task page (§15) |
| Postgres models | `runs.container_id`, `directory` and `worker_id` give way to the Run's name; new `egress_records`, `project_verifications.token_hash`, `runs.waiting_for = 'capacity'` |
| `deploy/docker-compose.yml`, `EXRADIUS_DATA_DIR`, the Docker socket | go in phase 1 |

## 20. Changes to existing docs

To be rewritten in the phase that makes them true, and only then:

- **`architecture.md`:**
  - §3: components and diagram, from this doc's §3.
  - §5.1–5.5: preparing as Run creation and init containers; waiting in-pod; ending by
    deleting the Run.
  - §6 and §12 data model: the columns of §19.
  - §9 security notes: pod security, NetworkPolicy, proxy injection.
  - §10 layout: §18.
  - §11 deferred: "multi-host workers" is done.
  - §12: every Docker paragraph ("Run containers and directories", networks, bundles
    extraction, memory, caches, data directory, reconciliation and orphan sweep) is replaced by
    pointers here.
  - §14: "Docker" becomes "Kubernetes" in the principle, and the audit table gains the new
    levels (ConfigMap passwd, hostAliases, NetworkPolicy, limits).
- **`runtime-bundles.md`:**
  - §2.2: the image volume, and the copy fallback.
  - §2.4: passwd from a ConfigMap; the home on the workspace PVC.
  - §2.5: the backend image has no Docker SDK.
  - §4: images built by Tilt or CI, not compose.
  - The hooks dispatcher in the bundle.
- **`environment-and-services.md`:**
  - §2: services as sidecars on `localhost` with `hostAliases`, startup probes, shared ports,
    HEALTHCHECK mapping.
  - Networks become NetworkPolicy.
  - §1: the network line of the environment section (phase 4).
- **`run-environment.md`:**
  - §2 and §2.1: the principle and the audit at the Kubernetes level.
  - §4: requests and limits instead of the budget; "waiting for cluster capacity".
  - §5: caches on the project volume, RWX or off.
  - §9: the skill's local check mirrors the pod. Services share the workstation's network
    (`docker run --network container:`), there are proxy variables, and `network.allow`.
- **`config-repo.md`:**
  - §2: `network.allow`, the port and name rules, what `memory` means now.
  - §4: Build objects and the registry.
  - §9: the worker's steps.
- **`setup-and-verify.md`:** the Verify step list (§13), and the skill's check script.
- **`onboarding.md`, `providers.md`:** system status without Docker; the probe as a Run;
  Settings without the data directory.
- **`agent-routing.md` §3.1:** "List tools" runs in the verify pod.
- **`ideas.md`:** the egress proxy entry points here.
- **README "Running it" and `CLAUDE.md` "Commands":**
  - from compose to the local setup: kind, kubectl, helm, tilt, `make dev-up`, `tilt up`, the
    ports, no data directory;
  - a short "Installing on a cluster" with Helm and a values file;
  - the README's "Configuring a project and an agent" section is already known to be stale.

## 21. Facts checked on 2026-11-08

| Fact | Source |
|---|---|
| Kubernetes 1.36 on the cluster reports `SidecarContainers`, `ImageVolume`, `UserNamespacesSupport`, `InPlacePodVerticalScaling` as GA, `PodLevelResources` and `ContainerRestartRules` as beta | the API server's `kubernetes_feature_enabled` metrics |
| User namespaces GA in 1.36 | kubernetes.io blog, 2026-04-23 |
| Image volumes need containerd 2.1+ or CRI-O | Kubernetes image volume docs; containerd release notes |
| Docker Desktop kubeadm (`docker://29.8.2`): no NetworkPolicy enforcement, no image volumes, no user namespaces | live probes in a throwaway namespace |
| kind: kindnet enforces NetworkPolicy since v0.24; containerd 2.x since v0.27; `hostUsers: false` fix for 1.36 in v0.32; v0.33 defaults to 1.37 | kind release notes |
| agent-sandbox v1.0.10 (2026-11-06), API `agents.x-k8s.io/v1beta1`: `Sandbox` (pod template, volume claim templates, `operatingMode: Running \| Suspended`, `shutdownTime`, `failedPodPolicy`), `SandboxTemplate`, `SandboxClaim`, `SandboxWarmPool`; breaking changes still land in patch releases (router) | github.com/kubernetes-sigs/agent-sandbox, `api/v1beta1/sandbox_types.go`, releases |
| kaniko archived 2025-06-03; rootless BuildKit is the common replacement | GitHub; GitLab docs |
| MinIO community edition unmaintained; SeaweedFS, Garage, RustFS are the usual alternatives | project pages and comparisons |

## 22. Decided against, and later

- **agent-sandbox.** Not used for now. Its `Sandbox` adds a controller and CRDs to install and
  upgrade for what a Pod and a PVC already give Exradius. Its warm pools assume interchangeable
  pods, while a run's pod is fixed at creation: project images, references to its Secret, its
  token. Phase 5 writes down whether warm pools are worth their own design. That design would
  be a per-project pod template, with the runner learning its run after a claim through a
  downward API file and a pod-bound token. The operator builds pods behind an interface, so a
  Sandbox backend can be added.
- **Stopping pods while waiting.** Rejected: every resume would restart services empty. Only a
  lost pod is recreated (§8.3).
- **A TLS-intercepting proxy.** Rejected: every tool would have to trust an Exradius CA.
- **A long-running buildkitd or the clouds' build services.** Rejected: the first gives the
  worker a builder client and a shared long-lived target; the second gives each cloud its own
  code path.
- **Later:**
  - resuming a run whose pod was lost while running;
  - Kueue;
  - api metrics;
  - a `resources.disk` key;
  - restricting DNS;
  - pre-pulling project images;
  - registry garbage collection.

## 23. Settled in phase 1

Details the earlier sections left open or got wrong, decided while building phase 1
(2026-11-08). The sections above are corrected where they were wrong; this is the list.

- **The Run spec** gains `id` (the run, verification or provider test id the pod reports
  to), `taskId` (printer column), `runtime.env` (the provider's variables, for the
  workstation only), `git.setupEnv` (setup containers' command-line git config) next to
  `git.env`, `repositories[].credentialKey`, and `model` for probes. An env entry is
  `{name, value}` or `{name, secretKey}`. Per-run Secret keys: `token`, `env.<NAME>`,
  `provider.<NAME>`, `repo.<n>` (a repository's credential, for its clone), `push.<n>` (the
  token part, for the push helper). The worker builds both in `worker/run_objects.py`.
- **Run status** carries `reason` and `message` (the operator's own failures:
  `Cancelled`, `PodLost`, `StepFailed`, `ImagePullFailed`, `CreateContainerConfigError`,
  `OOMKilled`, `Error`; `Unschedulable` while `Pending`), `podGeneration` (of the pod last
  created), `pullFailures` and `configErrorSince`. A container that could not start has no
  log, so its step shows the runtime's termination message.
- **Containers and markers:** `clone-<n>` and `setup-<n>` (§7); markers in `state/steps/`;
  `files` runs from the workstation image (the bundle image has no libc). Clone containers
  also get `EXRADIUS_CLONE_FOLDER`, `_URL`, `_BRANCH`, `_INDEX` and `EXRADIUS_GIT_AUTH`.
- **The bundle image's root is the bundle.** An image volume mounts the image's root, so
  the bundle is at `/` in `exradius-runtime-claude-code`, not under `/opt/exradius/runtime`
  (found on kind: the runner was not found at its path).
- **Without a project volume** (until phase 3) each clone fetches straight from the remote.
- **Reports and transitions.** `/reported` stores `reported_status` (`waiting_for_input` or
  `resumed`); the worker resets it to `null` when it moves a run to `resuming`, so a report
  in `resuming` always means the runner resumed. The runner keeps the ids of the answers it
  resumed with and ignores them on the next long-poll. On SIGTERM the runner reports
  `cancelled` when the run was cancelled, and otherwise fails the run with "the run's pod was
  stopped (SIGTERM) before the run ended".
- **Recreating a lost pod** is decided in `resume()` under the run-row lock (bump
  `spec.podGeneration`, then commit `resuming`), and a settle of a `resuming` run reads the
  Run again before it fails it, so the old pod's end is never taken for the new one's. The
  operator deletes older pods and waits until they are gone before it creates the new one,
  so two runners never share a workspace.
- **Recovery of a run in `preparing` without a Run** (the worker died before creating it):
  failed at once when no other worker sent a heartbeat in the last minute, else after 30
  minutes. `runs.run_object` tells it from a Run that disappeared.
- **Concurrency.** A slot is held while a run of this worker prepares, runs or resumes, and
  given back while it waits for input; recovered runs take a slot of the new worker.
- **The api** listens on both ports in one uvicorn server (two sockets); a middleware keeps
  `/internal/*` to 8001 and the rest to 8000. Both the api and the worker run
  `exradius migrate` (alembic under a Postgres advisory lock) in an init container.
- **Settings.** `EXRADIUS_DATABASE_URL` accepts CloudNativePG's `postgresql://` URI;
  `EXRADIUS_BROKER_USERNAME` and `EXRADIUS_BROKER_PASSWORD` replace the broker URL's user
  info (from the RabbitMQ operator's Secret); `EXRADIUS_CACHE_DIR` holds config mirrors.
- **The local stack.** `make dev-up` also installs cert-manager, which the RabbitMQ Cluster
  Operator needs for its webhooks. The chart does not create the release namespace (Helm
  cannot own it); `dev-up` creates `exradius` with the `restricted` labels. SeaweedFS runs
  as one `weed server` with only S3 exposed. The panel's nginx is unprivileged on 8080.
  Everything in `exradius` passes Pod Security `restricted`.
- **Operator RBAC** adds `runs/finalizers` (owned objects carry `blockOwnerDeletion`) and
  events in both the core and `events.k8s.io` groups.
- **Tilt** disables its Docker build cache pruning (by default it deletes every build cache
  older than six hours, other projects' too). A Python change rebuilds the bundle, because
  run pods mount it, and rolls the worker; the api is updated in place.
- **The kubelet's API server variables.** `enableServiceLinks: false` does not remove
  `KUBERNETES_SERVICE_HOST` and the other `KUBERNETES_SERVICE_*`/`KUBERNETES_PORT*`
  variables: the kubelet adds them to every container (checked on kind). The runner's
  `isolate()` removes them from its own process and from the agent's environment (the SDK
  starts the CLI with both); setup commands still see them.
  They give no access: run pods have no ServiceAccount token, and NetworkPolicy blocks the
  API server from phase 4.
- **Supplementary groups.** containerd gives the run uid the groups the image's own
  `/etc/group` lists for that uid (`sudo`, `adm` on Ubuntu images), before the generated
  `/etc/group` is mounted. `supplementalGroupsPolicy: Strict` (§7.8) prevents that.
- **Known gap, unchanged from v0.1.0:** the runner is the workstation's PID 1 and runs as the
  agent's uid, so the agent can read the run token in `/proc/1/environ`. The token reaches
  only this run's internal API. Phases 4 and 5 should decide whether to pass it as a file
  readable by another uid.

## 24. Settled in phase 2

Details decided while building image builds (2026-11-09). §12 is written to match; this is
the list.

- **The Build spec** is `id` (the `image_builds` row, the context's path), `project`,
  `name`, `image`, `cache`, `dockerfile` (its name inside the context) and `secretName`.
  Status carries `phase`, `reason`, `message`, `podName`, `nodeName`, `startedAt`,
  `finishedAt`, `digest`, `log`, `conditions`, `pullFailures`, `configErrorSince`. The
  worker builds both in `worker/builds.py`; the operator's side is `podspec/build.go`,
  `status/build.go` and `controller/build_controller.go`.
- **The token** of a Build is a Secret in `exradius-builds` owned by the Build, written by
  the worker (its RBAC there: Builds create/get/watch/delete, Secrets create/patch/delete).
  `/internal/builds/{id}/context` accepts it while the row is `running`; the worker clears
  the hash and deletes the Secret once it records the outcome. `image_builds` gains
  `build_object`, `digest`, `context_key` and `token_hash` (migration 0007).
- **A failed Build is retried** by the next caller, not kept as the answer for its tag.
- **The pod is deleted when the Build ends**, after its status and log are stored.
- **The operator's cache** is per type: Runs and PVCs in the runs namespace, Builds in the
  builds namespace, pods in both. A cache that watched every type in both namespaces asked
  for rights RBAC rightly refuses (found on kind).
- **Rootless BuildKit in a read-only root** needs `TMPDIR=/tmp` (the image sets it under the
  home) and `DOCKER_CONFIG` outside the home; buildctl writes its metadata file through a
  temporary file beside it, so it goes to `/tmp` and is copied into the termination message.
- **The registry cache works across builds:** a build whose Dockerfile's first `RUN` was
  unchanged extracted that layer from `<image>:buildcache` instead of running it (14 s
  became 6 s on kind). The first build of an image logs a failed cache import, which is
  harmless.
- **The builder image is upstream's**, pinned by digest in values, until phase 6 needs
  credential helpers. Tilt does not build one.
- **`exradius-builds` below `privileged`, checked on kind** (v1.36.4 on Docker Desktop, Mac
  arm64; `moby/buildkit:v0.34.1-rootless`, `--oci-worker-no-process-sandbox`):

  | Pod Security shape of the build container | Result |
  |---|---|
  | seccomp and AppArmor `Unconfined` (needs `privileged`) | builds and pushes |
  | `baseline`: seccomp `RuntimeDefault` | fails: `[rootlesskit:parent] error: failed to start the child: fork/exec /proc/self/exe: operation not permitted` |
  | `baseline`, `RuntimeDefault`, `hostUsers: false` | the same failure |
  | `restricted` shape (`allowPrivilegeEscalation: false`, drop `ALL`), `RuntimeDefault`, `hostUsers: false` | the same failure |
  | `baseline`, seccomp `Localhost` with an allow-all profile on the node | builds and pushes (no AppArmor setting needed on this node) |
  | `restricted` shape with that `Localhost` profile | fails: `newuidmap: operation not permitted` (no setuid under no_new_privs) |
  | `baseline`, `Localhost` profile, `hostUsers: false` | fails: `newuidmap: write to uid_map failed` (the image's 65536 subordinate ids do not fit the pod's mapping) |

  So: not below `privileged` with the runtime's default seccomp profile, with or without
  user namespaces. `baseline` is possible with a node-installed `Localhost` seccomp profile
  that allows creating user namespaces; phase 5 can write a tailored one (an allow-all
  profile proves only that seccomp is the one obstacle). `restricted` is not possible with
  rootless BuildKit.
- **Diagnosis** reads BuildKit's plain progress (`#10 0.044 /bin/sh: 1: corepack: not
  found`) without its step and time prefix.
