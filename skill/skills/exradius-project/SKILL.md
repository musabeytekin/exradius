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
admin pastes the config repository's URL, fills the store (or points it at a Vault path)
and allows the project to use it; the developers own everything else. You do the
developers' part, on their machine, where a mistake costs seconds instead of a commit, a
push and a Verify in the cluster.

The file is good when an agent, on its first ticket, is where a new engineer is after a
well-run first day: the tools are installed at the right versions, the dependencies are
there, the services answer, it knows which command checks its work and that command
works, it can start the application to reproduce a bug, and it knows how the team wants
changes delivered. Everything below serves that. A file that only passes Verify is not
done: Verify proves the workspace can be prepared, not that anyone can work in it. A file
whose tests pass is not done either while the application cannot start.

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
| `${CLAUDE_SKILL_DIR}/references/stacks.md` | before writing a Dockerfile: the rows of the project's languages and services |
| `${CLAUDE_SKILL_DIR}/references/pitfalls.md` | its cache table ("Every run downloads all dependencies again") before writing `workstation.cache`; the section a symptom points at when a check step fails. Not the whole file |
| `${CLAUDE_SKILL_DIR}/references/running-the-app.md` | before writing `services` and `notes`: what the application needs to start, and how to prove that it does |
| `${CLAUDE_SKILL_DIR}/references/examples.md` | the one example closest to the project's stack (its first table says which) |
| `${CLAUDE_SKILL_DIR}/references/admin-readme.md` | when writing the README |
| `${CLAUDE_SKILL_DIR}/references/verify-failures.md` | when the developer brings a failed Verify or task |

`${CLAUDE_SKILL_DIR}/scripts/check.sh` is the local check. It needs the Docker CLI with a
running daemon, git and Python 3.8 or later, and pulls one small image for its proxy
(`python:3.13-alpine`) the first time.

## Hard rules

- **Never ask for, read, print or write a secret value.** The file references secrets as
  `${secret:NAME}`; the admin puts values into the store. Read only env files that git
  tracks (`git ls-files '*.env*' '.env*'`): `.env.example`, `.env.test`, a tracked `.env`
  with placeholders. Never open an untracked `.env`, `.env.local` or similar, credentials
  files, `~/.npmrc`, or the developer's shell history. If a value in a tracked file looks
  like a real secret, use its name only and tell the developer. A key file CI itself
  takes from the repository is a test value: use it by path as CI does, never print it.
- **Never call the Exradius API** or the admin panel, and never ask for an Exradius URL or
  login. The skill's output is files and a git repository.
- **Show before you publish.** Writing files in a new local folder is fine; creating a
  remote repository, pushing, or opening a pull request happens only after the developer
  has seen the files and said yes. Private by default.
- **One shot, then review.** Do the reading yourself, ask the few questions of step 2 in a
  single round, then write everything with a one-line reason per choice. Do not
  interview.
- **Claim only what the check showed.** A command goes into `notes` after it ran under the
  run's conditions, and so does every statement about what works or does not ("the
  server runs without ffmpeg" was written once, untested, and was false). What was not
  run is listed as not run.

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
clean clone, as a run does), then what the team's guide tells agents to run (an
`AGENTS.md` section on verifying a change, with its memory limits and worker counts),
then lockfiles and version files, then Makefile and package scripts, then README and
CONTRIBUTING (they describe a machine that was set up long ago). CI is wherever the
project keeps it: `.github/workflows/`, `.gitlab-ci.yml`, `.circleci/`, `.forgejo/` or
`.gitea/workflows/`, `.woodpecker*`, `Jenkinsfile`, `azure-pipelines.yml`.

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
  containers, and `docker run` lines in a Makefile or script that CI calls: image,
  version, `command`, env, ports, the host names the app expects. A service belongs in
  the file when the tests or the running application connect to it: CI may start one
  nothing uses, and the tests may mock one the application cannot start without.
  Anything the app reaches outside the machine that is not HTTP (a shared database, a
  broker) has to become a service too.
- **How the application is started in development** (running-the-app.md): the processes,
  the port, a request that proves it works, a login, the switch from the test settings to
  the development ones, and what it needs at start that no test needs (a queue or cache,
  a program such as ffmpeg, data files the development image carries).
