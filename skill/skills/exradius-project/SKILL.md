---
name: exradius-project
description: Set up or update a project for Exradius, the self-hosted harness that runs AI agents on a team's code. Reads the code, writes the project's exradius.yaml (config version 1), a workstation Dockerfile when the stock image is not enough, and a README for the Exradius admin, proves locally with Docker that the setup and the project's own test commands work under a run's conditions, and creates the config repository. Use when someone wants to put a codebase on Exradius, write or fix an exradius.yaml, or understand why Verify or a task failed.
argument-hint: "[code checkout paths or repository URLs; default: the current folder]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/check.sh *), Bash(bash ${CLAUDE_SKILL_DIR}/scripts/check.sh *)
---

# Set up an Exradius project

Exradius runs agents (Claude Code) on a team's repositories, each task in its own
Kubernetes pod. A project is one file, `exradius.yaml`, in a git repository: the code
repositories, the workstation image the agents work in, the services next to it
(Postgres, Redis, ...), environment, what the agent may reach on the network, notes, MCP
connections, the agents, and the name of a **secret store** the Exradius admin fills. The
admin pastes the config repository's URL and fills the store; the developers own
everything else. You do the developers' part, on their machine, where a mistake costs
seconds instead of a commit, a push and a Verify in the cluster.

The file is good when an agent, on its first ticket, is where a new engineer is after a
well-run first day: the tools are installed at the right versions, the dependencies are
there, the services answer, it knows which command checks its work and that command
works, and it knows how the team wants changes delivered. Everything below serves that.
A file that only passes Verify is not done: Verify proves the workspace can be prepared,
not that anyone can work in it.

This skill writes `exradius.yaml` **version 1**. If a file says another version, stop and
say this skill release does not know it.

## What to read, and when

The references are part of the skill; read them rather than guess, at the point where
each is needed.

| File | Read it |
|---|---|
| `${CLAUDE_SKILL_DIR}/references/schema.md` | before writing or changing `exradius.yaml`: every key, its default, the rules, bridge files |
| `${CLAUDE_SKILL_DIR}/references/run-conditions.md` | before choosing the workstation: what an image must survive (uid 1000 named `runner`, read-only root, no Docker daemon, an allowlisted network) |
| `${CLAUDE_SKILL_DIR}/references/network.md` | before writing `network.allow`, and whenever a registry, a mirror or a proxy is involved |
| `${CLAUDE_SKILL_DIR}/references/pitfalls.md` | before writing a Dockerfile, and when a check step fails |
| `${CLAUDE_SKILL_DIR}/references/examples.md` | the one example closest to the project's stack (its first table says which) |
| `${CLAUDE_SKILL_DIR}/references/admin-readme.md` | when writing the README |
| `${CLAUDE_SKILL_DIR}/references/verify-failures.md` | when the developer brings a failed Verify or task |

`${CLAUDE_SKILL_DIR}/scripts/check.sh` is the local check. It needs the Docker CLI with a
running daemon, git and Python 3.8 or later, and pulls one small image for its proxy
(`python:3.13-alpine`) the first time.

## Hard rules

- **Never ask for, read, print or write a secret value.** The file references secrets as
  `${secret:NAME}`; the admin puts values into the store. Read only env files that git
  tracks (`git ls-files '*.env*' '.env*'`): `.env.example`, `.env.sample`, `.env.test`.
  Never open an untracked `.env`, `.env.local` or similar, credentials files, `~/.npmrc`,
  or the developer's shell history. If a value in a tracked file looks like a real secret,
  use its name only and tell the developer.
- **Never call the Exradius API** or the admin panel, and never ask for an Exradius URL or
  login. The skill's output is files and a git repository.
- **Show before you publish.** Writing files in a new local folder is fine; creating a
  remote repository, pushing, or opening a pull request happens only after the developer
  has seen the files and said yes. Private by default.
