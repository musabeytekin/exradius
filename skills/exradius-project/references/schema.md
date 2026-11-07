# `exradius.yaml`, version 1

The file that defines an Exradius project. Exradius reads it from a git repository (the
**config repository**) at the head of a branch, on every run and on every Verify. Nothing
about a project is edited in the Exradius admin panel; the admin only pastes the config
repository's URL and fills the secret store the file names.

## Where the file lives

- `exradius.yaml` at the root of the config repository, or, when that is absent,
  `.exradius/exradius.yaml` (a config kept inside the project's own code repository).
- Branch: the one the admin configures for the project; by default the remote's default
  branch. Exradius reads the branch head every time, so a push is all an update needs.
- Paths inside the file (`dockerfile`, `context`) are relative to the file's own
  directory and must stay inside the repository.

## The whole file

```yaml
version: 1                        # required; must be the integer 1

project:                          # required
  slug: bank                      # required; [a-z][a-z0-9-]*, at most 63 characters, unique in Exradius
  name: Bank of Anthos            # default: the slug
  description: Sample retail bank # default: empty

secret_store: bank-platform       # [a-z][a-z0-9-]*; required as soon as the file has a ${secret:...}

resources:                        # default: none
  memory: 6g                      # the workstation's memory; default: unreserved and unlimited

repositories:                     # default: none. Each is cloned into /work/<name>
  - name: bank-of-anthos          # required; folder name, [A-Za-z0-9][A-Za-z0-9._-]*
    url: https://github.com/acme/bank-of-anthos.git   # required
    branch: main                  # default: the remote's default branch
    credential: "${secret:GITHUB_TOKEN}"   # token or user:password, for fetch and push
    push: true                    # default false; true needs a credential

workstation:                      # the agents' container image; default: Exradius's default image
  dockerfile: docker/workstation.Dockerfile   # either dockerfile ...
  # image: maven:3.9-eclipse-temurin-21       # ... or image, not both
  # context: docker                           # build context; default: the Dockerfile's directory
  setup:                          # default: none; each runs in the workstation, cwd /work, after cloning
    - cd bank-of-anthos && mvn -B -q dependency:go-offline
  cache:                          # default: none; folders kept per project across runs
    - ~/.m2/repository
    - ~/.cache/uv

services:                         # default: none; containers next to the agent, reachable by name
  postgres:                       # the host name; [a-z][a-z0-9-]*
    image: postgres:16            # or dockerfile: (+ context:) as for the workstation
    env:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: "${secret:DB_PASSWORD}"
    ports: [5432]                 # documentation for the agent; nothing is published
    ready:                        # default: the image's HEALTHCHECK, else "the container is running"
      command: [pg_isready, -h, 127.0.0.1, -U, app]   # or: tcp: 5432
    memory: 512m                  # default: unreserved and unlimited
  minio:
    image: minio/minio
    command: [server, /data]      # default: the image's CMD; its ENTRYPOINT is always kept

env:                              # environment of the setup commands and the agent
  DATABASE_URL: "postgres://app:${secret:DB_PASSWORD}@postgres:5432/app"

notes: |                          # default: empty; quoted to the agent as the maintainers' notes
  Run Java tests with `mvn test` in the service directory.

connections:                      # MCP servers agents may use; default: none
  github:                         # a catalog entry
    catalog: github
    config: {token: "${secret:GITHUB_TOKEN}"}
  docs:                           # a custom stdio server, mcpServers style
    command: npx
    args: ["-y", "@acme/docs-mcp"]
    env: {DOCS_TOKEN: "${secret:DOCS_TOKEN}"}
  tracker:                        # a custom http server
    url: https://mcp.example.com/mcp
    headers: {Authorization: "Bearer ${secret:TRACKER_TOKEN}"}

agents:                           # required; at least one
  developer:                      # the agent's slug; tasks name it
    name: Developer               # default: the slug, capitalised
    role: |                       # default: empty; the last part of the agent's system prompt
      You are a software developer on this team. ...
    model: claude-sonnet-5-5      # default: the runtime's default model
    provider: main                # default: the default provider; a provider slug from the panel
    runtime: claude_code          # default and only value: claude_code
    identity:                     # git author of the agent's commits
      name: Developer             # default: the agent's name
      email: dev-bot@acme.com     # default: <agent>@<project slug>.exradius.local
    connections: [github]         # default: none; names from `connections`
```

## Rules

**Types are strict.** Unknown keys anywhere are errors (a typo never passes silently).
`version` is the integer `1`, not `"1"`. `push` is `true`/`false`. `ports` and `tcp` are
integers from 1 to 65535. In `env`, service `env`, connection `config`, `headers` and
`args`, numbers are turned into strings (as in compose files) but booleans are refused:
write `"true"`, `"yes"`, `"on"` in quotes, because YAML reads them as booleans. A value YAML
reads as a date (`2026-11-07`) must also be quoted. `branch: 2026` becomes the string
`"2026"`. A key given twice is an error.

**Names.** `project.slug`, `secret_store`, service, connection and agent names match
`[a-z][a-z0-9-]*` (at most 63 characters). Repository `name` matches
`[A-Za-z0-9][A-Za-z0-9._-]*` and is unique. Env keys match `[A-Za-z_][A-Za-z0-9_]*`.
Service names may not be `api`, `workstation`, anything starting with `exradius`, or a
repository folder name. The connection name `exradius` is reserved (every agent already
has a built-in connection of that name). `model` matches `[A-Za-z0-9][A-Za-z0-9._:@/[\]-]*`;
`identity.email` must look like an email address and `identity.name` may not contain
`<`, `>` or control characters.

**Images.** The workstation and each service give `image` or `dockerfile`, never both; a
service must give one of them, the workstation may give neither (then it runs in Exradius's
default image: Ubuntu 24.04 with git, curl, ca-certificates and build-essential).
`context` only goes with `dockerfile`. Both paths are relative to the file's directory,
must exist in the repository, may not be absolute, use `..` or pass through a symlink.
Exradius builds a Dockerfile from the committed tree of the config repository (files that
are only local or git-ignored are not in the build), tags the image by the context's git
tree hash, and rebuilds only when the context changes. It builds with the Docker Engine's
classic builder: BuildKit-only syntax (`RUN --mount=...`, heredocs, `# syntax=`) fails.
Images named with `image:` are pulled by the server, so they must be reachable from it.

**Memory.** `resources.memory` (the workstation: the setup commands, the agent's session
and a resumed session) and a service's `memory` are Docker size strings: a number and one
of `b`, `k`, `m`, `g` (`512m`, `6g`, `1.5g`; upper case works too), at least `6m`, always
with a unit (`memory: 512` is refused: there is no unit-less form). A declared memory does
two things:

- **Reservation.** The Exradius worker admits a run only when its reservation (the
  workstation's `memory` plus every service's `memory`) fits the server's memory budget
  next to the runs and verifications in progress; until then the task waits, shown as
  `waiting for memory` in the panel. The budget is the server's Docker memory minus 1 GiB
  unless the admin sets another; a reservation larger than the whole budget can never
  start, and Verify fails at its "Reserve memory" step.
- **Limit.** It is the container's cgroup limit, swap included (`--memory` and
  `--memory-swap` equal). A container that exceeds it is killed by its own limit, not by
  the host killing a neighbour; the run fails with "exceeded its declared memory (6g)".

