# The run environment: one principle, and the batch it implies

Agreed 2026-11-07, after four projects (bank, Outline, Saleor, Plane). Internal design for
the implementation sessions.

## 1. Why

Across the four projects, one category of finding repeated with the same shape: a tool
assumed a normal developer environment and the run container was not one.

| Finding | Root cause | Fix that worked |
|---|---|---|
| Maven ignored `~/.m2` (bank) | `HOME` set as an environment variable; Java reads the home from passwd | a generated `/etc/passwd` mounted into the container |
| A run was killed, exit 137 (Outline) | the home and `/tmp` were tmpfs, so a package install lived in RAM | bind mounts on disk |
| `postgres` resolved to Exradius's own database (bank) | runs joined the stack network | a dedicated network |
| Commit trailers missing (Plane) | `core.hooksPath` set in the run's global git config; husky's `prepare` script wrote a repo-local one, which wins | pending: §3 |

Every fix moved the same way: from a setting applied at the configuration level, where
userland tools expect to be in charge, to one applied where they cannot interfere.

## 2. The principle

> From the inside, a run looks like a developer's own workstation. Exradius deviates only
> where security requires it, and every deviation, and everything Exradius needs to stay
> true inside a run, is enforced at the level of the kernel, Docker or the command line,
> never through a setting a project, a tool or the agent can reset.

Goes into `docs/architecture.md` as §14, with the table above.

### 2.1 Audit of what Exradius sets inside a run