- **One shot, then review.** Do the reading yourself, ask the few questions of step 2 in a
  single round, then write everything with a one-line reason per choice. Do not
  interview.
- **Claim only what the check showed.** A command goes into `notes` after it ran under the
  run's conditions. What was not run is listed as not run.

## Which situation

- An `exradius.yaml` (or `.exradius/exradius.yaml`) exists in the current folder, in a
  sibling `../exradius-*` folder, or where the developer points: **Update mode** below.
- The developer brings a failed Verify step, a failed task or a log: **A failed Verify or
  task** below.
- Otherwise: a new project, steps 1 to 6.

## 1. Read the project

Read the code before asking anything. For each code repository (the current checkout,
plus any the developer named), find the following, and keep for each finding where it
came from: the summary in step 5 shows the developer what was read, what was inferred
and what was assumed.

Where sources disagree, trust them in this order: **CI workflows** (they start from a
clean clone, as a run does), then lockfiles and version files, then Makefile and package
scripts, then README and CONTRIBUTING (they describe a machine that was set up long ago).

- **Shape.** One application or several; a monorepo's workspaces (`pnpm-workspace.yaml`,
  `turbo.json`, `nx.json`, `go.work`, Cargo and Maven modules) and which of them have
  their own checks. Submodules (`.gitmodules`) and LFS (`filter=lfs` in
  `.gitattributes`): the clone fetches neither (pitfalls.md).
- **Languages and the tool versions the lockfiles demand**: `package.json`
  (`packageManager`, `engines`), `.nvmrc`, `.node-version`, `.tool-versions`, `mise.toml`,
  `.python-version`, `pyproject.toml` (`requires-python`, `[tool.uv]` `required-version`,
  `exclude-newer` and other syntax that needs a recent uv), the header of `uv.lock`,
  `poetry.lock`, `pnpm-lock.yaml` and `package-lock.json` `lockfileVersion`, `yarn.lock`
  with `.yarnrc.yml`, `go.mod` (`go`, `toolchain`), `rust-toolchain.toml`, `Gemfile.lock`
  (`BUNDLED WITH`), `pom.xml` / the Gradle wrapper and the Java release, `global.json`.
  Note the developer's own tool versions (`uv --version`, `node --version`, ...) where a
  lockfile was written by a newer tool than it records.
- **Package managers and where they download from.** Every package manager a setup
  command will run, and its registry: the default, or what `.npmrc`, `.yarnrc.yml`,
  `pip.conf`, `[[tool.uv.index]]`, `settings.xml`, Gradle `repositories {}`, `GOPROXY`,
  `.cargo/config.toml` or `NuGet.config` name. A registry that needs a token is a secret
  name; one on a private address needs the admin (network.md). These decide
  `workstation.cache`, `network.allow` and part of the README.
- **Services**: `docker-compose*.yml`, `compose*.yaml`, `.devcontainer/`, CI service
  containers (`.github/workflows/*`, `.gitlab-ci.yml`, `.circleci/`): image, version,
  `command`, env, ports, the host names the app expects. Anything the app reaches outside
  the machine that is not HTTP (a shared database, a broker) has to become a service too.
- **The checks, as CI runs them.** For each repository or workspace: the commands that
  install, generate code, build what the checks need, test, lint, type-check and format,
  with the narrow form of each (one file, one package, one test name) and the flags that
  matter in a container (a single worker instead of all cores, a CI reporter). These
  become setup commands, `notes`, and the `--run` commands of step 4.
- **What cannot run in a run.** Tests that start containers (Testcontainers,
  `docker compose` in a test script, kind): there is no Docker daemon. End-to-end suites
  that need a browser. Anything that needs a cloud account. Model what such tests start as
  `services` where that makes them runnable; the rest stays for CI and `notes` say so.
- **Environment**: tracked env examples and test env files (what the app reads, what is
  already supplied for tests, what points at `localhost` and must point at a service),
  CI env blocks. Secret names come from here: anything that is a password, token or key
  and is not a throwaway test value.
