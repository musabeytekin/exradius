# Runtime bundles

Status: agreed design, 2026-11-05. Companion to `architecture.md` and `providers.md`.
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

### 2.2 The worker mounts it

At start, and when it first sees a runtime version it has not extracted, the worker
creates a Docker volume `exradius-runtime-<runtime>-<version>`, copies the bundle into it
from the bundle image, and marks it ready. Every run container of that runtime gets the
volume mounted read-only at `/opt/exradius/runtime`. The entrypoint is
`/opt/exradius/runtime/bin/exradius-runner`.

The volume is per version, so a worker on a newer version never changes what a running
container sees. Old volumes are removed when no run references them and they are not the
current version.

### 2.3 The project image is any Linux image

- A project names an image, for example `rust:1.82`, `maven:3-eclipse-temurin-21`,
  `node:22`, `python:3.14`, or something the company maintains.
- Requirements: glibc-based Linux, `git` on PATH. Alpine and other musl images are not
  supported; the project form says so in its help text.
- Projects with no image get `exradius-project-default:<version>`: Ubuntu LTS with git,
  curl, ca-certificates and build-essential. Nothing inherits from it; it is a default,
  not a base.
- The `exradius.runner` image label and the "must be built FROM exradius-runner" rule are
  removed. `GET /api/system/images` returns every image on the daemon (tag, size,
  created) as suggestions; it no longer filters by label.
- Setup commands run in the project image as before, without the runtime mounted (they
  do not need it).

### 2.4 The run user

The bundle is world-readable and executable, so the unprivileged run user can execute it
regardless of the project image's own users. Home, `/tmp` and the state mount stay as
they are. `CLAUDE_CONFIG_DIR` still points into the state mount.

### 2.5 The api and worker services

Unchanged: they keep their own image (`exradius-backend`), which no longer needs Node or
the Agent SDK. The worker image needs the Docker SDK and git only. `deploy/runner.Dockerfile`
is replaced by `deploy/backend.Dockerfile`, `deploy/runtime-claude-code.Dockerfile`
(builds the bundle) and `deploy/project-default.Dockerfile`.

### 2.6 Provider tests and verification

- The `oauth_token` provider probe runs in the default project image with the Claude Code
  bundle mounted, like any run.
- Project verification runs setup commands in the project image, as before.

## 3. Agent, project and runtime

- `agents.runtime` already exists; the agent form gets a runtime pick list ahead of the
  provider one, hidden while there is one runtime, with the provider list filtered by
  what that runtime accepts (`GET /api/system/runtimes`).
- `projects.image` stays a single free image reference (nullable = default image).
- The earlier idea of a per-runtime image map on projects is dropped.
- `GET /api/system/status` workers gain `runtimes: [{name, version, ready}]` from the
  heartbeat, so the overview can show that the bundle is extracted; `runner_image_ok`
  is replaced by that.

## 4. Build and release

- `make bundle-claude-code` (or the uv equivalent) produces the bundle in
  `deploy/build/runtime-claude-code/` and the Dockerfile copies it in.
- The native Claude Code binary is downloaded at build time from Anthropic's installer
  endpoint and pinned to a version in the Dockerfile.
- Compose builds all three images. `EXRADIUS_RUNTIME_IMAGE_CLAUDE_CODE` and
  `EXRADIUS_PROJECT_DEFAULT_IMAGE` settings allow overriding the tags.

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

The runner writes only to the state mount and `/tmp`. The project image must provide
`bash` (Claude Code's shell tool uses it) and `git`; Debian-based official images have
both.
