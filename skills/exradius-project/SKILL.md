---
name: exradius-project
description: Set up or update a project for Exradius, the self-hosted harness that runs AI agents on a team's code. Reads the code, writes the project's exradius.yaml (config version 1), a workstation Dockerfile when the stock image is not enough, and a README for the Exradius admin, checks it all locally with Docker the way Exradius's Verify does, and creates the config repository. Use when someone wants to put a codebase on Exradius, write or fix an exradius.yaml, or understand why Verify failed.
argument-hint: "[code checkout paths or repository URLs; default: the current folder]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/check.sh *), Bash(bash ${CLAUDE_SKILL_DIR}/scripts/check.sh *)
---

# Set up an Exradius project

Exradius runs agents (Claude Code) on a team's repositories in containers. A project is
one file, `exradius.yaml`, in a git repository: the code repositories, the workstation
image the agents work in, the services next to it (Postgres, Redis, ...), environment,
notes, MCP connections, the agents, and the name of a **secret store** the Exradius admin
fills. The admin pastes the config repository's URL and fills the store; the developers
own everything else. You do the developers' part, on their machine, where a mistake costs
seconds instead of a commit, a push and a Verify on the server.

This skill writes `exradius.yaml` **version 1**. If a file says another version, stop and
say this skill release does not know it.

Bundled with this skill (read them; do not guess):

- `${CLAUDE_SKILL_DIR}/references/schema.md`: every key, its default and the rules.
- `${CLAUDE_SKILL_DIR}/references/run-conditions.md`: what a workstation must survive
  (uid 1000 named `runner`, read-only root, no capabilities, glibc, bash, git).
- `${CLAUDE_SKILL_DIR}/references/pitfalls.md`: the failures seen so far and their fixes.
  Read it before writing a Dockerfile.
- `${CLAUDE_SKILL_DIR}/references/examples.md`: three real projects (Java/Python, Node with
  Yarn 4, Python with uv plus a pnpm dashboard), their files and why each line is there.
- `${CLAUDE_SKILL_DIR}/references/admin-readme.md`: the README template and how to
  describe each kind of key.
- `${CLAUDE_SKILL_DIR}/scripts/check.sh`: the local check (Docker CLI, git, Python 3.8+).

## Hard rules

- **Never ask for, read, print or write a secret value.** The file references secrets as
  `${secret:NAME}`; the admin puts values into the store. Read only env files that git
  tracks (`git ls-files '*.env*' '.env*'`): `.env.example`, `.env.sample`, `.env.test`.
  Never open an untracked `.env`, `.env.local` or similar, credentials files, or the
  developer's shell history. If a value in a tracked file looks like a real secret, use
  its name only and tell the developer.
- **Never call the Exradius API** or the admin panel, and never ask for an Exradius URL or
  login. The skill's output is files and a git repository.
- **Show before you publish.** Writing files in a new local folder is fine; creating a
  remote repository, pushing, or opening a pull request happens only after the developer
  has seen the files and said yes. Private by default.
- **One shot, then review.** Do the reading yourself, ask the few questions of step 2 in a
  single round, then write everything with a one-line reason per choice. Do not
  interview.

## Mode

Look for an existing config first: `exradius.yaml` or `.exradius/exradius.yaml` in the
current folder, in a sibling `../exradius-*` folder, or one the developer names. If one
exists, go to **Update mode** below. Otherwise set up a new project.

## 1. Read the project

For each code repository (the current checkout, plus any the developer named):

- **Languages and tool versions the lockfiles demand**: `package.json` (`packageManager`,
  `engines`), `.nvmrc`, `.node-version`, `.tool-versions`, `mise.toml`, `.python-version`,
  `pyproject.toml` (`requires-python`, `[tool.uv]` `required-version`, `exclude-newer`
  and other syntax that needs a recent uv), the header of `uv.lock`, `poetry.lock`,
  `pnpm-lock.yaml` and `package-lock.json` `lockfileVersion`, `yarn.lock` with
  `.yarnrc.yml`, `go.mod` (`go`, `toolchain`), `rust-toolchain.toml`, `Gemfile.lock`
  (`BUNDLED WITH`), `pom.xml` / Gradle wrapper and the Java release, `global.json`.
  Note the developer's own tool versions (`uv --version`, `node --version`, ...) where a
  lockfile was written by a newer tool than it records.