- **Git remotes**: each repository's URL (as `https://`, whatever the local remote uses),
  default branch, host, whether it is private (`gh repo view --json visibility` when `gh`
  is available), and the team's delivery habits when the repository shows them (pull
  request template, branch protection, CODEOWNERS, commit conventions).
- **The team's own guide for agents and newcomers**: `AGENTS.md`, `CLAUDE.md`,
  `CONTRIBUTING.md`. `notes` point at it instead of copying it; it travels with the clone.
- **Memory**: the heaviest step (a large JavaScript install or type check, a Java build,
  pytest-xdist workers), for `resources.memory` and the README.

Then decide the workstation: the stock image of the main language is enough only when it
has every tool the setup commands, tests and linters call at the right version, plus bash
and git, and is glibc-based. Usually a short Dockerfile is needed (examples.md).

## 2. Ask what cannot be inferred, in one round

Ask these together, each with the default you would use, so "defaults are fine" is a
complete answer. Use the AskUserQuestion tool when it is available; otherwise one message.

1. **Repositories**: do other repositories belong to the project (a frontend, a shared
   library)? Default: only the ones found.
2. **Delivery**: may agents push branches and open pull requests? Default: yes for these
   repositories (needs a token in the store), a pull request for every change, never a
   push to the default branch.
3. **Where the config lives**: default a separate private repository `exradius-<slug>`
   under the same owner as the code. When the project has exactly one repository, offer
   `.exradius/exradius.yaml` inside it instead (one pull request, no second repository).
4. **Where tickets come from**: a Jira space (then which keys: it becomes
   `bridges/jira.yaml`), GitHub issues (nothing to write), or another way. Default:
   nothing to write.

Do not ask what you can read (versions, services, commands) or what has a default the
developer can edit afterwards. State these as assumptions in the summary instead:

- agents `developer` and `scout`, model and provider left to the install's defaults;
- secret store named `<slug>`;
- the agent may reach the package registries setup used, so it can add dependencies;
- the cluster's CPU architecture is unknown: the check ran for this machine's, and the
  README asks the admin (use `--platform` now if the developer knows it differs).

When there is nobody to ask (a non-interactive run), take every default and list them all
as assumptions.

## 3. Write the files

Into a new folder next to the code checkout, `../exradius-<slug>/` (or `.exradius/` in the
code repository when chosen). Validate as you go:
`bash ${CLAUDE_SKILL_DIR}/scripts/check.sh --validate <config folder>` reads the file as
Exradius does and prints the required keys and the hosts the agent may reach.

### `exradius.yaml`

Follow schema.md exactly (unknown keys are errors). Quote every value YAML would read as
a boolean, a number-like version or a date, and every string with `${secret:...}`, `: `,
`#`, `{`, `[` or a leading `*`.

- **`project`**: `slug` short and lowercase; `name`; a one-line `description` naming the
  languages and services.
- **`repositories`**: `https://` URLs, `branch` set to the default branch,
  `credential: "${secret:GITHUB_TOKEN}"` (or the host's token) when the repository is
  private or `push: true`. Public read-only repositories need none.
- **`workstation`**: `dockerfile: docker/workstation.Dockerfile` when needed, else `image:`.
  - `setup` makes a fresh clone ready for the checks, in the order CI does it: the
    frozen-lockfile install per repository (`yarn install --immutable`, `uv sync --locked`,
    `pnpm install --frozen-lockfile`, `npm ci`, `bundle install`, `go mod download`), code
    generation and builds the checks depend on, database creation and migrations for
    tests. Each command runs with cwd `/work`, as uid 1000, without root, and every run
    pays for it: only what most tickets need.
  - `cache`: one entry per package manager in setup, at the path in pitfalls.md's table,
    with the `env` entries that table names. Download stores only, never `node_modules`,
    `.venv` or build output.
- **`resources.memory`**: the workstation's peak (the heaviest of setup, tests and type
  checks, plus about 1 GB for the agent), rounded up with headroom: `4g` for a small
  service, `6g` to `8g` for a large JavaScript monorepo or a Java build. It is the pod's
  request and its hard limit, so do not cut it close, and keep it below what a cluster
  node has. A service gets a `memory` only when its documentation states one or it is
  known to be heavy (Elasticsearch, a JVM service).
