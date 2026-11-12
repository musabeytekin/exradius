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
- A bridge's settings for the project live next to the file, in `bridges/<name>.yaml`
  (see "Bridge files" below). `exradius.yaml` holds Exradius's configuration only.

## The whole file

```yaml
version: 1                        # required; must be the integer 1

project:                          # required
  slug: bank                      # required; [a-z][a-z0-9-]*, at most 63 characters, unique in Exradius
  name: Bank of Anthos            # default: the slug
  description: Sample retail bank # default: empty

secret_store: bank-platform       # [a-z][a-z0-9-]*; required as soon as the file has a ${secret:...}

resources:                        # default: none
  memory: 6g                      # the workstation's memory: request and limit; default: none

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

services:                         # default: none; next to the agent, by name on the run's localhost
  postgres:                       # the host name; [a-z][a-z0-9-]*
    image: postgres:16            # or dockerfile: (+ context:) as for the workstation
    env:
      POSTGRES_USER: app
      POSTGRES_PASSWORD: "${secret:DB_PASSWORD}"
    ports: [5432]                 # documentation for the agent; unique across services
    ready:                        # default: the image's HEALTHCHECK, else "the container is running"
      command: [pg_isready, -h, 127.0.0.1, -U, app]   # or: tcp: 5432
    memory: 512m                  # request and limit; default: none
  minio:
    image: minio/minio
    command: [server, /data]      # default: the image's CMD; its ENTRYPOINT is always kept

env:                              # environment of the setup commands and the agent
  DATABASE_URL: "postgres://app:${secret:DB_PASSWORD}@postgres:5432/app"

network:                          # default: none
  allow:                          # hosts the agent may reach besides its model, the
    - repo.maven.apache.org       # repositories and its connections; setup reaches any host
    - pypi.org
    - '*.pythonhosted.org'        # quoted: a leading * is YAML alias syntax

notes: |                          # default: empty; quoted to the agent as the maintainers' notes
  Run Java tests with `mvn test` in the service directory.

connections:                      # MCP servers agents may use; default: none
  github:                         # a catalog entry
    catalog: github
    config: {token: "${secret:GITHUB_TOKEN}"}
  docs:                           # a custom stdio server, mcpServers style
    command: npx
    args: ["-y", "@acme/docs-mcp@1.4.2"]
    env: {DOCS_TOKEN: "${secret:DOCS_TOKEN}"}
  tracker:                        # a custom http server
    url: https://mcp.example.com/mcp
    headers: {Authorization: "Bearer ${secret:TRACKER_TOKEN}"}

agents:                           # required; at least one
  developer:                      # the agent's slug; tasks name it
    name: Developer               # default: the slug, capitalised
    role: |                       # default: empty; the last part of the agent's system prompt,
      You are a software developer on this team. ...   # and what routing reads to pick an agent
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
Service names may not be `api`, `workstation`, `localhost`, anything starting with
`exradius`, or a repository folder name, and are at most 55 characters (a run's container is
`service-<name>`). The connection name `exradius` is reserved (every agent already
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
tree hash, and rebuilds only when the context changes. It builds with rootless BuildKit in
the cluster, with a layer cache in its registry, for the cluster's CPU architecture
(`amd64` or `arm64`: ask the admin). Multi-stage builds, `COPY --from=<image>`, heredocs
and cache mounts work; build secrets (`RUN --mount=type=secret`) and SSH mounts are not
given. A build reaches any public host through the egress proxy, and no private address.
Images named with `image:` are pulled by the cluster's nodes, so they must be reachable
from them (a private registry needs the install's pull secret) and exist for their
architecture.

**Memory.** `resources.memory` (the workstation: the setup commands and the agent's
session) and a service's `memory` are sizes: a number and one of `b`, `k`, `m`, `g`
(`512m`, `6g`, `1.5g`; binary units, upper case works too), at least `6m`, always with a
unit (`memory: 512` is refused: there is no unit-less form). Exradius runs on Kubernetes,
and a declared memory is the container's request and limit:

- **Placement.** The cluster puts the run's pod on a node with that much memory free. When
  no node has room, the task waits in `preparing`, shown as "waiting for cluster capacity"
  in the panel, and starts when another run ends.
- **Limit.** A container that exceeds it is killed by its own limit, not by the node
  killing a neighbour; the run fails with "exceeded its declared memory (6g)".

Undeclared means the install's small default request and no limit: the run competes for
the node's memory with every other run. Verify is the same pod, so a limit that is too low
shows up in Verify, not in the first task. Declare the peak of the heaviest step (the
dependency install or a type check, usually) with some headroom, and less than a cluster
node has.

**Caches.** `workstation.cache` lists folders inside the container that survive from run
to run of the project: `~/...` (in the run's home, `/home/runner`) or absolute paths,
never inside `/work`, `/exradius`, `/opt/exradius`, `/proc`, `/sys` or `/dev`, never `~`,
`/`, `/home`, `/tmp`, `/etc`, `/usr`, `/bin`, `/lib`, `/var` or `/opt` themselves, no
`..`, each listed once. Exradius keeps each as a folder of the project's volume (created on
first use, owned by uid 1000) and mounts it into the setup commands and the agent's
container, of tasks and Verify. The same folders are shared by every run of the project,
concurrent ones included, so list only caches whose tool is safe with concurrent writers
(the package managers' download stores are: pnpm, npm, Yarn, uv, pip, Maven, Gradle, Go,
Cargo). Nothing cleans them; the admin deletes the project's volume to reset them. Hard
links do not cross mounts, so tools that link from their store (pnpm, uv) copy instead:
still no download. A cache is per project, so a
run can only affect later runs of the same project. The paths per package manager are in
pitfalls.md.

**Network.** `network.allow` lists hosts the agent may reach: host names (`pypi.org`),
`*.name` for every name below it (`*.pythonhosted.org`, which does not include
`pythonhosted.org` itself), or IPv4 addresses. Not URLs, not `host:port` (every port of an
allowed host is allowed), not a bare `*`, not `localhost` or `127.*` (the run's own
services need no entry), each listed once. A run's requests all go through Exradius's
egress proxy. During setup (cloning, the setup commands, image builds) it lets any public
host through. In the agent phase it lets through only the agent's model, the project's
repositories (git only), the agent's connections (a catalog entry's own hosts, a custom
`url`), what the install allows for every project, and this list; anything else is a `403`
that names the host. Private addresses are refused in every phase unless the install
allows them. The agent's environment section states the list. What belongs on it and how
to find the hosts: network.md.

**Services.** A run's services and the agent's container share one network: each service
is reached by its name, which points at `127.0.0.1`, and listens on the same `localhost` as
the agent, as on a developer's machine. So a port belongs to one service (two that listen on
the same port collide, declared or not), and an app configured for `localhost:5432` also
works. `ready` gives exactly one of `command` (a list, run inside the service container;
exit 0 means ready) or `tcp` (a port the cluster connects to on the run's own address, not
`127.0.0.1`: a service that listens only on 127.0.0.1 never passes a `tcp` check and needs
a `command`). Without `ready`: the image's HEALTHCHECK when it has one, else ready once its
process runs. Each service has 120 seconds from its start to become ready; one that exits,
restarts or times out fails the run with its log tail. One that crashes after it was ready
is restarted.
`command` (a list of strings, at least one; quote numbers, `"6380"`, and write an empty
argument as `""`) is the container's command, as compose's `command:` and `docker run
<image> <args>`: it replaces the image's `CMD` and keeps its `ENTRYPOINT`. There is no
`entrypoint` key; an image whose entrypoint must change needs a service Dockerfile.
Secrets are not allowed in `command`; put them in the service's `env` and refer to them as
`$NAME` inside a `sh -c` command.

Services run as their image's user with all capabilities dropped except CHOWN, SETUID,
SETGID, FOWNER, DAC_OVERRIDE and NET_BIND_SERVICE, `no-new-privileges`, no mounts and no
published ports. Service images may be Alpine; only the workstation must be glibc-based.

**Connections.** Exactly one of `catalog`, `command` or `url`. `config` goes only with
`catalog`; `args` and `env` only with `command`; `headers` only with `url`. `type` is
accepted for `mcpServers` compatibility and ignored. Catalog entries and their config keys
(required unless marked optional):

| catalog      | config keys                          | runs with |
|--------------|--------------------------------------|-----------|
| `github`     | `token`                              | http (GitHub's hosted server) |
| `gitlab`     | `token`, `api_url` (optional)        | `npx` |
| `slack`      | `bot_token`, `team_id`               | `npx` |
| `sentry`     | `token`                              | `npx` |
| `linear`     | `api_key`                            | http (Linear's hosted server) |
| `jira`       | `url`, `username`, `api_token`       | `uvx` |
| `fetch`      | none                                 | `uvx` |
| `playwright` | none                                 | `npx` |
| `filesystem` | `path` (optional, default `/work`)   | `npx` |

There is no `postgres` entry. A connection to the project's database is a custom one, for
example `npx -y @bytebase/dbhub@1.5.0 --transport stdio --config /work/<repo>/dbhub.toml`
with `env: {DSN: ...}` and a `dbhub.toml` in the repository whose `execute_sql` tool has
`readonly = true` (its `dsn` can be `"${DSN}"`). It reaches a database in `services`.

A connection that runs with `npx` or `uvx` needs that launcher in the workstation image
(the default image has neither; Exradius warns). The catalog pins each package to an exact
version. A custom `npx` or `uvx` connection should too (`npx -y <package>@1.2.3`,
`uvx <command>@1.2.3`, or `uvx --from '<package>==1.2.3' <command>`); without one, each
session runs the latest release with the connection's secrets, and Exradius warns.

**Secrets.** `${secret:NAME}` (NAME of letters, digits, `_`, `.`, `-`) may appear only in
string values under `repositories[].credential`, `services.*.env`, `env`, and
`connections.*` `config`, `env` and `headers`. Anywhere else it is an error. The set of
names referenced is the project's **required keys**; nothing declares them separately.
Each resolves from the store named by `secret_store` when a run starts, if the admin
allowed this project to use that store. A project whose store lacks a key cannot run (tasks
are rejected with "missing secret NAME in store S"), nor one whose store does not allow it
("secret store S is not allowed for this project"); while an external store such as Vault
cannot be reached, tasks are rejected with "secret store S is unreachable: ...". Whether the
store keeps its values in Exradius or in Vault is the admin's business: the file only names
it.
The workstation also receives every secret that a service's env uses, under its own name
(so `$DB_PASSWORD` is set in the workstation when `services.postgres.env` references
`${secret:DB_PASSWORD}`). Repository credentials and connection values are used only where
they are referenced. Secret values never appear in the agent's prompt, the panel or logs.

**Repositories.** Cloned with their own `credential`; they never fall back to the
credential Exradius uses for reading the config repository. `push: true` lets agents push
with that credential; the agent is told, per repository, whether pushing is allowed. Work
an agent does not push is lost when the task ends (Exradius reports unpushed commits but
never commits or pushes for the agent). URLs must be `http(s)://`: git reaches a
repository through the egress proxy, which adds the credential, and an SSH remote
(`git@host:path`, `ssh://`) cannot go that way. No pod holds the credential, so
`git remote -v` inside a run shows the proxy's route rather than the URL. Submodules and
Git LFS objects are not fetched by the clone; a setup command does that when the project
needs them (`git submodule update --init`, `git lfs pull`), for public ones.