- **Services**: `docker-compose*.yml`, `compose*.yaml`, `.devcontainer/`, CI service
  containers (`.github/workflows/*`, `.gitlab-ci.yml`, `.circleci/`): image, version, env,
  ports, the host names the app expects.
- **Commands**: how dependencies are installed, how the database is created or migrated,
  how tests and linters run (README, CONTRIBUTING, AGENTS.md / CLAUDE.md, Makefile,
  `package.json` scripts, CI steps).
- **Environment**: tracked env examples and test env files (what the app reads, what is
  already supplied for tests, what points at `localhost` and must point at a service),
  CI env blocks. Secret names come from here: anything that is a password, token or key
  and is not a throwaway test value.
- **Git remotes**: each repository's URL, default branch, host (GitHub, GitLab, ...),
  whether it is private (`gh repo view --json visibility` when `gh` is available).

Then decide the workstation: the stock image of the main language is enough only when it
has every tool the setup commands, tests and linters call at the right version, plus bash
and git, and is glibc-based. Usually a short Dockerfile is needed (see examples.md).

## 2. Ask what cannot be inferred, in one round

Ask these together, each with the default you would use, so "defaults are fine" is a
complete answer. Use the AskUserQuestion tool when it is available; otherwise one message.
When there is nobody to ask (a non-interactive run), take the defaults and list them as
assumptions in the summary.

1. **Repositories**: are there other repositories that belong to the project (a frontend,
   a shared library, infrastructure)? Default: only the ones found.
2. **Agents**: default `developer` (implements tickets, runs the tests, pushes a branch
   and opens a pull request) and `scout` (investigates and reports, never changes code).
3. **Pushing**: may agents push to each repository? Default: yes for the developer's
   repositories (needs a token in the store), with a pull request for every change.