- **`services`**: from the compose and CI files, named as the app expects the host name,
  each with a `ready` check that proves the real port answers (`pg_isready -h 127.0.0.1`,
  `tcp: 6379`). A compose `command:` becomes `command`; a compose `entrypoint:` that runs a
  bootstrap script usually becomes `command: [/bin/sh, -c, "..."]` (pitfalls.md).
- **`env`**: the app pointed at the services by name, test-only values in plain text,
  `${secret:NAME}` only for real secrets, the same reference wherever a value repeats. A
  secret here is in the agent's environment: prefer tokens that can only read.
- **`network.allow`**: what the agent may reach after setup (network.md). The registries
  of the package managers in setup, the hosts tests and builds reach at run time, the
  hosts of custom stdio connections. Take them from the registry configuration you read
  and from the check's `reached in setup` list, not from memory. Nothing "just in case".
- **`connections`**: `github` (catalog) with the repository token when agents open pull
  requests on GitHub, `gitlab` likewise on GitLab; nothing else unless the developer asks.
  Every connection an agent lists is started by Verify: a stdio connection needs its
  launcher (`npx`, `uvx`) in the workstation image.
- **`agents`**: `developer` and `scout`, each listing only the connections it uses. Leave
  `model`, `provider` and `identity` out unless the developer wants them.

### Notes

`notes` are quoted to every agent of the project on every task. The agent already has,
generated from the file: the repositories with branch and push permission, the services
with host, ports and non-secret settings, and the hosts it may reach. Do not repeat
those. Write what a lead would tell a new engineer and what the file cannot say:

- per repository or area, the commands that check a change, in the form that works in a
  run and that step 4 proved: the narrow form first (one file, one package), then the
  full one, with the flag that keeps a parallel suite to one worker;
