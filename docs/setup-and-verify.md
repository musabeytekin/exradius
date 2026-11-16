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

A Claude Code skill, `exradius-project`, in `skill/` of this repository, packaged as a
plugin: the repository root's `.claude-plugin/marketplace.json` makes the repository a
marketplace, so developers install the skill with
`claude plugin marketplace add musabeytekin/exradius` (it was `musabeytekin/exradius-skill`
until 2026-11-10). The skill
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
changing the other. The check does not cover the runtime bundle, providers or pushes,
which only a real run exercises. Since skill 0.4.0 it stands in for the egress proxy
(§2.7).

### 2.5 An existing config

With an `exradius.yaml` already present (in the folder or in `.exradius/`), the work is
the second skill's, `exradius-update` (§2.8). Until skill 0.6.0 this was a mode of
`exradius-project`: it read the project again, diffed the code against the file, proposed
the edits, ran the check and pushed. Exradius reads the branch head on every run, so a
push is all an update needs.

### 2.6 Out of scope

A "Set up with AI" button in the panel (would need Docker inside a run container);
reading or writing secret values; calling the Exradius API from the skill.

### 2.7 Skill 0.4.0: a setup an agent can work in

Added 2026-11-12. The skill had stopped at phase 3: it did not know `network.allow`, its
check refused the key, and it proved only what Verify proves. A config could pass both and
leave the agent unable to add a dependency or run a test. What changed:

- **The network.** `references/network.md` is the developer's account of
  docs/kubernetes.md §11: the two phases, what needs no entry, how to find the hosts
  (the project's registry configuration, then the check's own list), what no entry lifts
  (private addresses, anything but HTTP, SSH remotes), tools that ignore the proxy
  variables, private registries.