| What | Level today | Overridable by a project? | Action |
|---|---|---|---|
| uid 1000 as `runner`, home `/home/runner` | passwd and group files bind-mounted | no | none |
| read-only root, no capabilities, no-new-privileges | Docker | no | none |
| networks (`exradius-runs` + the run's own) | Docker | no | none |
| what a run reaches (since phase 4: DNS, the egress proxy, the api's internal port) | NetworkPolicy, and the proxy's decision by the pod's phase and host (`docs/kubernetes.md` §11) | no: the policy is the chart's, the decision the proxy's, from the pod watch | none |
| the model, repository and connection credentials | since phase 4 not in the pod: the egress proxy's routes add them | no | none |
| the proxy for the project's tools | `HTTP(S)_PROXY`, `NO_PROXY`, `YARN_HTTP(S)_PROXY`, `JAVA_TOOL_OPTIONS`, written by the operator after the project's `env` | no: ours come last; a project's `JAVA_TOOL_OPTIONS` is kept before ours | none; a tool that reads none of them is configured in the workstation image |
| `/work`, home, `/tmp`, state, attachments | bind mounts | no | none |
| `safe.directory=*` | git command-line level (`GIT_CONFIG_*`) | no | none |
| author and committer identity | `GIT_AUTHOR_*`/`GIT_COMMITTER_*` (beats all config) plus the managed global config | no | none |
| `core.hooksPath` → the Exradius hooks | managed global config | **yes** (a repo-local value wins; husky, lefthook and `git config core.hooksPath` all write one) | §3 |
| credential helpers for push | managed global config | partly (a repo-local helper could be added; ours still answers) | moved to the command-line level with the hooks path (§3); since phase 4 replaced by the proxy's git route (`insteadOf` at the command-line level) |
| `push.autoSetupRemote`, `init.defaultBranch` | managed global config | yes, and that is fine: conveniences | none |
| Claude Code settings written by the runtime (`includeCoAuthoredBy: false`, ...) | user-level settings in `CLAUDE_CONFIG_DIR` | **yes**: a repository's `.claude/settings.json` outranks user settings | §7 |
| runtime bundle not on `PATH`; runner strips its own interpreter variables | runner process | not applicable | none |

## 3. Git hooks at the command-line level, chained to the project's own

`core.hooksPath` and the credential entries move from `exradius.gitconfig` to the
`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`/`GIT_CONFIG_VALUE_n` variables already used for
`safe.directory` (`containers.py` `GIT_ENV` becomes a function of the run). The managed
global config keeps identity and the two conveniences.

A forced hooks path means the project's own hooks never run, which breaks "looks like a
developer's workstation": Plane's husky `pre-commit` runs lint-staged, and a developer
expects it. So the Exradius hooks directory holds every standard hook name
(`applypatch-msg` ... `pre-push`, the list in `githooks(5)`), each a copy of one dispatcher:

1. run the Exradius part when there is one (today only `prepare-commit-msg`: the trailers);
2. find the project's hook: `git config --local core.hooksPath` if set (husky's `.husky`,
   lefthook's path), else `$(git rev-parse --git-path hooks)`; run `<dir>/<name>` when it
   exists and is executable, with the same arguments and stdin, and exit with its status.

A project hook that fails still fails the commit, as it would for a developer. The
dispatcher runs as the run user with the project's environment, so it can only do what the
agent could do anyway.

## 4. Memory: declared, placed, enforced

Two findings are the same gap: the Outline kill, and Plane being run one task at a time by
hand because its install needs several gigabytes. The worker admitted runs by count and knew
nothing about size.

`exradius.yaml` has:

```yaml
resources:
  memory: 6g          # the workstation: its setup commands and the agent's container; optional
services:
  postgres:
    image: postgres:16
    memory: 512m      # optional, per service
```

Sizes are binary (`6g` is 6 GiB). Rewritten for Kubernetes in phase 3 (2026-11-09;
`docs/kubernetes.md` §6); v0.1.0's per-worker budget, `EXRADIUS_RUN_MEMORY_BUDGET` and the
"Reserve memory" step are gone.

- **Placement.** A declared `memory` is the request and the limit of its containers: the
  setup containers and the workstation for `resources.memory`, the service's sidecar for a
  service's. The scheduler places a run's pod on a node whose free requests fit it. A pod
  that fits nowhere waits in `Pending`: the task stays `preparing`, no status event is
  published, and the panel's task row says "waiting for cluster capacity"
  (`runs.waiting_for = 'capacity'`). It starts when another run ends and frees its requests.
  There is no deadline for that wait (no run limits).
- **Enforcement.** A container over its declared limit is killed by its own cgroup, not by
  the node killing a neighbour, and the run ends `failed` with the diagnosis "exceeded its
  declared memory (6g)": "setup command '…' exceeded its declared memory (6g) and was killed
  (exit 137)", "the run exceeded its declared memory (6g) …", or for a service that dies
  before it is ready "service <name> (<image>) exceeded its declared memory (512m): …".
- **Undeclared** memory gets the install's default request (`runs.defaults.memoryRequest`
  for the workstation, `serviceMemoryRequest` for a service) and no limit. CPU gets a request
  from values and never a limit, because limits throttle builds and tests.
- `worker.concurrency` still caps the runs one worker has in flight, so RabbitMQ keeps the
  backlog. Kueue (quota-based admission) can come later.
- Verify uses the same pod, so a limit that is too low shows up in Verify, not in the first
  task.
- The environment section does not mention memory. The skill estimates it and writes it.

This revisits the earlier "no run limits" decision deliberately: an undeclared run is still
unlimited, and a declared limit is the project's own statement, enforced so that it means
something for the other runs on the cluster.

## 5. Per-project caches

Every Plane run spent about 65 s in `pnpm install` and every Outline run longer in
`yarn install`, downloading the same packages into a home that is deleted with the run.

```yaml
workstation:
  cache:
    - ~/.local/share/pnpm/store
    - ~/.cache/uv
    - ~/.m2/repository
```

Rewritten for Kubernetes in phase 3 (2026-11-09; `docs/kubernetes.md` §7.6).

- Each entry is a path inside the container, `~`-relative or absolute, outside `/work`.
  Each is the directory `caches/<n>-<sanitised path>` of the project's **project volume**, a
  PVC (`exradius-project-<slug>`) that the operator creates on the project's first run and
  the worker deletes with the project. It is mounted at its path in the setup containers and
  the workstation, of tasks and of Verify. The same directories are shared by every run of
  the project, concurrent ones included; package managers that write their stores
  concurrently (pnpm, uv, npm, Maven, Go) are built for that.
- The run's first container creates the directories, and the mount point of every cache
  below the home or `/tmp`, as the run user; otherwise the kubelet would create a missing
  `~/.cache` as root and other tools could no longer write there.
- The same volume holds the repositories' git mirrors (`docs/kubernetes.md` §7.5), so a
  clone fetches only what is new.
- Values `runs.projectVolume`: `ReadWriteOnce` for one node (kind), `ReadWriteMany` with an
  RWX storage class (EFS, Filestore, Azure Files, NFS) for several, or `enabled: false`: then
  clones fetch from the remote and each cache is an empty directory of the run's own
  workspace. An RWX class is NFS or SMB, which user-namespaced pods cannot mount
  (`docs/kubernetes.md` §28).
- Nothing cleans the caches, and the panel no longer shows their size: no Exradius process
  mounts the volume. Resetting a project's caches means deleting its PVC while no run of the
  project is active (`kubectl -n exradius-runs delete pvc exradius-project-<slug>`); the
  next run creates it again, empty.
- Hard links do not cross mounts, so pnpm copies from its store into `/work` instead of
  linking. That is still a download saved; the skill notes it.
- Trust: a cache is per project, so a run can only affect later runs of the same project,
  the same boundary the repository itself already has.

## 6. Services get `command`

```yaml
services:
  minio:
    image: minio/minio
    command: [server, /data]
```

`command: list[str]` is the container's arguments (Docker's `CMD`); the image's entrypoint
is kept.
Plane's bucket bootstrap no longer needs a Dockerfile. No `entrypoint` key: an image that
needs its entrypoint replaced is a Dockerfile, as today.