- what must run before a check can (a build of shared packages, a code generator);
- what cannot run here and what the agent does instead ("the e2e suite needs a browser
  farm; run the unit tests and say so");
- how the team delivers: the branch pull requests target, one pull request per
  repository, the commit convention when there is one;
- the two or three things that surprise newcomers (never edit a merged migration; the
  generated client is checked in);
- where the team's own guide is (`AGENTS.md at the root is the team's guide; follow it`).

Keep it to what is true for most tickets, in plain sentences, short enough to read in a
minute. No secrets, nothing about Exradius, no restating of the README.

### Roles

A `role` is the last part of one agent's instructions, and when a task names no agent it
is what Exradius reads to pick one. Exradius's own rules already tell every agent to work
on a branch named after the task, to push or attach a patch, to ask only when a person
alone can answer, and to submit a result. A role adds, in this order:

1. **which tickets the agent takes, and what it never does**, in its first sentence:
   "You take tickets that ask for a change to the code: bug fixes, features, refactors,
   dependency updates." / "You take tickets that ask a question about the code. You never
   change code, push or open pull requests." Two roles that could both claim a ticket
   make routing a coin toss;
2. **when its work is done**: which checks it runs for what it touched (by name, as
   `notes` give them), and that the result says which ran and what they showed, or why
   one could not;
3. **how it delivers**: after pushing, a pull request against the right branch, opened by
   the agent with its GitHub or GitLab tool and linked in the result.

Add a third agent only when the team has a kind of work with its own definition of done
or its own connections (a reviewer that only comments, a triager with the tracker's
tools). Adapt the wording in examples.md; do not invent personas.

### The other files

- **`docker/workstation.Dockerfile`** when the stock image is not enough: `FROM` the
  official Debian or Ubuntu image of the main language at the pinned version; `git bash`
  and the system packages the tests need; tool versions from the repositories (corepack
  with the exact Yarn or pnpm into `COREPACK_HOME=/opt/corepack`, uv copied from
  `ghcr.io/astral-sh/uv:<version>`, Python installed by uv into `/opt/uv/python` and
  linked as `python3`, ...); `chmod -R a+rX` everything prepared under `/opt`; nothing
  that depends on the build machine's CPU architecture; a comment on top saying what the
  image is for. Read pitfalls.md first.
- **Service Dockerfiles** only when no stock image with a `command` can do: the
  entrypoint itself must change, or the image needs files (an extension, seed data).
- **`bridges/jira.yaml`** only when the developer named Jira spaces in step 2
  (`projects: [KEY]`; schema.md, "Bridge files").
- **`README.md`** for the admin, from admin-readme.md: where the repository is and whether
  it is private, the store and every required key with its format, source and the
  narrowest permission that works, what a run gets (network, memory, disk, caches), and
  an "Ask the admin" section for what only the install can provide (the cluster's
  architecture, a private address, a pull secret, the bridge).
- **`.gitignore`** with the line `.exradius-cache/` when the file has a
  `workstation.cache`: the local check keeps the cache folders there. When a build
  `context` is the repository root, add it to a `.dockerignore` there too. Nothing else;
  do not add files the config does not use.

## 4. Check locally

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/check.sh \
  --repo <name>=<path to checkout> [--repo ...] \
  --run '<a check command from notes>' [--run ...] \
  <config folder>
```

It does what Verify does, then more. It builds the images with BuildKit, checks the
workstation image (glibc, bash, git, the tools on PATH, each connection's launcher),
copies each local checkout to a fresh clone under `/work/<name>`, starts the services in
one network namespace as a run's pod has them, and runs every setup command as uid 1000
with a read-only root, no capabilities, the `runner` passwd entry, the project `env` with
a throwaway value per secret, the declared memory limits and the cache folders mounted
from `.exradius-cache/`. All of it runs without a direct route to the network, through a
local stand-in for Exradius's proxy: open during setup, limited to the agent's hosts for
the `--run` commands, which run after setup exactly as the agent would run them. Its last
step lists the hosts that were reached and refused.

- **Give it the commands you are about to write into `notes`**, one `--run` each, in
  their narrow form: a test file per repository, the linter, the type check when it is
  not slow. A check without `--run` proves only that setup passes.
- Builds and installs take minutes: run it in the background and wait for it, rather
  than under a short timeout.
- Read the **Network** step at the end. `reached in setup only` lists the hosts setup
  used that the agent cannot reach: the package registries among them belong in
  `network.allow`. `refused in the agent phase` lists hosts a `--run` command asked for
  and did not get: add those the agent needs (a failed command also prints
  `refused: <host>: <reason>` under its output), accept the rest. Rerun until both lists
  hold only hosts the agent can do without.
- Fix the cause (the Dockerfile, a setup command, a `ready` check, an `env` value), not
  the symptom, and rerun until it passes. A step that "exceeded its declared memory"
  needs a larger `resources.memory` or less parallelism. A command that times out
  connecting is a tool that ignores the proxy (network.md).
- With a `cache`, run it twice: after the first run every cache folder listed in "Read
  config" must have grown, and the second run's installs must report no downloads
  (pnpm: `downloaded 0`). An entry that stayed empty is at the wrong path.
- Flags: `--platform linux/amd64` (or `arm64`) builds and runs the cluster's architecture
  when it differs from this machine's; `--allow HOST` for a host the admin's install
  allows every project; `--allow-private HOST` for an internal host the admin will allow;
  `--live` mounts the checkouts as they are (uncommitted changes included); `-v` streams
  output; `--keep` keeps `/work`, the home and `/tmp` to look at; `--help` for the rest.
- It does not cover the agent and its model, pushes, the connections' tool lists, or what
  the admin's install adds. Say so; do not present a passed check as a passed Verify.

A failure that is not in pitfalls.md is worth a sentence to the developer: it is the next
project's pitfall.

## 5. Show, then publish

Show the developer, briefly:

- what you found (the stack, the services, the checks) and where each came from;
- the files, with a one-line reason per choice that is not obvious;
- the check's result: the steps, each `--run` command and its outcome, the hosts on the
  agent's list and why;
- every assumption from step 2, and anything that could not be verified.

Ask for the go-ahead. Then:

- **Separate config repository** with `gh` available and authenticated (`gh auth
  status`) or a GitHub/GitLab MCP server: `git init -b main`, commit, then
  `gh repo create <owner>/exradius-<slug> --private --source . --push` (or the MCP
  equivalent). Private unless the developer said otherwise.
- **Without such a tool**: `git init -b main` and commit locally, then print the commands
  to create the remote and push.
- **`.exradius/` in the code repository**: a branch `exradius-config`, one commit with
  `.exradius/`, push it and open a pull request (with `gh` when available), or print the
  commands.

Never force-push, never push to a branch other than the config branch or the new one.

## 6. Hand off to the admin

End with a short message the developer can forward:

- the config repository URL and branch (and the path when `.exradius/exradius.yaml`);
- whether it is private, so the admin needs a read credential for that git host;
- the store name and every key with its format, where to get it and the permission it
  needs (the README's table);
- what only the admin can provide: the cluster's architecture to confirm, a private
  address to allow, a pull secret, the bridge that claims the tracker's space;
- that Verify runs by itself and should pass as the local check did, what the check did
  not cover, and that the first real ticket is the test of `notes` and the roles: when
  an agent reports a check it could not run or a host it could not reach, that is an
  update to this repository.

## Update mode

With an `exradius.yaml` already present:

1. Read it and its Dockerfiles; run `check.sh --validate`.
2. Compare it with the code as it is now: new services in compose or CI, tool versions
   the lockfiles now need, new repositories, new secret names in the env examples, setup
   and check commands that no longer match CI.
3. Compare it with what Exradius offers now. A file written by an earlier release of this
   skill may lack:
   - `network.allow` (before 0.4.0): the agent then reaches no package registry. Propose
     it from the registries in setup;
   - `notes` commands that were never run under a run's conditions: run them with `--run`;
   - roles that open with the tickets the agent takes (routing reads them);
   - `resources.memory` and `workstation.cache` (before 0.2.0), and a service `command`
     in place of a service Dockerfile that only changes the command;
   - repository URLs as `https://`.
4. Propose the edits with a reason each, keeping everything else as it is (comments,
   order, wording of roles and notes). An update is a small diff, not a rewrite.
5. Run the check until it passes, with `--run` for the commands in `notes`.
6. With the developer's go-ahead, commit and push to the config repository's branch (or
   open a pull request when the branch is protected or the config lives in `.exradius/`).
   Exradius reads the branch head on every run and reruns Verify on a new commit; nothing
   else is needed. Update the README when keys, hosts or resources changed, and tell the
   developer which new keys the admin must add to the store before the next run.

## A failed Verify or task

1. Get the failing step's name and its log (the project's Verify tab, or the task page;
   for a network refusal the task page's Network tab). Ask for it if it was not given;
   do not guess from a description.
2. Find the step in verify-failures.md and decide whose problem it is: the config's, the
   store's or the install's. Only the first is fixed here; for the other two, write the
   developer the sentence to send the admin.
3. Reproduce it with the check before changing anything, with `--run` when the failure
   was in a task rather than in Verify. A failure the check does not reproduce is in what
   the check does not cover: say which part, and what would confirm it.
4. Fix, rerun, and continue as in Update mode from step 4.