- **The check stands in for the proxy.** The pod's containers join a Docker network
  created with `--internal`, so nothing has a direct route out, as under NetworkPolicy. A
  proxy container on that network and on the default bridge runs the check's own script
  (standard library only): open in the setup phase, limited in the agent phase to
  `network.allow`, the connections' hosts (`network.allowed_hosts`) and `--allow` hosts
  (the install's `egress.allow`), refusing private addresses unless `--allow-private`
  (`egress.allowPrivate`). Every container gets the variables the operator writes
  (`proxyEnv`, `javaEnv`). Not mirrored: the reverse routes (the model, git's credential
  route, HTTP connections), so the repositories' hosts are open in the local agent phase
  and the Network step says when something used them; and image builds, which use
  Docker's network.
- **`--run '<command>'`** runs a command after setup in the workstation's conditions with
  the agent phase's network. The skill passes the test and lint commands it is about to
  write into `notes`, so `notes` hold only commands that worked in a run's conditions.
  Verify does not run tests; this is the only place they are tried before a task.
- **Connections.** The check fails when a stdio connection's command is not in the
  workstation image. Listing tools still needs the runtime bundle and stays with Verify.
- **Reading and writing.** CI is trusted over the README; registries, workspaces,
  generated code, submodules and LFS are read; the skill shows what it read, inferred and
  assumed. `notes` do not repeat the environment section; a role opens with the tickets
  its agent takes, because routing (agent-routing.md §3) reads it. `bridges/jira.yaml` is
  written when the developer names Jira spaces (config-repo.md §2.1).
- **A failed Verify or task** is its own entry point, with
  `references/verify-failures.md` by step and the rule to decide first whose problem it
  is: the config's, the store's or the install's.
- **Parity.** `backend/tests/test_skill_check.py` compares the check's validator, catalog
  and host list with `schemas/project_config.py`, `catalog.py` and `network.py`, and
  fails when a key of the schema is missing from its sample file, so a new key cannot be
  missed again.
- **Gone:** `--classic` and the classic-builder rules (v0.1.0), and the assumption that the
  cluster is `amd64`.

### 2.8 Skill 0.6.0: the update skill

Added 2026-11-14. A config describes the code as of the day it was written, and nothing
in Exradius notices when the code moves away from it: Verify proves that the workspace
can be prepared, not that the image has the Node version the lockfile now wants or that
`notes` name a script that still exists. The update mode of §2.5 had no memory of what
it had last looked at, so each update was a second setup. `exradius-update`
(`skill/skills/exradius-update/`) replaces it:

- **A baseline.** `check/reviewed.json` next to `exradius.yaml` holds, per repository,
  the commit the config was last checked against, and the skill release that wrote it.
  Both skills write it after a passing check (`changes.sh --record`: the newest commit
  that is both in the checkout's HEAD and in the config's branch). Exradius does not read
  the file, and it is outside the default build context (the Dockerfile's folder), so it
  rebuilds nothing. Without it the baseline is estimated: the last commit of the code
  before the config's own last commit; `--since` names another.
- **A report, not a reread.** `scripts/changes.sh` (standard library, git only) compares
  the baseline with the head of the config's branch (`origin/<branch>`) and lists only
  what the config depends on, grouped by the part of the config: paths the config itself
  names (setup, notes, roles, `check/start-app.sh`) that are gone or changed, tool
  versions with old and new value, package managers and workspaces, registries, compose
  services and images next to the config's own, env names, CI, scripts, the team's guide.
  It prints names and versions and never the content of an env file or a registry
  configuration. It imports the YAML reader and validator of `exradius_check.py`.
- **The check first.** Before any edit the skill runs the local check on the old config
  and the new code with the commands in `notes`: what fails is drift without argument,
  what passes is left alone.
- **A small diff, in both directions.** Stale lines are removed as well as new ones
  added; comments, order and wording stay.
- **Three sources.** The code (the report), Exradius and the skill (`--validate`, and
  what a file written by an earlier release lacks), and the runs (what the developer
  brings: a check an agent could not run, a refused host). The skill still never calls
  the Exradius API.
- **Publishing around keys.** Intake rejects a project's tasks while a referenced key is
  missing from the store (config-repo.md §7), so a change that references a new key goes
  out as a pull request and is merged after the admin added the key.

`backend/tests/test_skill_changes.py` runs the report on local git repositories.

### 2.9 Skill 0.7.0: proof from the running application

Added 2026-11-14. Since 0.5.0 a setup is done only when the application starts in a run
and `notes` say how, but the roles the skill wrote still ended at "say which checks ran".
A result for a ticket such as "add an export button" could say that the tests passed
without anyone, the agent included, having seen the button. What changed:

- **The role asks for proof.** The role of every agent that changes code says: when the
  change can be observed in the running application, start it as `notes` say, see the
  change there, and put what was seen into the result (a screenshot for a page, the
  request and response for an API, the command and output for a command line). It also
  names what needs no such proof (a refactor, a dependency update, tests,
  documentation), because every run pays for a start, and it forbids describing as seen
  what was not seen. This is role text only: the operating rules (architecture.md §13)
  already say "attach files that help", and the result's artifacts are the files an
  agent names from its working directory (`runner/tools.py`).
- **A browser by default where there are pages.** For an application with pages a
  browser shows, the agents that change code get the catalog's `playwright` connection,
  with Chromium's libraries in the workstation Dockerfile, the browser as a setup
  command and `~/.cache/ms-playwright` in `workstation.cache`. Until now a connection
  other than `github` or `gitlab` was written only when the developer asked; this one is
  an assumption shown with its cost, which the developer can take out. The browser tool
  saves a screenshot under a relative file name in the session's working directory,
  which is where artifacts are read from; the role tells the agent to keep such files
  out of the repositories.
- **The check proves the browser.** `check/start-app.sh` ends with a screenshot taken by
  the Playwright the catalog pins. Verify's "List tools" starts no browser, so this is
  the only place it is tried before a task.
- **The update skill** proposes the role's sentences, and the connection, for a config
  written by an earlier release.

Not changed: Exradius itself. Whether a result's artifacts reach the person is the
bridge's business (integration-api.md).

### 2.10 Skill 0.8.0: agents and questions

Added 2026-11-17. Two things people do with a project that is set up had no place: asking
what it is, and giving it another agent.

- **`exradius-info`** answers from the file and changes nothing. `scripts/describe.sh`
  (standard library, the check's YAML reader, validator and catalog) prints the config as
  Exradius reads it: per agent the model, provider, git author, tools and the opening of
  the role; connections with transport, keys, hosts and the agents that list them;
  repositories, workstation, services, variable names, the agent phase's hosts
  (`allowed_hosts`), every key with the places that reference it. It never prints a
  value. The skill sorts each answer into one of three kinds: in the file, a default
  Exradius applies, or decided by the install. For the last it names the page of the
  panel and does not guess: a model the file leaves out is `None` in the run spec, the
  runtime's default on the resolved provider, and the Providers page's test result names
  the model that answered. The skill still never calls the Exradius API.
- **`exradius-agent`** adds, changes or removes an agent. Its work is wording, because
  nothing local proves a role: it first decides with the developer whether the request
  is an agent at all (or a line in `notes`, a sentence in a role, a `model`), then writes
  the role in the setup skill's order and makes the roles divide the tickets, since
  routing reads each agent's name, role and tools (agent-routing.md §3). It states what a
  role cannot hold: repositories, `push`, env and network belong to the project, so only
  connections and their tokens separate agents. Publishing waits for the admin in two
  cases: a new key (intake rejects tasks while a referenced key is missing), and a
  project's second agent (agent-routing.md §6: without an OpenAI key a task that names no
  agent is rejected once the project has two).

`backend/tests/test_skill_describe.py` runs the description on sample configs and checks
that every path a `SKILL.md` names under `${CLAUDE_SKILL_DIR}` exists.
