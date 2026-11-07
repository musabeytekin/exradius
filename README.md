# exradius-skill

A Claude Code plugin with one skill, `exradius-project`, that puts a codebase on
[Exradius](#what-exradius-needs-from-a-project), the self-hosted harness that runs AI
agents on a team's repositories. On the developer's machine it:

1. reads the code (languages, tool versions the lockfiles demand, services from compose and
   CI, test commands, secret names from tracked env examples);
2. asks the few things it cannot infer, in one round (other repositories, agents, pushing,
   the secret store's name, where the config lives);
3. writes `exradius.yaml` (config **version 1**), `docker/workstation.Dockerfile` when the
   stock image is not enough, and a `README.md` for the Exradius admin listing every secret
   key with its format and where to get it;
4. checks it locally with Docker, the way Exradius's Verify does;
5. creates the config repository (private by default) with `gh` or a git-host MCP server,
   or prints the commands, and ends with the hand-off for the admin.

It never asks for or reads a secret value and never calls the Exradius API.

## Two limits to know first

Every Exradius run, and so every project, lives with two limits:

1. **No Docker daemon inside a run.** Tests that start containers themselves
   (Testcontainers, `docker compose` in a test script) cannot run there. The skill models
   what they start as `services` next to the agent and says which tests stay for CI.
2. **A read-only root filesystem.** Nothing can be installed at run time outside the
   home, `/work` and `/tmp`: whatever a run needs besides the project's own dependencies
   goes into the workstation Dockerfile.

## Install

In Claude Code:

```
/plugin marketplace add musabeytekin/exradius-skill
/plugin install exradius@exradius-skill
```

Or from a shell:

```bash
claude plugin marketplace add musabeytekin/exradius-skill
```

```bash
claude plugin install exradius@exradius-skill
```

Update later with `/plugin marketplace update exradius-skill`. To try a local clone
without installing: `claude --plugin-dir /path/to/exradius-skill`.

## Use

In a checkout of the project's code (or an empty folder, naming the repositories):

```
/exradius:exradius-project
```

Claude also picks the skill up from a plain request such as "set this repository up for
Exradius" or "Verify failed on the build step, here is the log". With an `exradius.yaml`
already present it runs in update mode: it diffs the code against the config (new
services, tool versions, repositories, secret names), proposes the edits, checks and pushes.

Requirements on the developer's machine: Docker (Desktop or Engine) running, git, Python
3.8 or later (standard library only), and optionally the `gh` CLI to create the
repository.

## The local check on its own

`skills/exradius-project/scripts/check.sh` runs without Claude:

```bash
skills/exradius-project/scripts/check.sh --repo myapp=../myapp ../exradius-myapp
```

Steps, in Verify's order:

| step | what it does |
|---|---|
| Read config | parses `exradius.yaml` (or `.exradius/exradius.yaml`) with the same strict rules as Exradius, lists the required secret keys, warns about uncommitted or git-ignored files the server would not see |
| Build workstation / Build service *name* | `docker build` of each Dockerfile from its declared context, with the classic builder like the server (`--buildkit` to compare); `image:` entries are pulled when missing |
| Check workstation image | glibc (no Alpine/musl), `bash`, `git`, and the tools found on PATH, as uid 1000 on a read-only root |
| Prepare /work | a fresh clone of each local checkout (`--repo NAME=PATH`, `--repos-dir DIR`, or the config's own repository for `.exradius/`), else a clone from the URL; `--live` mounts the checkouts as they are |
| Start service *name* | on a temporary network under its name, with the server's capabilities, its `command` and `memory`, and its `ready` check (`command`, `tcp`, image HEALTHCHECK, or running), 120 s timeout |
| Setup: *command* | `/bin/sh -c` in the workstation with `--user 1000:1000 --read-only --cap-drop ALL --security-opt no-new-privileges`, generated `/etc/passwd`/`/etc/group` (`runner`, home `/home/runner`), `/work`, home and `/tmp` as writable bind mounts, `HOME=/home/runner`, the project `env` with one throwaway value per `${secret:NAME}`, joined to the services' network; `resources.memory` as `--memory` and `--memory-swap`; each `workstation.cache` folder mounted from `.exradius-cache/` next to `exradius.yaml` |
| Clean up | removes the containers, the network and the temporary folders (`--keep` keeps the folders); built images and `.exradius-cache/` stay |

Exit status 0 when every step passed, 1 with the first failing step and its output
otherwise, 2 for usage errors. `--validate` only reads the file (no Docker),
`--platform linux/amd64` checks the server's architecture from an Apple Silicon Mac,
`--cache-dir DIR` keeps the cache folders elsewhere (delete the folder to start cold), `-v`
streams output, `--help` lists the rest. The skill adds `.exradius-cache/` to the config
repository's `.gitignore`.

Not covered locally: the agent runtime, providers and models, pushes with the repository
credential, MCP connections and the server's network. A Verify on the server covers those.

## What Exradius needs from a project

The skill's references are the developer documentation for Exradius projects:

- [`references/schema.md`](skills/exradius-project/references/schema.md): every key of
  `exradius.yaml` version 1, its default, and the rules;
- [`references/run-conditions.md`](skills/exradius-project/references/run-conditions.md):
  what a workstation image must survive;
- [`references/pitfalls.md`](skills/exradius-project/references/pitfalls.md): failures
  seen on real projects and their fixes;
- [`references/examples.md`](skills/exradius-project/references/examples.md): four worked
  examples (Bank of Anthos, Outline, Saleor, Plane);
- [`references/admin-readme.md`](skills/exradius-project/references/admin-readme.md): the
  README the admin receives.

## Versions

| skill (plugin) | writes `exradius.yaml` |
|---|---|
| 0.2.0 | version 1, with `resources`, service `memory` and `command`, `workstation.cache` |
| 0.1.0 | version 1 |

A change of the file's schema in Exradius is followed by a release of this plugin.

### 0.2.0

For the run environment keys Exradius added to version 1 (all optional; a file without
them means what it meant before):

- `resources.memory` and a service's `memory`: a Docker size (`6g`, `512m`) that Exradius
  reserves before starting a run and enforces as the container's limit. The skill
  estimates the workstation's peak and writes it; the check applies it as `--memory` and
  `--memory-swap` and reports a step that went over it.
- `workstation.cache`: download stores kept per project across runs (pnpm, Yarn, npm, uv,
  pip, Maven, Gradle, Go, Cargo; the paths are in `references/pitfalls.md`). The skill adds
  one per package manager it finds; the check mounts them from `.exradius-cache/`.
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
.claude-plugin/marketplace.json   this repository as a marketplace (name: exradius-skill)
skills/exradius-project/
  SKILL.md                        the flow
  references/                     schema, run conditions, pitfalls, examples, admin README
  scripts/check.sh                the local check (wraps exradius_check.py)
  scripts/exradius_check.py       standard-library Python: YAML reader, validator, Docker steps
```
