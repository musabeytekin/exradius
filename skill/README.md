# The Exradius project skills

A Claude Code plugin with four skills for [Exradius](#what-exradius-needs-from-a-project),
the self-hosted harness that runs AI agents on a team's repositories:

| skill | for |
|---|---|
| `exradius-project` | putting a codebase on Exradius: the config, proven locally (below) |
| `exradius-update` | a config whose code has moved on ([Keeping a config current](#keeping-a-config-current)) |
| `exradius-agent` | a new agent, or another model, tool or role for one ([Agents](#agents)) |
| `exradius-info` | questions: what a project or an agent runs with ([Asking about a project](#asking-about-a-project)) |

On the developer's machine `exradius-project`:

1. reads the code (languages, tool versions the lockfiles demand, package registries,
   services from compose and CI, the checks as CI runs them, how the application is
   started in development, secret names from tracked env examples, and what the team's
   own guide says to agents), trusting CI over the README where they disagree;
2. asks the few things it cannot infer, in one round (other repositories, whether agents
   push and open pull requests, where the config lives, where tickets come from, and
   anything in the team's guide that forbids or restricts agents), and states the rest
   as assumptions;
3. writes `exradius.yaml` (config **version 1**) with notes and agent roles an agent can
   work from, `docker/workstation.Dockerfile` when the stock image is not enough, a
   bridge file when the team's tracker needs one, and a `README.md` for the Exradius admin
   listing every secret key with its format and where to get it, and what only the
   install can provide;
4. checks it locally with Docker: the steps of Exradius's Verify, then the project's own
   test and lint commands and the start of the application as an agent would run them,
   with the agent's network, and measures the memory each step needs;
5. creates the config repository (private by default) with `gh` or a git-host MCP server,
   or prints the commands, and ends with the hand-off for the admin.

It never asks for or reads a secret value and never calls the Exradius API. With a failed
Verify step or task and its log, it finds whose problem it is (the config's, the store's
or the install's), reproduces it locally and fixes what is the config's.

## Three limits to know first

Every Exradius run, and so every project, lives with three limits:

1. **No Docker daemon inside a run.** Tests that start containers themselves
   (Testcontainers, `docker compose` in a test script) cannot run there. The skill models
   what they start as `services` next to the agent and says which tests stay for CI.
2. **A read-only root filesystem.** Nothing can be installed at run time outside the
   home, `/work` and `/tmp`: whatever a run needs besides the project's own dependencies
   goes into the workstation Dockerfile.
3. **The agent's network is an allowlist.** Setup reaches any public host; the agent
   reaches its model, the project's repositories, its connections and the hosts the
   config lists in `network.allow`. The skill lists the package registries setup used, so
   an agent can add a dependency, and the check shows which hosts were reached.

## Install

In Claude Code:

```
/plugin marketplace add musabeytekin/exradius
/plugin install exradius@exradius
```

Or from a shell:

```bash
claude plugin marketplace add musabeytekin/exradius
```

```bash
claude plugin install exradius@exradius
```

Update later with `/plugin marketplace update exradius`. To try a local clone
without installing: `claude --plugin-dir /path/to/exradius/skill`.

## Use

In a checkout of the project's code (or an empty folder, naming the repositories):

```
/exradius:exradius-project
```

Claude also picks the skill up from a plain request such as "set this repository up for
Exradius" or "Verify failed on the build step, here is the log". With an `exradius.yaml`
already present, the work is `exradius-update`'s.

Requirements on the developer's machine: Docker (Desktop or Engine) running, git, Python
3.8 or later (standard library only), and optionally the `gh` CLI to create the
repository. The check pulls `python:3.13-alpine` once, for its proxy (`--proxy-image`
names another image with Python).

## Keeping a config current

A config describes the code as it was when the file was written. In the config folder, or
in a checkout of the code with the config next to it:

```
/exradius:exradius-update
```

or a plain request such as "we moved to Node 22 and added Meilisearch, update the
Exradius config" or "is our exradius.yaml still current?". The skill:

1. finds the config and a checkout of every repository it lists, and runs `changes.sh`:
   for each repository, what changed between the commit the config was last compared
   with and the head of its branch, limited to what the config depends on;
2. validates the file against this release and goes through what a file written by an
   earlier release may lack, and takes what the developer brings from the runs (a check
   an agent could not run, a refused host);
3. runs the local check on the config as it is and the code as it is now, with the
   commands `notes` give: what fails is drift nobody has to argue about;
4. edits the config as a small diff (additions and removals, comments and wording kept),
   asks only what the code cannot answer, and reruns the check until it passes;
5. records the commits the config now matches in `check/reviewed.json`, shows the diff
   and publishes with the developer's go-ahead: a change that references a new secret
   key goes out as a pull request, merged after the admin added the key, because
   Exradius rejects a project's tasks while a referenced key is missing.

`skills/exradius-update/scripts/changes.sh` runs without Claude and needs only git and
Python:

```bash
skills/exradius-update/scripts/changes.sh --fetch ../exradius-myapp
```

| group | what it lists | the part of the config |
|---|---|---|
| Named by the config | paths that setup, notes, a role or `check/start-app.sh` name and that are gone or changed | the command or sentence that names them |
| Tool versions | version files, `packageManager` and `engines`, `go`, `requires-python`, lockfile formats, wrappers: old and new value | the workstation Dockerfile or image |
| Package managers and workspaces | lockfiles and manifests added or deleted, workspace files | setup, `workstation.cache`, `network.allow`, notes |
| Registries | registry configuration files (never their content) | `network.allow`, secret names, the README |
| Services and images | compose services added, removed or on another image, next to the config's own; devcontainer and Dockerfile changes | `services`, the Dockerfile's system packages, `env` |
| Environment | names added to and removed from tracked env files (never values) | `env`, `${secret:...}` names, the README's keys |
| CI | workflow files, with the `*-version` and `image` values that came and went | setup, the commands in notes, services |
| Scripts and task runners | `package.json` scripts, Makefile, justfile, Procfile, entry scripts | setup, notes |
| The team's guide and delivery | `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, the pull request template, hooks | notes, roles, a question to the developer |
| Clone, New top-level folders, Dependencies | `.gitmodules` and LFS patterns; new folders at the root; manifests that changed | setup, notes, the Dockerfile |

The comparison starts at the commit in `check/reviewed.json` (`--record` writes it; both
skills do after a passing check). For a config from before 0.6.0 it is estimated from the
config's own last commit, and `--since NAME=REV` or `--since NAME=YYYY-MM-DD` names
another start. `--exit-code` makes the exit status 1 when something was found, for a
scheduled job that only wants to know whether a config needs a look.

## Agents

```
/exradius:exradius-agent a reviewer that comments on pull requests and never pushes
```

or a plain request such as "add an agent that triages new issues" or "have the scout use
the smaller model". For a new agent the skill:

1. reads the agents and connections the project has, their roles in full, and `notes`;
2. says when the request is not an agent: a rule for everyone belongs in `notes`, a
   refinement in an existing role, another model for the same work in `model`;
3. asks in one round what only the developer knows (which tickets it takes, with
   examples, and what it must never do; whether it changes code; the tools it needs) and
   proposes the rest;
4. writes the block: a slug that names the job, a role in the order routing and the
   agent need (the tickets it takes, when its work is done, proof and delivery when it
   changes code), and only the connections it uses, each secret as a key name;
5. makes the roles divide the tickets: Exradius routes a task that names no agent by
   reading every agent's name, role and tools, so the skill sorts real tickets by the
   first sentences alone and narrows a sentence where two agents could take the same;
6. says what a role cannot hold: every agent of a project has the same repositories,
   `push` setting, environment and network, and only connections and their tokens differ;
7. validates the file (the Docker check only when a connection's launcher, the image or
   a command is new) and publishes with the go-ahead: after the admin added a new key,
   and, when a project goes from one agent to two, after the admin confirmed that agent
   routing is set up, because tasks that name no agent are rejected without it.

Changing a model, a role or a connection, and removing or renaming an agent, are the same
skill.

## Asking about a project

```
/exradius:exradius-info which model does the scout use?
```

or the question alone, in a folder with the config. The skill changes nothing. It runs
`describe.sh`, which prints the file as Exradius reads it with the defaults filled in,
and answers with the key the answer rests on. Each answer is one of three kinds: in the
file; a default Exradius applies (the git address `<agent>@<slug>.exradius.local`); or
decided by the install, with the panel page that shows it (a model the file does not
set is the runtime's default on the install's default provider, and the skill does not
guess its name).

`skills/exradius-info/scripts/describe.sh` runs without Claude:

```bash
skills/exradius-info/scripts/describe.sh --agent scout ../exradius-myapp
```

It lists the agents (model, provider, git author, tools, how the role opens), the
connections (how each runs, its keys and hosts, the agents that list it), the
repositories, the workstation and its setup, the services, the variable names, the hosts
the agent may reach, and every secret key with the places that reference it. It never
prints a value.

## The local check on its own

`skills/exradius-project/scripts/check.sh` runs without Claude:

```bash
skills/exradius-project/scripts/check.sh --repo myapp=../myapp \
  --run 'cd myapp && npm test -- src/health.test.ts' \
  --run-file ../exradius-myapp/check/start-app.sh ../exradius-myapp
```

Steps, in Verify's order:

| step | what it does |
|---|---|
| Read config | parses `exradius.yaml` (or `.exradius/exradius.yaml`) with the same strict rules as Exradius, lists the required secret keys, warns about uncommitted or git-ignored files the server would not see |
| Build workstation / Build service *name* | `docker build` of each Dockerfile from its declared context with BuildKit, as Exradius builds in the cluster; `image:` entries are pulled when missing |
| Check workstation image | glibc (no Alpine/musl), `bash`, `git`, and the tools found on PATH, as uid 1000 on a read-only root |
| Check connections | each stdio connection an agent lists has its command (`npx`, `uvx`, a custom one) in the workstation image, as Verify's "List tools" needs |
| Start egress proxy | a Docker network without a route out, and a small proxy on it: the stand-in for Exradius's egress proxy, open during setup, limited to the agent's hosts afterwards (`--open-network` skips it) |
| Prepare /work | Docker volumes for `/work`, the home and `/tmp` (a Linux filesystem, as a run's disk), and in `/work` a fresh clone of each local checkout (`--repo NAME=PATH`, `--repos-dir DIR`, or the config's own repository for `.exradius/`), else a clone from the URL; `--live` mounts the checkouts as they are |
| Start service *name* | in the run's network namespace, as a sidecar of the run's pod: a holder container owns it, with every service name as `127.0.0.1` in its `/etc/hosts`, and each service joins it (`--network container:`), with the operator's capabilities, its `command` and `memory`, and its `ready` check (`command`, `tcp` against the pod's own address, image HEALTHCHECK, or running), 120 s from its start |
| Setup: *command* | `/bin/sh -c` in the workstation with `--user 1000:1000 --read-only --cap-drop ALL --security-opt no-new-privileges`, generated `/etc/passwd`/`/etc/group` (`runner`, home `/home/runner`), `/work`, home and `/tmp` writable, `HOME=/home/runner`, the project `env` with one throwaway value per `${secret:NAME}`, the proxy variables Exradius sets (`HTTPS_PROXY`, `YARN_HTTPS_PROXY`, `JAVA_TOOL_OPTIONS`, ...), in the same network namespace (services on `localhost` by name); `resources.memory` as `--memory` and `--memory-swap`; each `workstation.cache` folder mounted from `~/.cache/exradius-check/<slug>/`; the step's peak memory is printed |
| Agent phase: *command* | each `--run` command and `--run-file` script, after setup, in the same workstation conditions, with the network limited to `network.allow`, the connections' hosts and `--allow` hosts: the project's test and lint commands and the start of the application, as an agent would run them. All of them run, also after one failed |
| Memory, Disk | the highest peak of any step against `resources.memory`, and what the run left on its disk |
| Network | from the proxy's records: `reached in setup`, `reached in setup only` (what the agent cannot reach), `refused in the agent phase`, repository hosts something other than git may have used, and private addresses the admin's install must allow |
| Clean up | removes the containers, the network and the volumes (`--keep` keeps the volumes), also when the check is interrupted; built images and the cache folders stay |

Exit status 0 when every step passed, 1 with each failing step and its output
otherwise, 2 for usage errors. `--validate` only reads the file (no Docker),
`--platform linux/amd64` (or `arm64`) checks the cluster's architecture when it differs
from the machine's, `--allow HOST` and `--allow-private HOST` stand for what the admin's
install allows (`egress.allow`, `egress.allowPrivate`), `--cache-dir DIR` keeps the cache
folders elsewhere (delete the folder to start cold), `--log PATH` names the full log,
`-v` streams output, `--help` lists the rest.

Not covered locally: the agent runtime and the model, pushes and git's credential route,
the connections' tool lists, and image builds' network (builds use Docker's own). A Verify
in Exradius covers those; neither runs the tests, which is what `--run` is for.

## What Exradius needs from a project

The skill's references are the developer documentation for Exradius projects:

- [`references/schema.md`](skills/exradius-project/references/schema.md): every key of
  `exradius.yaml` version 1, its default, and the rules;
- [`references/run-conditions.md`](skills/exradius-project/references/run-conditions.md):
  what a workstation image must survive;
- [`references/network.md`](skills/exradius-project/references/network.md): what a run
  reaches in each phase, how to write `network.allow`, private registries, the proxy;
- [`references/stacks.md`](skills/exradius-project/references/stacks.md): per language
  and service, the image and tool choices that worked;
- [`references/running-the-app.md`](skills/exradius-project/references/running-the-app.md):
  what an application needs to start in a run, how to prove it does, and what an agent
  shows as proof of a change;
- [`references/pitfalls.md`](skills/exradius-project/references/pitfalls.md): failures
  seen on real projects and their fixes;
- [`references/verify-failures.md`](skills/exradius-project/references/verify-failures.md):
  a failed Verify step or task, by step: cause, whose it is, the fix;
- [`references/examples.md`](skills/exradius-project/references/examples.md): four worked
  examples (Bank of Anthos, Outline, Saleor, Plane);
- [`references/admin-readme.md`](skills/exradius-project/references/admin-readme.md): the
  README the admin receives.

## Versions

| skill (plugin) | writes `exradius.yaml` |
|---|---|
| 0.8.0 | version 1; `exradius-agent` and `exradius-info` |
| 0.7.0 | version 1; roles that ask for proof from the running application, and a browser for projects with pages |
| 0.6.0 | version 1; `exradius-update` and `check/reviewed.json`, the commits a config was checked against |
| 0.5.0 | version 1; a setup in which the application starts, proven locally, with measured memory |
| 0.4.0 | version 1, with `network.allow`; checked with the run's network and the project's own check commands |
| 0.3.0 | version 1, checked the way Exradius 0.2.0 (on Kubernetes) runs it |
| 0.2.0 | version 1, with `resources`, service `memory` and `command`, `workstation.cache` |
| 0.1.0 | version 1 |

A change of the file's schema in Exradius is followed by a release of this plugin.

### 0.8.0

- **`exradius-agent`**: add, change or remove an agent of a project that is set up.
  Until now a third agent was two sentences in the setup skill. The new skill decides
  with the developer whether the work needs an agent, writes the role, checks that the
  roles of all agents divide the tickets (routing reads their first sentences), keeps
  the agent's connections to what it uses, and orders the publishing around a new key
  and around the first time a project has more than one agent.
- **`exradius-info`**: answers what a project or an agent runs with, read-only.
  `scripts/describe.sh` prints the config with its defaults filled in and marks what the
  install decides; the skill says which page of the panel shows that.

### 0.7.0

Passing tests do not show the person who asked for a button that it is on the page.

- **Proof from the running application.** The role of every agent that changes code now
  tells it, when the change can be observed in the running application, to start it, see
  the change there and put what it saw into the result: a screenshot for a page, the
  request and the response for an API, the command and its output for a command line. A
  refactor, a dependency update, tests or documentation need none of it, and the role
  says so; an agent that could not start the application says what stopped it
  (`SKILL.md`, "Roles"; `references/running-the-app.md`, "Proof of a change").
- **A browser by default for a project with pages.** The agents that change code get the
  `playwright` connection, with Chromium's libraries in the Dockerfile, the browser in
  setup and its folder in `cache`. It is shown to the developer as an assumption with
  its cost. `check/start-app.sh` ends with a screenshot taken by the pinned Playwright,
  the only proof before a task that the browser starts under a run's conditions.
- `notes` say how a change is looked at (the URL for the browser tool, or that there is
  no browser); the four examples' developer roles carry the new sentences in the form
  without a browser.
- `exradius-update` proposes both for a config written by an earlier release.

### 0.6.0

- **`exradius-update`**, a second skill, for a project that is set up and whose code has
  moved on. The update mode of `exradius-project` read the whole project again and
  compared from memory; the new skill starts from a report of what changed
  (`scripts/changes.sh`), lets the check show what broke before anything is edited,
  removes what went stale as well as adding what is new, and orders the publishing
  around new secret keys. `references/what-changes-mean.md` says, per kind of change,
  what to read, what it can change and what proves it.
- **`check/reviewed.json`** in the config repository: the commit of each repository the
  config was last checked against, and the skill release. `exradius-project` writes it
  after its last passing check (`scripts/record.sh`), `exradius-update` after each
  update. Exradius does not read it.
- `exradius-project` no longer has an update mode: with a config present it hands over
  to `exradius-update`.

### 0.5.0

From trials of 0.4.0 on thirteen open source projects (Rust, Java, Python, Go, Ruby, PHP,
Elixir, .NET, Node monorepos): every setup passed its checks, two applications could not
start, and no project's notes said how to start it.

- **Running the application.** The skill reads how developers start the app, puts what
  only its start needs into `services`, the Dockerfile and setup, proves the start with
  the check (`--run-file check/start-app.sh`) and writes the commands, the port and the
  login into `notes` (`references/running-the-app.md`). A test mode no longer goes into
  `env`.
- **The team's own rules.** A guide that forbids AI-written changes, a mirror where pull
  requests are closed, or a delivery procedure that contradicts the chosen one becomes a
  question to the developer instead of being worked around.
- **`references/stacks.md`**: image and tool choices per language (Rust, Go, Ruby, PHP,
  .NET, Elixir, Bun, pip, a second runtime) and per service, and cache and host rows for
  them in `pitfalls.md` and `network.md`.
- **Corrections.** pnpm 12 is installed with npm (the corepack recipe left a wrapper that
  downloads at first use); Gradle's wrapper is fetched in setup instead of allowing a host
  that redirects to GitHub; Yarn 1 and Composer hosts come from the lockfile; an
  entrypoint is tried before it is assumed to run a shell command; Redis 8's image needs
  `SKIP_DROP_PRIVS`; a `ready` command must not be an HTTP client that asks the proxy.
- **The proxy variables and the project's own tests**: the run's own address is not in
  `NO_PROXY`, lower-case variables override a test's own, Node's `fetch` needs
  `NODE_USE_ENV_PROXY`; each with its symptom and the command prefix for `notes`.
- **Memory** is measured, not guessed: one rule (the check's peak plus about 1 GB, at
  least `4g`) and the flags that bound each tool's parallelism.
- No secret store, and a role without a delivery sentence, for projects that need neither.
- **The check**: `/work`, the home and `/tmp` are Docker volumes, so Unix sockets work as
  in a run (on macOS they failed on the mounted folders); every step's peak memory, a
  Memory and a Disk line; a process killed for memory inside a step is named; every
  `--run` runs, also after a failure; `--run-file`; a passing command's last lines are
  shown; failure output shows the lines that say why; proxy errors and the run's own
  address are named in the Network lists; the cache folders moved to
  `~/.cache/exradius-check/<slug>`; the full log has a stable place; an interrupted check
  cleans up.

### 0.4.1

- The `playwright` connection: the catalog starts it with Chromium and no longer lists the
  browser's download hosts, so the references say what a project adds for an agent that
  looks at the app (Chromium's libraries in the Dockerfile, the browser in setup) and
  `network.md` no longer suggests those hosts for `network.allow`.
- The catalog has no `postgres` entry any more, and `gitlab`, `slack` and `filesystem` run
  other pinned packages with the same settings: `schema.md` shows a read-only database
  connection written as a custom one.

### 0.4.0

For the egress proxy, agent routing and bridges, and for a setup an agent can work in,
not only one that passes Verify:

- `network.allow`: the schema reference, the validator and the examples know the key. The
  skill reads the project's registry configuration and writes the hosts the agent needs
  (the package registries, so it can add a dependency), and `references/network.md` says
  what needs no entry, what no entry lifts (private addresses, anything but HTTP, SSH
  remotes) and what to do about a tool that ignores the proxy variables.
- The check runs without a direct route to the network, through a local stand-in for the
  egress proxy with the variables Exradius sets, open during setup and limited afterwards.
  `--run '<command>'` runs a command after setup as the agent would, so the test and lint
  commands in `notes` are proven before they are written. The Network step lists the
  hosts reached and refused. `--allow` and `--allow-private` stand for the install's own
  lists; `--open-network` restores the old behaviour.
- The check fails when a connection's launcher (`npx`, `uvx`) is not in the workstation
  image, as Verify's "List tools" would.
- Reading the project: CI before the README, registries, monorepo workspaces, generated
  code, submodules and LFS, what cannot run in a run. The skill shows what it read,
  inferred and assumed.
- `notes` and roles: what the agent already knows from Exradius is not repeated; notes
  give each area's check commands in their narrow and full form; a role opens with the
  tickets its agent takes, which is what agent routing reads.
- `bridges/jira.yaml` when the team's Jira spaces belong to the project.
- A failed Verify step or task: `references/verify-failures.md`, by step, with whose
  problem it is.
- The admin README gains the network line, an "Ask the admin" section (the cluster's
  architecture, private addresses, pull secrets, the bridge) and where the caches are now
  (the project's volume).
- Gone: `--classic` and the rules for v0.1.0's classic builder (heredocs and cache mounts
  work in Exradius's builds); the assumption that the cluster is `amd64`.
- `backend/tests/test_skill_check.py` in the Exradius repository holds the check's
  validator to the backend's schema, so a new key cannot be missed again.

### 0.3.0

For Exradius 0.2.0, which runs each task as a Kubernetes pod (no schema change):

- The check mirrors the run's pod: the services and the setup commands share one network
  namespace, so services are reached by name on `localhost`, and a `tcp` check connects to
  the run's own address (a service that listens only on 127.0.0.1 fails it, as in
  Exradius). Services keep 120 s from their start.
- The file check refuses a port that two services list, the service name `localhost`, and
  service names longer than 55 characters, as Exradius does.
- Images are built with BuildKit, as Exradius does in the cluster.
- Memory is placement, not a reservation: a declared size is the pod's request and limit,
  and a run waits as "waiting for cluster capacity" until a node has room. The check warns
  when the declared total is more than the Docker host has.
- Caches live on the project's volume in Exradius; the panel no longer shows their size.

### 0.2.0

For the run environment keys Exradius added to version 1 (all optional; a file without
them means what it meant before):

- `resources.memory` and a service's `memory`: a Docker size (`6g`, `512m`) that Exradius
  reserves before starting a run and enforces as the container's limit. The skill
  estimates the workstation's peak and writes it; the check applies it as `--memory` and
  `--memory-swap`, reports a step that went over it with Verify's wording, and warns
  when the total is more than the Docker host's memory minus 1 GiB (Exradius's default
  budget).
- `workstation.cache`: download stores kept per project across runs (pnpm, Yarn, npm, uv,
  pip, Maven, Gradle, Go, Cargo; the paths are in `references/pitfalls.md`). The skill adds
  one per package manager it finds; the check mounts them from `.exradius-cache/`, in
  folders named as on the server (`0-local_share_pnpm_store`).
- A service's `command`: the container command, the image's entrypoint kept. The skill
  prefers it to a service Dockerfile; Plane's MinIO bucket bootstrap is now a stock image
  with a `command`.
- The run conditions now open with the two limits above, and note that hard links do not
  cross bind mounts (pnpm and uv copy from their caches).
- The examples gain Plane and the new keys. The Bank of Anthos example's Maven pre-fetch
  now lands in `~/.m2/repository`, where Maven looks (it used to be copied to `~/.m2`).

## Layout

```
.claude-plugin/plugin.json        the plugin (name: exradius)
../.claude-plugin/marketplace.json the repository root as a marketplace (name: exradius)
skills/exradius-project/
  SKILL.md                        the flow
  references/                     schema, run conditions, network, stacks, running the app,
                                  pitfalls, Verify failures, examples, admin README
  scripts/check.sh                the local check (wraps exradius_check.py)
  scripts/exradius_check.py       standard-library Python: YAML reader, validator, Docker steps,
                                  the local egress proxy
  scripts/record.sh               writes check/reviewed.json (wraps the update skill's changes.sh)
skills/exradius-update/
  SKILL.md                        the flow
  references/what-changes-mean.md per kind of change: what to read, what it changes, the proof
  scripts/changes.sh              the change report and --record (wraps exradius_changes.py)
  scripts/exradius_changes.py     standard-library Python: git comparison, classification,
                                  the record; uses exradius_check.py's YAML reader and validator
  scripts/check.sh                the local check under this skill's path
skills/exradius-agent/
  SKILL.md                        the flow
  scripts/describe.sh, check.sh   the info skill's description and the local check under this
                                  skill's path
skills/exradius-info/
  SKILL.md                        where each answer is
  scripts/describe.sh             the config as Exradius reads it (wraps exradius_describe.py)
  scripts/exradius_describe.py    standard-library Python; uses exradius_check.py's YAML reader,
                                  validator and catalog
```
