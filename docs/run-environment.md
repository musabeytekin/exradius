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
| `/work`, home, `/tmp`, state, attachments | bind mounts | no | none |
| `safe.directory=*` | git command-line level (`GIT_CONFIG_*`) | no | none |
| author and committer identity | `GIT_AUTHOR_*`/`GIT_COMMITTER_*` (beats all config) plus the managed global config | no | none |
| `core.hooksPath` → the Exradius hooks | managed global config | **yes** (a repo-local value wins; husky, lefthook and `git config core.hooksPath` all write one) | §3 |
| credential helpers for push | managed global config | partly (a repo-local helper could be added; ours still answers) | move to command-line level with the hooks path (§3) |
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

## 4. Memory: declared, reserved, enforced

Two findings are the same gap: the Outline kill, and Plane being run one task at a time by
hand because its install needs several gigabytes. The worker admits runs by count
(`EXRADIUS_WORKER_CONCURRENCY`) and knows nothing about size.

`exradius.yaml` gains:

```yaml
resources:
  memory: 6g          # the workstation (setup, run, resume containers); optional
services:
  postgres:
    image: postgres:16
    memory: 512m      # optional, per service
```

- **Reservation.** The worker keeps a memory budget, `EXRADIUS_RUN_MEMORY_BUDGET`
  (default: the Docker daemon's `MemTotal` minus 1 GiB, read at start). A run's reservation
  is `resources.memory` plus every declared service `memory`. A run starts only when the
  reservations of the runs and verifications in progress plus its own fit the budget;
  otherwise it stays `queued` (message unacknowledged, as with the concurrency slot) and
  the panel's task row says `waiting for memory`. Undeclared means zero reserved, so a
  project that declares nothing behaves as today.
- **Enforcement.** A declared `memory` is also the container's cgroup limit (`mem_limit`,
  and `memswap_limit` equal to it, so swap does not hide an overrun). A run that exceeds it
  is killed by its own cgroup, not by the host killing a neighbour, and ends `failed` with
  the diagnosis "exceeded its declared memory (6g)"; the same for a service. Undeclared
  means no limit, as today.
- Verify reserves and limits the same way, so a limit that is too low shows up in Verify,
  not in the first task.
- The environment section does not mention memory. The skill estimates it and writes it
  (it already does, in the README; now in the file).

This revisits the earlier "no run limits" decision deliberately: an undeclared run is
still unlimited, and a declared limit is the project's own statement, enforced so that it
means something for the other runs on the machine.

## 5. Per-project caches

Every Plane run spends about 65 s in `pnpm install` and every Outline run longer in
`yarn install`, downloading the same packages into a home that is deleted with the run.

```yaml
workstation:
  cache:
    - ~/.local/share/pnpm/store
    - ~/.cache/uv
    - ~/.m2/repository
```

- Each entry is a path inside the container, `~`-relative or absolute, outside `/work`.
  Each is bind-mounted from `<data_dir>/caches/<project slug>/<n>-<sanitised path>/`,
  created on first use and owned by the run user, into the setup, run and resume
  containers and into Verify. The same directories are shared by every run of the project,
  concurrent ones included; package managers that write their stores concurrently (pnpm,
  uv, npm, Maven, Go) are built for that.
- Nothing cleans them. The size per project is shown on the project page (one `du` at
  read time is enough); an admin removes the directory to reset. A later "Clear caches"
  button is out of scope.
- Hard links do not cross bind mounts, so pnpm copies from its store into `/work`
  instead of linking. That is still a download saved; the skill notes it.
- Trust: a cache is per project, so a run can only affect later runs of the same project,
  the same boundary the repository itself already has.

## 6. Services get `command`

```yaml
services:
  minio:
    image: minio/minio
    command: [server, /data]
```

`command: list[str]` is passed as the container command; the image's entrypoint is kept.
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

A verification that failed only because the store did not exist or keys were missing is
not rerun when the keys arrive (the automatic trigger is a new config commit). After
`POST /api/stores`, `PUT /api/stores/{id}/keys` and `PUT /api/stores/{id}/keys/{key}`, every
project whose config names that store and whose required keys are now all present and
whose freshness is not `current` gets a verification enqueued, under the one-open rule.

## 9. The skill

The skill (`musabeytekin/exradius-skill`) is updated in step, as a release after this
batch lands: the schema reference gains `resources`, service `memory`, service `command`
and `workstation.cache`; the pitfalls gain the cache paths per package manager (pnpm, yarn
Berry, npm, uv, pip, Maven, Gradle, Go, Cargo) and the hard-link note; the run conditions
state the two known limits up front: no Docker daemon inside a run (tests that start
containers themselves cannot run; use `services`), and a read-only root (nothing is
installed at run time outside the home; what a run needs goes in the Dockerfile). The
check script accepts and applies the new keys (`memory` as a `--memory` flag, `cache` as
bind mounts from a local cache directory, `command`). The four example configs in the
skill's references gain `resources` and `cache` lines.

## 10. Out of scope

Docker inside runs; CPU or disk reservations; cache eviction; any change to the operating
rules or the environment section.