4. **Secret store name**: default `<slug>` (or the team's shared store if they have one).
5. **Where the config lives**: default a separate private repository
   `exradius-<slug>` under the same owner as the code. When the project has exactly one
   repository, offer `.exradius/exradius.yaml` inside it instead (one pull request, no
   second repository; Exradius reads that location when the root has no `exradius.yaml`).

Do not ask about things you can read (versions, services, commands) or that have
defaults the developer can edit afterwards (agent roles' wording, model, notes).

## 3. Write the files

Into a new folder next to the code checkout, `../exradius-<slug>/` (or `.exradius/` in the
code repository when chosen):

- **`exradius.yaml`**, version 1. Follow schema.md exactly (unknown keys are errors).
  Conventions, as in examples.md:
  - `project.slug` short, lowercase, `[a-z][a-z0-9-]*`; `name` and a one-line
    `description` naming the languages and services.
  - `repositories` with `branch` set to the default branch; `credential:
    "${secret:GITHUB_TOKEN}"` (or a GitLab token) when the repository is private or
    `push: true`; public read-only repositories need no credential.
  - `workstation.dockerfile: docker/workstation.Dockerfile` when needed, else `image:`;
    `setup` with the frozen-lockfile install per repository (`cd <repo> && yarn install
    --immutable`, `uv sync --locked`, `pnpm install --frozen-lockfile`, `npm ci`,
    `bundle install`, `go mod download`), database creation and migrations for tests,
    `cp -r /opt/m2 ~/.m2`-style cache copies. Each command runs with cwd `/work`, as uid
    1000, without root.
  - `services` from the compose/CI files, named as the app expects the host name, each
    with a TCP-based `ready` (`pg_isready -h 127.0.0.1 ...`, `tcp: 6379`).
  - `env` pointing the app at the services by name, test-only values in plain text,
    `${secret:NAME}` only for real secrets, the same reference wherever a value repeats.
  - `notes`: what you would tell a new engineer on day one: where things are, how to run
    tests and linters per repository, anything surprising.
  - `connections`: `github` (catalog) with the same token when agents open pull requests
    on GitHub; nothing else unless the developer asks.
  - `agents`: `developer` and `scout` with roles in the style of examples.md, adapted to
    the project's checks and pull request rule. Leave `model`, `provider` and `identity`
    out unless the developer wants them (defaults: the runtime's model, the default
    provider, `<agent>@<slug>.exradius.local`).
  - Quote every value YAML would read as a boolean, number-like version, or date, and
    every string with `${secret:...}`, `: `, `#`, `{` or `[`.
- **`docker/workstation.Dockerfile`** when the stock image is not enough: `FROM` the
  official Debian/Ubuntu image of the main language at the pinned version; install
  `git bash` and the system packages the tests need; install tool versions from the
  repositories (corepack + exact Yarn/pnpm into `COREPACK_HOME=/opt/corepack`, uv
  copied from `ghcr.io/astral-sh/uv:<version>`, Python installed by uv into
  `/opt/uv/python` and linked as `python3`, ...); `chmod -R a+rX` everything prepared under
  `/opt`; no `RUN --mount`, no heredocs; a comment on top saying what the image is for.
  Read pitfalls.md first.
- **Service Dockerfiles** only when no stock image can do (an extension, seed data).
- **`README.md`** for the admin, from admin-readme.md: the config repository URL and
  branch, whether it is private (so a host credential is needed), the store name, every
  required key with its format, where to obtain it and what it is used for, a `.env`
  block of the key names with empty values, and the memory and disk a run needs.
- A `.gitignore` is not needed; do not add files the config does not use.

Validate as you go: `bash ${CLAUDE_SKILL_DIR}/scripts/check.sh --validate <config folder>`
reads the file like Exradius does and lists the required keys (use that list for the
README).

## 4. Check locally

```bash
bash ${CLAUDE_SKILL_DIR}/scripts/check.sh --repo <name>=<path to checkout> [--repo ...] <config folder>
```

It builds the images with the classic builder (as the server does), checks the
workstation image (glibc, bash, git, and lists the tools on PATH), copies each local
checkout to a fresh clone under `/work/<name>` (repositories without `--repo` or
`--repos-dir` are cloned from their URL with the developer's own git credentials), starts
the services on a temporary network and waits for `ready` with the server's 120-second
timeout, runs every setup command as uid 1000 with a read-only root filesystem, no
capabilities, `no-new-privileges`, the generated `runner` passwd entry, `HOME=/home/runner`
and the project `env` with a throwaway value for each secret, then removes everything.
Exit 0 means every step passed; on a failure it prints the failing step and its output's
tail, and the full log path.

- Builds and installs can take many minutes: run it in the background and wait for it,
  rather than under a short timeout.
- Fix the cause (the Dockerfile, a setup command, a `ready` check, an `env` value), not the
  symptom, and rerun until it passes. Every failure is a lesson for pitfalls.md's list;
  mention new ones to the developer.
- Flags: `--live` mounts the checkouts as they are (uncommitted changes included);
  `--platform linux/amd64` builds and runs the server's architecture on an Apple Silicon
  Mac (slow; worth it once when the Dockerfile downloads binaries); `-v` streams output;
  `--keep` keeps the temporary folders; `--help` for the rest.
- It does not cover the agent runtime, providers, pushes or MCP connections. Say so.

## 5. Create the repository and push

Show the developer the files (paths and a one-line reason per choice, plus the check's
result) and ask for the go-ahead. Then:

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
- the store name and every key with its format and where to get it (the README's table);
- that Verify on the project should pass, as the local check did, and what the check did
  not cover.

## Update mode

With an `exradius.yaml` already present (in the folder or in `.exradius/`):

1. Read it and its Dockerfiles; run `check.sh --validate`.
2. Diff the code against it: new services in compose or CI, tool versions the lockfiles
   now need (a new `packageManager`, a newer uv or Node), new repositories, new secret
   names in the env examples, setup commands that no longer match the README or CI.
   When the developer brings a failed Verify's step and log, start from that.
3. Propose the edits with a reason each, keeping everything else as it is (comments,
   order, wording of roles and notes).
4. Run the check until it passes.
5. With the developer's go-ahead, commit and push to the config repository's branch (or
   open a pull request when the branch is protected or the config lives in `.exradius/`).
   Exradius reads the branch head on every run; nothing else is needed. Update the README
   when keys changed, and tell the developer which new keys the admin must add to the
   store before the next run.
