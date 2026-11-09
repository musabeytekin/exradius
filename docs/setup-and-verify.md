# Project setup skill and verification without clicks

Agreed 2026-11-07, after setting up three projects (bank, Outline, Saleor) by hand. Each
one failed Verify once on a real mistake in its workstation Dockerfile (no corepack on
node:26, a uv too old for the lockfile, python3 missing from PATH). Verify found the
mistakes; the loop around it was slow: edit locally, commit, push, Re-read, Verify, wait,
read a log tail. Two changes: Verify runs itself and diagnoses, and a Claude Code skill
does the setup work on the developer's machine, where the loop takes seconds.

Internal design for the implementation sessions. Exradius still carries no user
documentation; the skill is the executable form of the developer documentation.

## 1. Verify runs itself and says what is wrong

### 1.1 When it runs

- `POST /api/projects` enqueues a verification as soon as the config was read (readable
  file, with or without validation errors; a file that cannot be read at all is a 422 as
  today and nothing is enqueued).
- `POST /api/projects/{id}/reread` enqueues one when `snapshot_commit` changed.
- The Verify button stays for a manual run.
- At most one open verification per project: a new request while one is `pending` or
  `running` returns that one.

### 1.2 Verified at which commit

`project_verifications` gets `commit` (nullable), set by the "Read config" step. The
project view (`ProjectOut`) gets `verification`: the latest verification's `id`, `state`,
`commit`, `ended_at`, `diagnosis`, plus `freshness`:

| freshness | meaning                                                       |
|-----------|---------------------------------------------------------------|
| current   | passed at `snapshot_commit`                                   |
| stale     | passed, but at an older commit than `snapshot_commit`         |
| failed    | the latest verification failed (any commit)                   |
| running   | pending or running                                            |
| never     | no verification yet                                           |

Projects list, Overview and the project header show it as one word with the short commit.
`runnable` stays what it is (config valid and keys present); freshness is advice, never a
gate.

### 1.3 Diagnosis

`project_verifications.diagnosis` (nullable text): one or two sentences the worker writes
in `_finish` from the failed step and its log tail, in `exradius/worker/diagnose.py`. A
table of patterns, first match wins, each giving the sentence and what to change:

- `command not found` / `No such file or directory` for an executable in a setup command
  → the workstation image lacks that tool; name it.
- a Dockerfile build failure → the failing instruction (the last `RUN`/`COPY` line in the
  tail) and the first error line after it.
- `exit 137` → killed: out of memory in the setup command.
- lockfile/tool mismatches: uv (`exclude-newer`, `unsupported lock version`), yarn
  (`packageManager`, corepack), pnpm, npm `lockfileVersion` → the tool in the image is
  older than the lockfile needs; name the tool.
