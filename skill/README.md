# The Exradius setup skill

A Claude Code plugin with one skill, `exradius-project`, that puts a codebase on
[Exradius](#what-exradius-needs-from-a-project), the self-hosted harness that runs AI
agents on a team's repositories. On the developer's machine it:

1. reads the code (languages, tool versions the lockfiles demand, package registries,
   services from compose and CI, the checks as CI runs them, secret names from tracked env
   examples), trusting CI over the README where they disagree;
2. asks the few things it cannot infer, in one round (other repositories, whether agents
   push and open pull requests, where the config lives, where tickets come from), and
   states the rest as assumptions;
3. writes `exradius.yaml` (config **version 1**) with notes and agent roles an agent can
   work from, `docker/workstation.Dockerfile` when the stock image is not enough, a
   bridge file when the team's tracker needs one, and a `README.md` for the Exradius admin
   listing every secret key with its format and where to get it, and what only the
   install can provide;
4. checks it locally with Docker: the steps of Exradius's Verify, then the project's own
   test and lint commands as an agent would run them, with the agent's network;
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
already present it runs in update mode: it diffs the code against the config (new
services, tool versions, repositories, secret names) and the config against what Exradius
offers now (a file from before 0.4.0 has no `network.allow`), proposes the edits, checks
and pushes.

Requirements on the developer's machine: Docker (Desktop or Engine) running, git, Python
3.8 or later (standard library only), and optionally the `gh` CLI to create the
repository. The check pulls `python:3.13-alpine` once, for its proxy (`--proxy-image`
names another image with Python).

## The local check on its own

`skills/exradius-project/scripts/check.sh` runs without Claude:

```bash
skills/exradius-project/scripts/check.sh --repo myapp=../myapp \
  --run 'cd myapp && npm test -- src/health.test.ts' ../exradius-myapp
```

Steps, in Verify's order:

| step | what it does |
|---|---|
| Read config | parses `exradius.yaml` (or `.exradius/exradius.yaml`) with the same strict rules as Exradius, lists the required secret keys, warns about uncommitted or git-ignored files the server would not see |
| Build workstation / Build service *name* | `docker build` of each Dockerfile from its declared context with BuildKit, as Exradius builds in the cluster; `image:` entries are pulled when missing |
| Check workstation image | glibc (no Alpine/musl), `bash`, `git`, and the tools found on PATH, as uid 1000 on a read-only root |
| Check connections | each stdio connection an agent lists has its command (`npx`, `uvx`, a custom one) in the workstation image, as Verify's "List tools" needs |
| Start egress proxy | a Docker network without a route out, and a small proxy on it: the stand-in for Exradius's egress proxy, open during setup, limited to the agent's hosts afterwards (`--open-network` skips it) |
| Prepare /work | a fresh clone of each local checkout (`--repo NAME=PATH`, `--repos-dir DIR`, or the config's own repository for `.exradius/`), else a clone from the URL; `--live` mounts the checkouts as they are |
| Start service *name* | in the run's network namespace, as a sidecar of the run's pod: a holder container owns it, with every service name as `127.0.0.1` in its `/etc/hosts`, and each service joins it (`--network container:`), with the operator's capabilities, its `command` and `memory`, and its `ready` check (`command`, `tcp` against the pod's own address, image HEALTHCHECK, or running), 120 s from its start |
| Setup: *command* | `/bin/sh -c` in the workstation with `--user 1000:1000 --read-only --cap-drop ALL --security-opt no-new-privileges`, generated `/etc/passwd`/`/etc/group` (`runner`, home `/home/runner`), `/work`, home and `/tmp` as writable bind mounts, `HOME=/home/runner`, the project `env` with one throwaway value per `${secret:NAME}`, the proxy variables Exradius sets (`HTTPS_PROXY`, `YARN_HTTPS_PROXY`, `JAVA_TOOL_OPTIONS`, ...), in the same network namespace (services on `localhost` by name); `resources.memory` as `--memory` and `--memory-swap`; each `workstation.cache` folder mounted from `.exradius-cache/` next to `exradius.yaml` |
| Agent phase: *command* | each `--run` command, after setup, in the same workstation conditions, with the network limited to `network.allow`, the connections' hosts and `--allow` hosts: the project's test and lint commands as an agent would run them |
| Network | from the proxy's records: `reached in setup`, `reached in setup only` (what the agent cannot reach), `refused in the agent phase`, repository hosts something other than git may have used, and private addresses the admin's install must allow |
| Clean up | removes the containers, the network and the temporary folders (`--keep` keeps the folders); built images and `.exradius-cache/` stay |

Exit status 0 when every step passed, 1 with the first failing step and its output
otherwise, 2 for usage errors. `--validate` only reads the file (no Docker),
`--platform linux/amd64` (or `arm64`) checks the cluster's architecture when it differs
from the machine's, `--allow HOST` and `--allow-private HOST` stand for what the admin's
install allows (`egress.allow`, `egress.allowPrivate`), `--cache-dir DIR` keeps the cache
folders elsewhere (delete the folder to start cold), `-v` streams output, `--help` lists
the rest. The skill adds `.exradius-cache/` to the config repository's `.gitignore`.

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
| 0.4.0 | version 1, with `network.allow`; checked with the run's network and the project's own check commands |
| 0.3.0 | version 1, checked the way Exradius 0.2.0 (on Kubernetes) runs it |
| 0.2.0 | version 1, with `resources`, service `memory` and `command`, `workstation.cache` |
| 0.1.0 | version 1 |

A change of the file's schema in Exradius is followed by a release of this plugin.

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
  references/                     schema, run conditions, network, pitfalls, Verify failures,
                                  examples, admin README
  scripts/check.sh                the local check (wraps exradius_check.py)
  scripts/exradius_check.py       standard-library Python: YAML reader, validator, Docker steps,
                                  the local egress proxy
```