**Agents.** The agent's system prompt is Claude Code's preset, Exradius's operating
rules, a generated "your environment" section, then `role`.

- The operating rules are the same for every agent of every project. They already say:
  each folder of the project directory is a git clone; work on a branch whose name starts
  with the task id; push to `origin` when pushing is allowed, otherwise attach a patch;
  ask through `request_information` only what a person alone can answer; end with
  `submit_result`; nobody is watching, so decide and act; the directory is discarded
  afterwards.
- The environment section is generated from the file on every run: the image, each
  repository with its branch and whether pushing is allowed, each service with its host
  name, ports and non-secret settings (secrets as `$NAME`), the network line, then
  `notes` quoted as the maintainers' notes.
- So `notes` and `role` carry only what neither of those says: `notes` what is true of
  the project for every agent (how to run the checks, where things are, what cannot run
  here), `role` what this agent is for and when its work is done.

A task names the project and usually an agent. When it names none, Exradius picks one
from each agent's `name`, `role` and the tools of its `connections` (as the last Verify
listed them), so a role's first sentence must say which tickets the agent takes. An agent
named by a task and missing from the current file rejects the task.

## Bridge files

A bridge connects a tracker to Exradius (it is separate software the admin installs).
What a bridge needs to know about this project is a file of its own, next to
`exradius.yaml`:

```
exradius.yaml
bridges/
  jira.yaml
```

(`.exradius/bridges/` when the config is `.exradius/exradius.yaml`.) The file's name is
the bridge's name, `[a-z][a-z0-9-]*`; its content is YAML with a mapping at the top, at
most 64 KiB, which Exradius passes to the bridge as written. Exradius does not know the
keys and does not resolve `${secret:...}` there: no secrets in bridge files. A malformed
file is a warning on the project, never an error.

- **Jira** (`bridges/jira.yaml`): `projects: [PLANE]`, the Jira space keys
  (`[A-Z][A-Z0-9_]*`, at least one) whose issues belong to this project. It is the only
  key; unknown keys are errors the bridge reports. A space belongs to one Exradius
  project.
- **GitHub issues**: no file. The bridge finds the project from the issue's repository
  and offers one `agent: <Name>` label per agent of the project.

Write a bridge file only when the developer says the team uses that bridge.

## What the admin does with the file

1. Once per git host: give Exradius a credential to read config repositories (only for
   private ones).
2. Per project: paste the config repository URL (and a branch or path when not the
   defaults).
3. Per store: create the secret store the file names, allow the project to use it, and
   fill every required key (the panel accepts a pasted `.env` block for several keys at
   once), or point the store at the Vault path whose fields hold them.

Then Verify runs, by itself after every new commit of the config: "Read config" (commit,
errors, warnings, missing keys), "Build workstation" and "Build service <name>", "Prepare
workspace", "Clone <repo>" for each repository, "Start service <name>", "Setup:
<command>" for each setup command, and "List tools <name>" for each connection an agent
uses. A failure gets a diagnosis (verify-failures.md). Verify does not run the tests or
the agent.