## 7. Claude Code settings the runtime needs

`claude_code.py` writes `includeCoAuthoredBy: false` (and whatever else Exradius needs) as
user-level settings in `CLAUDE_CONFIG_DIR`. A repository's `.claude/settings.json`
outranks them. The implementation session checks what the Agent SDK offers to make
Exradius's values final (a managed or command-line settings source, or the SDK option
that limits which setting sources load) without losing the project's own `CLAUDE.md`,
`AGENTS.md` and `.claude/` content, which is the team's guide and must stay. If nothing
makes them final, the environment section is not the place either; document the gap in
§12 of the architecture notes and leave it.

## 8. Verify after the keys arrive

A verification that failed only because the store did not exist, did not allow the project
or lacked keys is not rerun when that is fixed (the automatic trigger is a new config
commit). After `POST /api/stores`, `PATCH /api/stores/{id}` (allowed projects, kind and
settings), `PUT /api/stores/{id}/keys` and `PUT /api/stores/{id}/keys/{key}`, every project
whose config names that store, which is runnable now and whose freshness is not `current`
gets a verification enqueued, under the one-open rule. A key added in Vault is not a write
Exradius sees: re-run Verify, or wait for the next config commit.

## 9. The skill

The skill (then `musabeytekin/exradius-skill`, now `skill/`) is updated in step, as a release after this
batch lands: the schema reference gains `resources`, service `memory`, service `command`
and `workstation.cache`; the pitfalls gain the cache paths per package manager (pnpm, yarn
Berry, npm, uv, pip, Maven, Gradle, Go, Cargo) and the hard-link note; the run conditions
state the two known limits up front: no Docker daemon inside a run (tests that start
containers themselves cannot run; use `services`), and a read-only root (nothing is
installed at run time outside the home; what a run needs goes in the Dockerfile). The
check script accepts and applies the new keys (`memory` as a `--memory` flag, `cache` as
bind mounts from a local cache directory, `command`). The four example configs in the
skill's references gain `resources` and `cache` lines.

Since phase 3 of the move to Kubernetes the check script mirrors the run's pod rather than
v0.1.0's containers: one container holds the network namespace, with every service name
mapped to `127.0.0.1`, and the services, the setup commands and the workstation join it
(`docker run --network container:<it>`), so services are reached on `localhost` by name and
two services on one port collide as they would in a run. A service's `command` is its
arguments, its `tcp` check connects to the shared address, and the 120 s deadline counts
from its start. The egress proxy variables and `network.allow` came with skill 0.4.0
(setup-and-verify.md §2.7), which also runs the project's own check commands as the agent
would.

## 10. Out of scope

Docker inside runs; CPU or disk reservations; cache eviction; any change to the operating
rules or the environment section.
