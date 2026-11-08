# Runtime bundles

Status: agreed design, 2026-11-05; §2.2, §2.4, §2.5 and §4 rewritten for Kubernetes on
2026-11-08 (`kubernetes.md` §7.2, §7.3). Companion to `architecture.md` and `providers.md`.
Supersedes the "project images must be built FROM exradius-runner" rule in
`architecture.md` §5.2 and §10.

## 1. Why

The agent decides how it runs: its runtime and its provider. The project decides only the
code and the toolchain needed to build and debug it. Today every project image must
inherit from `exradius-runner`, which bakes one runtime (Claude Code: Python, Node, the
Agent SDK) into every project. A Java or Rust project needs a custom Dockerfile, and every
Exradius release forces a rebuild of every project image. With a second runtime the model
breaks entirely.

The fix is the one CI systems use: the runtime is **mounted into** the run container, and
the project image is any Linux image.

## 2. The model

### 2.1 A bundle per runtime

Each runtime ships a self-contained directory, the **bundle**, that depends on nothing in
the project image except glibc:

```
<bundle>/
  bin/exradius-runner        standalone build of the runner (own interpreter, SDK included)
  bin/claude                 native Claude Code binary (no Node)
  VERSION                    exradius version + runtime name
```

For Claude Code, the runner is the existing `exradius.runner` package packaged as a
standalone executable (PyInstaller or equivalent) that carries its own Python and the
Claude Agent SDK, and launches `bin/claude` from the bundle by path. No Node anywhere.

Bundles are built in the release pipeline and published as an image
(`exradius-runtime-claude-code:<version>`) whose only content is the bundle directory.

### 2.2 The pod mounts it