- **The checks, as CI runs them.** For each repository or workspace: the commands that
  install, generate code, build what the checks need, test, lint, type-check and format,
  with the narrow form of each (one file, one package, one test name) and the flags that
  matter in a container (a single worker instead of all cores, a CI reporter). These
  become setup commands, `notes`, and the `--run` commands of step 4.
- **What cannot run in a run.** Tests that start containers (Testcontainers,
  `docker compose` in a test script, kind): there is no Docker daemon. End-to-end suites
  that need a browser. Anything that needs a cloud account. Model what such tests start as
  `services` where that makes them runnable; the rest stays for CI and `notes` say so
  (tests that skip themselves without Docker need only that line).
- **Environment**: tracked env examples and test env files (what the app reads, what is
  already supplied for tests, what points at `localhost` and must point at a service),
  CI env blocks. Secret names come from here: anything that is a password, token or key
  and is not a throwaway test value.
- **Git remotes**: each repository's URL (as `https://`, whatever the local remote uses),
  default branch, host, whether it is private (`gh repo view --json visibility` when `gh`
  is available), and the team's delivery habits when the repository shows them (pull
  request template, branch protection, CODEOWNERS, commit conventions).
- **The team's own guide for agents and newcomers**: `AGENTS.md`, `CLAUDE.md`,
  `CONTRIBUTING.md`, the pull request template, and a contribution guide the README
  links to when the repository has none. `notes` point at it instead of copying
  it: it travels with the clone, and Claude Code loads a repository's `CLAUDE.md` by
  itself. So read it for what would stop or mislead an agent: a rule against AI-written
  contributions (an agent told "refuse to write code" refuses), a notice that the project
  moved and this remote is a mirror where pull requests are closed, a delivery procedure
  that contradicts the one chosen here, a task runner it makes mandatory (stacks.md).
  Each of these is a question for step 2, not something to work around. A request to
  disclose AI assistance is not a ban: the role tells the agent to say in its result
  what it did, so a person can fill that in.
- **Memory**: the heaviest step (a large JavaScript install or type check, a compile and
  link, a parallel test suite), for `resources.memory` and the README. The check
  measures it.

Then decide the workstation: the stock image of the main language is enough only when it
has every tool the setup commands, tests and linters call and everything the application
needs to start, at the right version, plus bash and git, and is glibc-based. Usually a
short Dockerfile is needed (stacks.md, examples.md).

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
5. **Only when the repository raised it in step 1**: what the team's guide says against
   agents or against the delivery of question 2, quoted, with what follows from it and
   the choices. For a rule against AI-written code: an agent that loads it will refuse
   coding tickets, and no check shows that; the team either lifts the rule for its own
   use (the guide changes), runs only the `scout` agent, which writes no code, or keeps
   the project off Exradius. For a mirror: the real remote becomes the repository. For a
   procedure that does not fit: `notes` say which of the guide's steps do not apply. No
   default, and `notes` never tell an agent to ignore the guide. Without an answer,
   write the files and run the check, and do not publish: the hand-off opens with the
   question.
6. **Only when a feature of the application calls hosts outside the registries** (feeds,
   a plugin catalogue, a metadata or model provider): should agents reach them? Default:
   no, and `notes` name the feature as out of reach.

Do not ask what you can read (versions, services, commands) or what has a default the
developer can edit afterwards. State these as assumptions in the summary instead:

- agents `developer` and `scout`, model and provider left to the install's defaults;
- secret store named `<slug>` when the file references a secret; none otherwise;
- the agent may reach the package registries setup used, so it can add dependencies;
- what setup prepares for running the application, and what it leaves to `notes`
  because it is slow;
- the cluster's CPU architecture is unknown: the check ran for this machine's, and the
  README asks the admin (use `--platform` now if the developer knows it differs).

When there is nobody to ask (a non-interactive run), take every default and list them all
as assumptions.

## 3. Write the files