- service readiness timeout → the service never became ready; name it, suggest checking
  its env and `ready`, and for a `tcp` check that a service listening only on 127.0.0.1
  needs a `ready` command (the check connects to the run's own address).
- a service that exits before it is ready → name it and its exit code; point at its env,
  its `command` and its ports (services share the run's `localhost`).
- a container killed under a declared limit → which limit (`resources.memory` or the
  service's `memory`) to raise; a service that ran out of memory without one → declare it.
- an image that cannot be pulled → the config key that names it (`workstation.image`,
  `services.<name>.image`) and the install's `runs.imagePullSecrets`.
- a pod that fits on no node ("waiting for cluster capacity", the scheduler's message) →
  lower `resources.memory`, or add a node with room; otherwise the install's node selector
  and tolerations.
- clone failures: authentication → the repository credential; not found → the URL or
  branch.
- missing store keys / config errors → the errors themselves (already text).
- no match → `null`; the panel then shows only the step and the tail.

Patterns are plain substring and regex checks on the tail; no model call.

### 1.4 The steps

Since 0.2.0 Verify is a Run like a task's (docs/kubernetes.md §13), so its steps are the
pod's:

1. "Read config" (the worker): commit, errors, warnings, missing keys;
2. "Build workstation", "Build service <name>": one per Dockerfile, each a Build object
   with its log as it grows, or "… is up to date (context unchanged)";
3. "Prepare workspace";
4. "Clone <repo> (<branch>)" per repository, which refreshes its mirror on the project
   volume first;
5. "Start service <name>" per service, with what it waits for, then "ready (…)" or the
   failure with the service's log tail;
6. "Setup: <command>" per setup command, with the services up;
7. "List tools <name>" per connection an agent uses, in the pod, so a stdio server can
   reach the services on `localhost`.

v0.1.0's "Create directory", "Fetch mirror", "Prepare attachments folder", "Reserve
memory", "Stop services" and "Remove directory" are gone: the steps that remain cover them,
and deleting the verify Run removes everything it made. A pod that waits for cluster capacity
shows it on the first step.

### 1.5 Panel

The Verify tab leads with the outcome: for a failure, the failed step's name, the
diagnosis, the command (for setup steps) and the log tail, then the other steps collapsed
with their states. For a pass, one line with the commit and the time, steps collapsed. A
running verification shows the current step. The list of past verifications stays below.

## 2. The setup skill

A Claude Code skill, `exradius-project`, in its own repository
(`musabeytekin/exradius-skill`), packaged as a plugin so developers add that repository
as a marketplace and install the skill without access to the Exradius source. The skill
states which `exradius.yaml` `version` it writes; a schema change in Exradius is followed
by a release of the skill, and this document names the skill repository so the two are
found together. A developer invokes it inside a checkout of their code (or an empty
folder, naming the repositories). It never calls the Exradius API, never asks for or
stores a secret value, and creates or updates nothing without showing the developer the
files first.

### 2.1 What it knows

`references/` holds, in the skill's own words (not links into Exradius):

- the `exradius.yaml` schema and rules (docs/config-repo.md §2), with the defaults;
- the run conditions a workstation must survive (runtime-bundles.md §2.3, §2.4; the
  operator's pod builder, docs/kubernetes.md §7): glibc image with bash and git, uid 1000
  named `runner` with home `/home/runner`, read-only root filesystem with `/work`, the home
  and `/tmp` writable, no capabilities, services reachable by name on the run's
  `localhost`;
- the pitfalls found so far, each with its fix: node images without corepack; uv older
  than the lockfile's syntax; `python3` not on PATH; tools that read the home from
  passwd; Alpine images; Maven caches that must be copied into the home in `setup`;
  parallel installs needing disk, not RAM;
- three worked examples: the bank, Outline and Saleor config repositories (the YAML and
  the Dockerfiles as they are on GitHub today, with one line on why each choice).

### 2.2 The flow

1. **Read the project.** Languages, package managers and the versions the lockfiles
   demand (`packageManager`, `uv.lock` revision, `.tool-versions`, `.nvmrc`,
   `.python-version`), services from docker-compose, CI config and env examples, test
   commands from README and CI, secret names from env examples. Decide whether the stock
   image is enough or a Dockerfile is needed.
2. **Ask only what cannot be inferred**, in one round: other repositories that belong to
   the project, the agent roles wanted (default: `developer` with the pull-request role,
   `scout` read-only), whether agents may push, the secret store name (default: the
   project slug), and where the config lives (§2.3).
3. **Write the files.** `exradius.yaml`, `docker/workstation.Dockerfile` when needed,
   service Dockerfiles only when a stock image cannot do, and `README.md` for the admin:
   the config repository URL and branch, whether it is private (so a host credential is
   needed), the store name, every required key with its format and where to obtain it.
   The README is the answer to "store keys have no format hint": the format lives with
   the project, not in Exradius.
4. **Check locally** with `scripts/check.sh` (§2.4). Fix and repeat until it passes.
5. **Create the repository and push.** With a tool that can create a remote (the `gh`
   CLI, or a GitHub or GitLab MCP server), create the config repository (private by
   default) and push. Without one, create the local repository with a first commit and
   print the commands to create the remote and push. Either way the final message is the
   admin hand-off: the URL, the store name and the keys.

One shot, then review: the skill proposes everything with a one-line reason per choice
and the developer edits; it is not an interview.

### 2.3 Where the config lives

Default: a separate config repository named `exradius-<slug>`. When the project has
exactly one repository, the skill offers `.exradius/exradius.yaml` inside it instead
(one pull request, no second repository); Exradius already reads that location.

### 2.4 The local check

`scripts/check.sh <config dir>` reproduces a verification with the Docker CLI alone, no
Exradius installed:

- parses `exradius.yaml` (a small Python helper, standard library only), substitutes every
  `${secret:NAME}` with one throwaway value per name so references stay consistent
  between a service's env and `env` (no real secrets are needed; code repositories are
  the developer's local checkouts, so no credential is needed either);
- builds `workstation.dockerfile` and every service Dockerfile from the declared
  contexts;
- checks the workstation image: glibc, `bash`, `git`;
- mirrors the run's pod (docs/kubernetes.md §7): one container holds the network
  namespace, with every service name mapped to `127.0.0.1` (`--add-host`, as the pod's
  `hostAliases`), and everything else joins it with `--network container:<it>`, so the
  services and the workstation share `localhost`;
- starts the services there, in order, with their image's user, the capabilities the
  operator gives them (`--cap-drop ALL --cap-add CHOWN,SETUID,SETGID,FOWNER,DAC_OVERRIDE,NET_BIND_SERVICE`,
  `no-new-privileges`), `command` as their arguments and declared `memory` as
  `--memory`, and waits for each one's `ready` check (`command` with `docker exec`, `tcp` by
  connecting to the shared address, or the image's HEALTHCHECK) with the operator's 120 s
  deadline from its start, printing its log tail when it fails;
- runs the setup commands in the workstation with the run's conditions: `--user
  1000:1000`, `--read-only`, `--cap-drop ALL`, `--security-opt no-new-privileges`,
  generated `/etc/passwd` and `/etc/group`, the repositories mounted at `/work`, a
  temporary home at `/home/runner` and a temporary `/tmp`, `HOME=/home/runner`, the
  project `env`, `resources.memory` as `--memory`, and each `workstation.cache` from a
  local cache directory;
- stops and removes everything, prints the first failing step with its output.

The flags mirror the operator's pod builder (`operator/internal/podspec/podspec.go` in the
Exradius repository); a comment in both places names the other, and changing one means
changing the other. The check does not cover the runtime bundle, providers, the egress
proxy and its allowlist (phase 4), or pushes, which only a real run exercises.

### 2.5 Update mode

With an `exradius.yaml` already present (in the folder or in `.exradius/`), the skill
reads it, diffs the code against it (new services in compose, tool versions the lockfiles
now need, new repositories, new secret names in env examples), proposes the edits, runs
the check and pushes to the config repository's branch. Exradius reads the branch head
on every run, so nothing else is needed.

### 2.6 Out of scope

A "Set up with AI" button in the panel (would need Docker inside a run container);
reading or writing secret values; calling the Exradius API from the skill.