Undeclared means nothing reserved and no limit: the run competes for the server's memory
with every other run. Verify reserves and limits the same way, so a limit that is too low
shows up in Verify, not in the first task. Declare the peak of the heaviest step (the
dependency install or a type check, usually) with some headroom.

**Caches.** `workstation.cache` lists folders inside the container that survive from run
to run of the project: `~/...` (in the run's home, `/home/runner`) or absolute paths,
never inside `/work`, `/exradius`, `/opt/exradius`, `/proc`, `/sys` or `/dev`, never `~`,
`/`, `/home`, `/tmp`, `/etc`, `/usr`, `/bin`, `/lib`, `/var` or `/opt` themselves, no
`..`, each listed once. Exradius bind-mounts each from `<data
dir>/caches/<slug>/<n>-<path>/` (created on first use, owned by uid 1000) into the setup,
agent and resumed containers and into Verify. The same folders are shared by every run of
the project, concurrent ones included, so list only caches whose tool is safe with
concurrent writers (the package managers' download stores are: pnpm, npm, Yarn, uv, pip,
Maven, Gradle, Go, Cargo). Nothing cleans them; the project page shows their size, and the
admin deletes the folder to reset. Hard links do not cross bind mounts, so tools that link
from their store (pnpm, uv) copy instead: still no download. A cache is per project, so a
run can only affect later runs of the same project. The paths per package manager are in
pitfalls.md.

**Services.** `ready` gives exactly one of `command` (a list, run inside the service
container with `exec`; exit 0 means ready) or `tcp` (a port that must be listening inside
the container). Without `ready`: the image's HEALTHCHECK when it has one (must report
healthy), else ready once the container has been running for a second. Each service has
120 seconds to become ready; one that exits or times out fails the run with its log tail.
`command` (a list of strings, at least one; quote numbers, `"6380"`, and write an empty
argument as `""`) is the container's command, as compose's `command:` and `docker run
<image> <args>`: it replaces the image's `CMD` and keeps its `ENTRYPOINT`. There is no
`entrypoint` key; an image whose entrypoint must change needs a service Dockerfile.
Secrets are not allowed in `command`; put them in the service's `env` and refer to them as
`$NAME` inside a `sh -c` command.