Into a new folder next to the code checkout, `../exradius-<slug>/` (or `.exradius/` in the
code repository when chosen). Make the new folder a git repository at once
(`git init -b main`) and commit before each check: Exradius builds from the committed
tree, and the check warns about what is not in it. A local commit publishes nothing.
Validate as you go:
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
    `pnpm install --frozen-lockfile`, `npm ci`, `BUNDLE_FROZEN=true bundle install`,
    `go mod download`), code
    generation and builds the checks depend on, database creation and migrations for
    tests. Then what starting the application needs, when it takes under about a minute
    (its development database and seed); slower steps go to `notes` with their duration
    (running-the-app.md). Each command runs with cwd `/work`, as uid 1000, without root,
    and every run pays for it: only what most tickets need. In a monorepo, build only
    what the checks import, without type checks, with the build tool's concurrency capped
    (`turbo run build --filter='<package>...' --concurrency=2`).
  - `cache`: one entry per package manager in setup, at the path in pitfalls.md's table,
    with the `env` entries that table names. Download stores only, never `node_modules`,
    `.venv` or build output.
- **`resources.memory`**: the highest peak the check prints in its Memory line, plus the
  running application's peak (the agent keeps it up while it builds and tests; the check
  measures each step alone), plus about 1 GB for the agent, rounded up to a whole
  number, and never below `4g`: the commands of a real ticket are wider than the ones
  measured. Before the first check, start from `4g` (`8g` for a large JavaScript
  monorepo) and correct it from the measurement. It is
  the pod's request and its hard limit: keep it below what a cluster node has, and keep
  it when it is above this machine's Docker memory (it is the cluster's number). A
  service gets a `memory` only when its documentation states one or it is known to be
  heavy (Elasticsearch, a JVM service).
- **`services`**: what the tests and the running application connect to, named as the
  app expects the host name, each with a `ready` check that proves the real port answers
  (`pg_isready -h 127.0.0.1`, `tcp: 6379`; never an HTTP client such as `wget` or `curl`
  without its no-proxy flag: pitfalls.md). A compose `command:` becomes `command`; a
  compose `entrypoint:` that runs a bootstrap script usually becomes
  `command: [/bin/sh, -c, "..."]` (pitfalls.md; stacks.md has the common services).
- **`env`**: the app pointed at the services by name, test-only values in plain text,
  `${secret:NAME}` only for real secrets, the same reference wherever a value repeats. A
  secret here is in the agent's environment: prefer tokens that can only read. Only what
  holds for testing and for running the application: not a test mode
  (`RAILS_ENV=test`, `MIX_ENV=test`), which belongs on the command that needs it.
- **`network.allow`**: what the agent may reach after setup (network.md). The registries
  of the package managers in setup, the hosts tests and builds reach at run time, the
  hosts of custom stdio connections. Take them from the registry configuration you read,
  the lockfiles' download URLs and the check's `reached in setup` list, not from memory.
  Nothing "just in case".
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
- how to run the application, as step 4 proved it (running-the-app.md): the commands in
  order from `/work/<repo>`, the port and a URL that shows it is up, the login, what
  takes long, and how to stop it when it holds a port or database the tests use;
