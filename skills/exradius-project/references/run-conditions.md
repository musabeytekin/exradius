# The conditions a workstation must survive

Every setup command and every agent session runs in a container started from the
workstation image with these conditions. A workstation that works on a laptop as root can
still fail here; `scripts/check.sh` reproduces all of them locally.

## The image

- **Any glibc-based Linux image**: Debian, Ubuntu, the official `node`, `python`, `maven`,
  `eclipse-temurin`, `golang`, `rust` images (their default Debian variants), or a
  company image. **Alpine and other musl images are not supported** for the workstation
  (the agent runtime Exradius mounts into it needs glibc). Services may be Alpine.
- **`bash`, `git` and `/bin/sh`** must be on the image. Setup commands run as
  `/bin/sh -c '<command>'`.
- No Exradius base image or label is needed. Without a workstation the project gets
  Exradius's default image: Ubuntu 24.04 with git, curl, ca-certificates and
  build-essential. Nothing inherits from it; write a Dockerfile for anything more.
- Built by the Exradius worker with the **classic builder** (no BuildKit): `RUN --mount`,
  heredoc `RUN <<EOF` and `# syntax=` lines fail. Multi-stage builds and
  `COPY --from=<image>` work.
- Built for the **server's CPU architecture** (usually `linux/amd64`). A Mac with Apple
  Silicon builds `linux/arm64` locally; `check.sh --platform linux/amd64` builds and runs
  the server's architecture under emulation (slower, but catches downloads of
  architecture-specific binaries).

## The user

- **uid 1000, gid 1000**, whatever users the image defines.
- `/etc/passwd` and `/etc/group` are **replaced** (read-only bind mounts) by:

  ```
  root:x:0:0:root:/root:/bin/sh
  runner:x:1000:1000::/home/runner:/bin/bash
  ```

  and `root:x:0:` / `runner:x:1000:`. The user is named `runner`, its home is
  `/home/runner`, and every other user of the image (`node`, `ubuntu`, ...) disappears.
  Tools that read the home from passwd instead of `$HOME` (Java's `user.home`, so Maven's
  `~/.m2`, Gradle's `~/.gradle`) therefore see `/home/runner` too.
- `HOME=/home/runner`.
- **No root, no sudo, no capabilities** (`--cap-drop ALL`), `no-new-privileges`. Nothing
  can be installed with `apt-get` at run time; it belongs in the Dockerfile.

## The filesystem

- **The root filesystem is read-only.** Writable are only:
  - `/work`: the project directory, one folder per repository (`/work/<name>`), the
    working directory of setup commands and of the agent;
  - `/home/runner`: the run's home, empty at the start of a run;
  - `/tmp`: the run's temporary directory (mode 1777), empty at the start.
- All three are directories **on the server's disk, not in memory**: package manager
  caches (`~/.cache/yarn`, `~/.npm`, `~/.cache/uv`, `~/.m2`, `~/go/pkg/mod`), Yarn Berry's
  staging in `/tmp` and build output take disk space. They persist from the setup
  commands to the agent session and across a resumed session of the same task, and are
  removed when the task ends.
- `/work/.exradius/attachments` is a read-only folder with the task's attachments.
- Anything the image puts elsewhere (`/opt`, `/usr/local`) is readable but not writable at
  run time. Prepare shared tool state (corepack's cache, uv's Python installs, a Maven
  repository) in the Dockerfile under a world-readable path (`chmod -R a+rX`) and point
  the tool at it with an `ENV`, or copy it into the home in a setup command.
- Repositories are cloned by the Exradius worker and handed to uid 1000. Git runs with
  `safe.directory=*` (set through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`),
  so ownership warnings do not occur. The agent's git identity and push credential are
  configured by Exradius.

## The environment

- The project's `env`, with `${secret:NAME}` resolved from the store.
- Every secret a service's `env` references, under its own name.
- `HOME=/home/runner`, `EXRADIUS_WORK_DIR=/work`, `EXRADIUS_STATE_DIR` (the agent
  runtime's state).
- The image's own `ENV` lines (`PATH`, `JAVA_HOME`, `COREPACK_HOME`, ...).

## The network

- Outbound network access is available (package registries, git hosts).
- Services run on a network of their own per run; the workstation joins it and reaches
  each service by its name (`postgres:5432`). No ports are published, no Docker daemon is
  available inside the workstation, and there is no Kubernetes.
- Services start before the setup commands and stay up for the whole task.

## The order of a run (and of Verify)

1. Read `exradius.yaml` at the branch head; refuse the task if it has errors or the store
   lacks a key.
2. Build the workstation and service images that come from Dockerfiles (cached by
   content), pull the others.
3. Clone each repository into `/work/<name>` at its branch.
4. Start the services; wait for each to be ready (120 seconds each).
5. Run each setup command in its own container from the workstation image, under the
   conditions above, cwd `/work`. A non-zero exit fails the run with the output's tail.
   There is no time limit per command, but every run pays for them, so keep them to
   what the agent needs (dependency installs, database creation, cache copies).
6. Start the agent in the same conditions; the runtime is mounted read-only at
   `/opt/exradius/runtime`.

## What the local check does not cover

The agent runtime itself, providers and models, pushing with the repository credential,
MCP connections, the server's network policies, and its CPU architecture unless
`--platform` is given. Only a Verify on the server, then a real task, covers those.