Services run with all capabilities dropped except CHOWN, SETUID, SETGID, FOWNER,
DAC_OVERRIDE and NET_BIND_SERVICE, `no-new-privileges`, no mounts and no published ports.
Service images may be Alpine; only the workstation must be glibc-based.

**Connections.** Exactly one of `catalog`, `command` or `url`. `config` goes only with
`catalog`; `args` and `env` only with `command`; `headers` only with `url`. `type` is
accepted for `mcpServers` compatibility and ignored. Catalog entries and their config keys
(required unless marked optional):

| catalog      | config keys                          | runs with |
|--------------|--------------------------------------|-----------|
| `github`     | `token`                              | http (GitHub's hosted server) |
| `gitlab`     | `token`, `api_url` (optional)        | `npx` |
| `postgres`   | `url` (a connection string)          | `uvx` |
| `slack`      | `bot_token`, `team_id`               | `npx` |
| `sentry`     | `token`                              | `npx` |
| `linear`     | `api_key`                            | http (Linear's hosted server) |
| `jira`       | `url`, `username`, `api_token`       | `uvx` |
| `fetch`      | none                                 | `uvx` |
| `playwright` | none                                 | `npx` |
| `filesystem` | `path` (optional, default `/work`)   | `npx` |

A connection that runs with `npx` or `uvx` needs that launcher in the workstation image
(the default image has neither; Exradius warns).

**Secrets.** `${secret:NAME}` (NAME of letters, digits, `_`, `.`, `-`) may appear only in
string values under `repositories[].credential`, `services.*.env`, `env`, and
`connections.*` `config`, `env` and `headers`. Anywhere else it is an error. The set of
names referenced is the project's **required keys**; nothing declares them separately.
Each resolves from the store named by `secret_store` when a run starts. A project whose
store lacks a key cannot run (tasks are rejected with "missing secret NAME in store S").
The workstation also receives every secret that a service's env uses, under its own name
(so `$DB_PASSWORD` is set in the workstation when `services.postgres.env` references
`${secret:DB_PASSWORD}`). Repository credentials and connection values are used only where
they are referenced. Secret values never appear in the agent's prompt, the panel or logs.

**Repositories.** Cloned with their own `credential`; they never fall back to the
credential Exradius uses for reading the config repository. `push: true` lets agents push
with that credential; the agent is told, per repository, whether pushing is allowed. Work
an agent does not push is lost when the task ends (Exradius reports unpushed commits but
never commits or pushes for the agent).

**Agents.** The agent's system prompt is Claude Code's preset, Exradius's operating
rules, a generated "your environment" section (the image, repositories with branch and
push permission, services with their non-secret settings, the notes), then `role`. Tasks
name the project slug and the agent slug; an agent missing from the current file rejects
the task.

## What the admin does with the file

1. Once per git host: give Exradius a credential to read config repositories (only for
   private ones).
2. Per project: paste the config repository URL (and a branch or path when not the
   defaults).
3. Per store: create the secret store the file names and fill every required key. The
   panel accepts a pasted `.env` block for several keys at once.

Then Verify runs: "Read config" (commit, errors, warnings, missing keys), "Reserve
memory" (when the file declares some), "Build
workstation" and "Build service <name>", clone each repository, "Start service <name>",
"Setup: <command>" for each setup command, then stop the services and remove the
directory.