- what cannot run here and what the agent does instead ("the e2e suite needs a browser
  farm; run the unit tests and say so"; "feeds from other hosts cannot be fetched");
- how the team delivers: the branch pull requests target, one pull request per
  repository, the commit convention when there is one; when setup installs git hooks
  (husky, lefthook), what a commit runs, proved with a commit as a `--run`;
- the two or three things that surprise newcomers (never edit a merged migration; the
  generated client is checked in);
- where the team's own guide is (`AGENTS.md at the root is the team's guide; follow it`),
  and which of its steps do not apply in a run (its task runner is not installed; its
  pull request procedure when agents attach patches).

Keep it to what is true for most tickets, in plain sentences, short enough to read in a
minute. No secrets, nothing about Exradius, no restating of the README. A workaround
only the local check needed does not belong here.

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
   the agent with its GitHub or GitLab tool and linked in the result. With `push: false`
   this item is one sentence or nothing: Exradius's rules already make the agent attach
   a patch, and no `github` connection is needed for it.

Add a third agent only when the team has a kind of work with its own definition of done
or its own connections (a reviewer that only comments, a triager with the tracker's
tools). Adapt the wording in examples.md; do not invent personas.

### The other files

- **`docker/workstation.Dockerfile`** when the stock image is not enough: `FROM` the
  official Debian or Ubuntu image of the main language at the pinned version; `git bash`
  and the system packages the tests and the application's start need; tool versions from
  the repositories (corepack with the exact Yarn into `COREPACK_HOME=/opt/corepack`, uv
  copied from `ghcr.io/astral-sh/uv:<version>`, Python installed by uv into
  `/opt/uv/python` and linked as `python3`, a second runtime copied from its official
  image, ...); `chmod -R a+rX` everything prepared under `/opt`; nothing that depends on
  the build machine's CPU architecture; a comment on top saying what the image is for.
  Read stacks.md and pitfalls.md first. Versions: CI's over a version file when they
  disagree (a newer Python than CI tests may have no wheels); where CI follows a channel
  (`stable`, `latest`), pin today's release and say so in the README; where CI's newest
  version has no official image yet, the newest that has one.
- **Service Dockerfiles** only when no stock image with a `command` can do: the
  entrypoint itself must change, or the image needs files (an extension, seed data).
- **`bridges/jira.yaml`** only when the developer named Jira spaces in step 2
  (`projects: [KEY]`; schema.md, "Bridge files").
- **`README.md`** for the admin, from admin-readme.md: where the repository is and whether
  it is private, the store and every required key with its format, source and the
  narrowest permission that works (or that the project needs no store), what a run gets
  (network, memory, disk, caches), and an "Ask the admin" section for what only the
  install can provide (the cluster's architecture, a private address, a pull secret, the
  bridge).
- **`check/start-app.sh`**, the script that proved the application starts
  (running-the-app.md). Exradius does not read it; the next update reruns it.
- Nothing else; do not add files the config does not use.

## 4. Check locally

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/check.sh \
  --repo <name>=<path to checkout> [--repo ...] \
  --run '<a check command from notes>' [--run ...] \
  --run-file check/start-app.sh \
  <config folder>
```

It does what Verify does, then more. It builds the images with BuildKit, checks the
workstation image (glibc, bash, git, the tools on PATH, each connection's launcher),
copies each local checkout to a fresh clone under `/work/<name>`, starts the services in
one network namespace as a run's pod has them, and runs every setup command as uid 1000
with a read-only root, no capabilities, the `runner` passwd entry, the project `env` with
a throwaway value per secret, the declared memory limits and the cache folders mounted.
`/work`, the home and `/tmp` are Docker volumes, a Linux filesystem as a run's disk is.
All of it runs without a direct route to the network, through a local stand-in for
Exradius's proxy: open during setup, limited to the agent's hosts for the `--run`
commands, which run after setup exactly as the agent would run them. It ends with the
peak memory of every step and the hosts that were reached and refused.

- **Give it the commands you are about to write into `notes`**, one `--run` each, in
  their narrow form: a test file per repository, the linter, the type check when it is
  not slow. A check without `--run` proves only that setup passes. Each runs as
  `/bin/sh -c`; every one is run and reported even when an earlier one failed.
- **Give it the start of the application** as `--run-file check/start-app.sh`
  (running-the-app.md). A passing step prints its last ten lines: they must show the
  application answering, not only the script ending.
- **Prove a registry entry** with one command per package manager that downloads with an
  empty cache, since tests and linters download nothing: `npm view <package> version`,
  `uv lock --upgrade-package <package> --dry-run`, `cargo add --dry-run <crate>`,
  `COMPOSER_CACHE_DIR=/tmp/c composer reinstall <package>`.
- Builds and installs take minutes: run it in the background and wait for it, rather
  than under a short timeout. One check at a time on a laptop: two share Docker's
  memory, and a step killed for that is not the config's fault.
- Read the **Memory** line. It names the heaviest step and its peak: `resources.memory`
  follows from it (step 3). "A process in this step was killed for memory" means the
  limit, or the tool's parallelism (pitfalls.md has the flags per tool).
- Read the **Network** step. `reached in setup only` lists the hosts setup used that the
  agent cannot reach: the package registries among them belong in `network.allow`.
  `refused in the agent phase` lists hosts a `--run` command asked for and did not get:
  add those the agent needs, accept the rest (telemetry, update checks). Refusals shown
  under a failed step happened during it and may have nothing to do with the failure.
  Rerun until both lists hold only hosts the agent can do without.
- Fix the cause (the Dockerfile, a setup command, a `ready` check, an `env` value), not
  the symptom, and rerun until it passes. A command that cannot resolve a name or times
  out connecting is a tool, or the project's own code, ignoring the proxy (network.md).
  A test that only looks a public name up fails in the check and passes in a run
  (network.md, "Differences to know"): leave it out of the check's command, not out of
  `notes`.
- **When a full suite fails**, compare with CI on the same commit before changing the
  config (`gh run list --branch <branch>`, `gh run view --log-failed`; when that
  commit's job never reached the tests, the nearest earlier one that did, named). A
  suite that is red on the default branch is recorded as "fails as CI does", with the
  tests that differ, if any; `notes` tell the agent it is red and since when, and the
  last check runs without it, so that its result speaks about the config. A command that
  could not be proven in the check is written as not proven, with the reason, never as
  checked.
- With a `cache`, run it twice. After the first run every cache folder in "Read config"
  has grown, unless the tool had nothing to keep there (`~/.cargo/git` without git
  dependencies). The second run's installs must not download: pnpm prints
  `downloaded 0`, the npm log under `~/.npm/_logs` has no `cache miss`, and for the
  others the host the packages come from is missing from `reached in setup` (the index
  may still be asked: `index.rubygems.org` stays, `rubygems.org` goes; NuGet has one
  host for both, so compare the restore's duration). An entry that
  stayed empty although its tool downloaded is at the wrong path. A cache saves the
  downloads, not the compiling: an install that builds native extensions takes as long
  warm as cold, and the README says so.
- Flags: `--platform linux/amd64` (or `arm64`) builds and runs the cluster's architecture
  when it differs from this machine's; `--allow HOST` for a host the admin's install
  allows every project; `--allow-private HOST` for an internal host the admin will allow;
  `--live` mounts the checkouts as they are (uncommitted changes included); `-v` streams
  output; `--log PATH` chooses where the full log goes (the result names it either way);
  `--keep` keeps the volumes to look at; `--cache-dir DIR` moves the cache folders
  (default `~/.cache/exradius-check/<slug>`; delete it to start cold); `--help` for the
  rest.
- It does not cover the agent and its model, pushes, the connections' tool lists, or what
  the admin's install adds. Say so; do not present a passed check as a passed Verify.

A failure that is not in pitfalls.md is worth a sentence to the developer: it is the next
project's pitfall.

## 5. Show, then publish

Show the developer, briefly:

- what you found (the stack, the services, the checks) and where each came from;
- the files, with a one-line reason per choice that is not obvious;
- the check's result: the steps, each `--run` command and its outcome, how the
  application was started and what it answered, the memory peak, the hosts on the
  agent's list and why;
- every assumption from step 2, and anything that could not be verified.

Ask for the go-ahead. Then:

- **Separate config repository** with `gh` available and authenticated (`gh auth
  status`) or a GitHub/GitLab MCP server: commit, then
  `gh repo create <owner>/exradius-<slug> --private --source . --push` (or the MCP
  equivalent). Private unless the developer said otherwise.
- **Without such a tool**: commit locally, then print the commands to create the remote
  and push.
- **`.exradius/` in the code repository**: a branch `exradius-config`, one commit with
  `.exradius/`, push it and open a pull request (with `gh` when available), or print the
  commands.

Never force-push, never push to a branch other than the config branch or the new one.

## 6. Hand off to the admin

End with a short message the developer can forward:

- the config repository URL and branch (and the path when `.exradius/exradius.yaml`);
- whether it is private, so the admin needs a read credential for that git host;
- the store name and every key with its format, where to get it and the permission it
  needs (the README's table), or that the project needs no store;
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
   - `notes` that do not say how to run the application, and services or system
     packages only its start needs (before 0.5.0): running-the-app.md;
   - a test mode in `env` (`RAILS_ENV=test`), which a plain start then inherits;
   - `notes` commands that were never run under a run's conditions: run them with `--run`;
   - roles that open with the tickets the agent takes (routing reads them);
   - `resources.memory` and `workstation.cache` (before 0.2.0), and a service `command`
     in place of a service Dockerfile that only changes the command;
   - repository URLs as `https://`.
4. Propose the edits with a reason each, keeping everything else as it is (comments,
   order, wording of roles and notes). An update is a small diff, not a rewrite.
5. Run the check until it passes, with `--run` for the commands in `notes` and
   `--run-file` for the application's start. A `.exradius-cache/` folder next to the
   file is from an earlier release: the check still uses it when it is there; delete it
   to use the new place.
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