The bundle image (`exradius-runtime-claude-code:<version>`) is mounted into every run pod as
an **image volume** at `/opt/exradius/runtime`, read-only, in the `prepare`, `clone-*` and
`files` containers and in the workstation, but not in setup containers (they do not need it).
The operator writes the mount; the Run spec names only the image
(`spec.runtime.image`, from the worker's `EXRADIUS_RUNTIME_IMAGE_CLAUDE_CODE`). The
workstation's command is `/opt/exradius/runtime/bin/exradius-runner <run id>`.

A rebuilt bundle is a new image digest, so a running pod never sees it change; there is
nothing to extract and nothing to clean up. Image volumes need containerd 2.1 or later on the
nodes (GA in Kubernetes 1.36); kind has it. For a cloud node image without it, values
`runs.bundleMount: copy` is planned for phase 6 (`kubernetes.md` §7.2): an init container
that copies the bundle into an `emptyDir`.

The bundle also carries `git-hooks/`: the hooks dispatcher under every standard hook name
(`run-environment.md` §3), written at build time by `python -m exradius.runner.gitconfig`.
`core.hooksPath` points there through command-line git config, and the mount is read-only,
so the agent cannot edit it.

### 2.3 The project image is any Linux image

- A project names an image, for example `rust:1.82`, `maven:3-eclipse-temurin-21`,
  `node:22`, `python:3.14`, or something the company maintains.
- Requirements: glibc-based Linux, `git` on PATH. Alpine and other musl images are not
  supported; the project form says so in its help text.
- Projects with no image get `exradius-project-default:<version>`: Ubuntu LTS with git,
  curl, ca-certificates and build-essential. Nothing inherits from it; it is a default,
  not a base.
- The `exradius.runner` image label and the "must be built FROM exradius-runner" rule are
  removed. Images are pulled by the cluster's nodes; there is no daemon whose images the
  panel could suggest.
- Setup commands run in the project image as before, without the runtime mounted (they
  do not need it).

### 2.4 The run user

The bundle is world-readable and executable, so the unprivileged run user can execute it
regardless of the project image's own users. `CLAUDE_CONFIG_DIR` still points into the
state directory (`/exradius/state/claude`).

The home, `/home/runner`, and `/tmp` are the `home` and `tmp` directories of the run's
workspace PVC, mounted by subPath into setup containers and the workstation alike and owned
by the run user (`tmp` with mode 1777; `prepare` creates both). Package manager caches
(`~/.cache/yarn`, `~/.npm`, `~/.m2`, `~/go/pkg/mod`), the archives Yarn Berry stages in
`/tmp`, and build output therefore take disk, not memory, and a cache filled by a setup
command is still there for the agent and after a resume. Both go with the PVC when the Run is
deleted. The provider probe has no workspace and uses `emptyDir`s.

Project images often have their own passwd entry for uid 1000 with another home (Ubuntu's
`ubuntu:x:1000:1000::/home/ubuntu`), and tools that read the home from passwd rather than
`$HOME` (Java's `user.home`, and so Maven's `~/.m2`) then look in the wrong place. The chart
therefore creates one ConfigMap in `exradius-runs`, `exradius-run-identity`, with `passwd`
and `group`, and the operator mounts each by subPath, read-only, over `/etc/passwd` and
`/etc/group` in `clone-*`, `setup-*` and the workstation:

```
root:x:0:0:root:/root:/bin/sh
runner:x:<uid>:<gid>::/home/runner:/bin/bash
```

and `root:x:0:` and `runner:x:<gid>:` in the group file. The run still uses the configured uid
and gid (values `runs.user`, default `1000:1000`); only the names and the home change, and
other users of the image disappear for the run. With the stock `maven:3-eclipse-temurin-21`
image, `user.home` is `/home/ubuntu` without the mount and `/home/runner` with it. Services
(phase 3) keep their image's users.

Every session gets the settings Exradius needs (`includeCoAuthoredBy: false` and empty
`attribution.commit` and `attribution.pr`) as the SDK's `settings` option, which the CLI
receives as `--settings`: that outranks user, project and local settings key by key, so a
repository's `.claude/settings.json` cannot turn the co-author line back on. All setting
sources still load (the SDK's default), so the repository's `CLAUDE.md`, `AGENTS.md` and
`.claude/` commands, agents and skills are there. `CLAUDE_CONFIG_DIR` (in the state mount)
is the agent's own user settings; nothing is written there any more.

### 2.5 The api and worker services

They keep their own image (`exradius-backend`), which has no Node, no Agent SDK and no Docker
SDK: the worker talks to the Kubernetes API over HTTPS and never to a container runtime. It
needs git (config-repository mirrors) and runs as uid 1000. The operator has its own image
(`exradius-operator`, `deploy/operator.Dockerfile`).

### 2.6 Provider tests and verification

- The `oauth_token` provider probe is a Run with `purpose: probe`: the default project image
  with the Claude Code bundle mounted, like any run (`kubernetes.md` §13).
- Project verification is a Run with `purpose: verify`, the same pod as a task's: setup
  commands run in the project image, and the runner lists the connections' tools.

## 3. Agent, project and runtime

- `agents.runtime` already exists; the agent form gets a runtime pick list ahead of the
  provider one, hidden while there is one runtime, with the provider list filtered by
  what that runtime accepts (`GET /api/system/runtimes`).
- `projects.image` stays a single free image reference (nullable = default image).
- The earlier idea of a per-runtime image map on projects is dropped.
- `GET /api/system/status` workers report `runtimes: [{name, version, ready, image}]` from
  the heartbeat: the bundle image each worker is configured with.

## 4. Build and release

- `deploy/runtime-claude-code.Dockerfile` builds the bundle; `make -C deploy
  bundle-claude-code` exports the directory alone to `deploy/build/runtime-claude-code/`.
- The native Claude Code binary is downloaded at build time from Anthropic's release bucket
  and pinned to a version (with its checksum) in the Dockerfile.
- Locally, Tilt builds every image (backend, frontend, operator, runtime bundle, project
  default) and pushes it to the kind registry, and injects the bundle and default image
  references into the worker's environment; `make -C deploy/kind images` builds and pushes
  them with the version tag for a plain `helm install`. CI builds them for releases. Values
  `runtime.claudeCode.image` and `projectDefaultImage` name them.

## 5. Isolation between the runner and the project

The runner is a guest in the project's container. Two rules keep it that way:

1. **The runner cleans its own environment.** Before it starts, it removes variables that
   steer an interpreter (`PYTHONPATH`, `PYTHONHOME`, `PYTHONSTARTUP`, `VIRTUAL_ENV`,
   `NODE_OPTIONS`, `NODE_PATH`) from its own process only. Its executable carries its own
   Python and never reads the image's interpreter, site-packages or PATH.
2. **The agent gets the project's environment, not the runner's.** The bundle directory is
   never added to PATH. The SDK session is started with the environment exactly as the
   container received it, plus only the provider credential and `CLAUDE_CONFIG_DIR`. When
   the agent runs `python`, `node`, `cargo` or `mvn`, it gets the project image's tools and
   the project's variables.

The runner writes only to the workspace (the state directory, `/tmp`, the attachments in
`prepare` and `files`). The project image must provide
`bash` (Claude Code's shell tool uses it) and `git`; Debian-based official images have
both.
