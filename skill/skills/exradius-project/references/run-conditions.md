# The conditions a workstation must survive

Every setup command and every agent session runs in a container started from the
workstation image with these conditions. A workstation that works on a laptop as root can
still fail here; `scripts/check.sh` reproduces all of them locally.

From the inside, a run looks like a developer's own workstation: a normal user with a
home, the project's tools, its services by host name, and a network that reaches what
the project declared. Exradius deviates only where security requires it, and enforces each deviation at the level of the kernel,
Kubernetes or the command line, where no project setting, tool or agent can change it.
Exradius runs each task as one Kubernetes pod: the setup commands, the services and the
agent's container are containers of that pod.

## Three limits to know before anything else

1. **No Docker daemon inside a run.** Nothing in the workstation can start containers:
   no `docker`, no `docker compose`, no Testcontainers, no kind or minikube. Tests that
   start their own containers cannot run there. Model each dependency they start as a
   `services` entry (same image, same env) and point the tests at it by host name;
   tests that only work by starting containers themselves stay for CI, and the notes
   should say so.
2. **A read-only root filesystem.** At run time nothing can be installed outside the
   home, `/work` and `/tmp`: no `apt-get`, no `npm install -g`, no `pip install` into the
   system, no `corepack enable`, no `sudo`. Everything a run needs that is not a project
   dependency goes into the workstation Dockerfile; project dependencies are installed by
   the setup commands into `/work` and the home.
3. **The agent's network is an allowlist.** Setup commands reach any public host. The
   agent reaches only its model, the project's repositories (git only), its connections
   and the hosts in `network.allow`. What setup did not download, the agent can download
   only from a listed host (network.md).

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
- Built in the cluster with **rootless BuildKit**: multi-stage builds, `COPY --from=<image>`,
  heredocs and cache mounts work; build secrets and SSH mounts are not given.
- Built for the **cluster's CPU architecture**, `linux/amd64` or `linux/arm64` (cloud
  installs are often Arm): the admin knows which. The local check builds the developer
  machine's own architecture (`arm64` on Apple Silicon); when the two differ,
  `check.sh --platform linux/<arch>` builds and runs the cluster's under emulation
  (slower, but it catches a Dockerfile that downloads a binary for one architecture
  only, and a service image that does not exist for the other).

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
  - `/tmp`: the run's temporary directory (mode 1777), empty at the start;
  - each `workstation.cache` folder (schema.md), mounted at its path from the project's
    volume, which outlives the run: the same folders in every run of the project,
    concurrent ones included.
- `/work`, the home and `/tmp` are directories **on the run's disk volume, not in memory**
  (an ordinary Linux filesystem: Unix sockets, hard links within one of them and file
  locks work; the local check uses Docker volumes for them for the same reason):
  package manager caches (`~/.cache/yarn`, `~/.npm`, `~/.cache/uv`, `~/.m2`,
  `~/go/pkg/mod`), Yarn Berry's staging in `/tmp` and build output take disk space. They
  persist from the setup commands to the agent session and across a resumed session of
  the same task, and are removed when the task ends; a download cache listed in
  `workstation.cache` is kept instead, so the next run does not download it again.
- **Hard links do not cross bind mounts.** `/work`, the home, `/tmp` and each cache folder
  are separate mounts, so a tool that hard-links files from its store into the project
  (pnpm into `node_modules`, uv into `.venv`) copies them instead. pnpm and uv fall back to
  copying by themselves (uv prints a warning; `UV_LINK_MODE=copy` in `env` silences it).
  The copy still saves the download; it costs disk in `/work` as on a machine without
  hard links.
- `/work/.exradius/attachments` is a read-only folder with the task's attachments.
- Anything the image puts elsewhere (`/opt`, `/usr/local`) is readable but not writable at
  run time. Prepare shared tool state (corepack's cache, uv's Python installs, a Maven
  repository) in the Dockerfile under a world-readable path (`chmod -R a+rX`) and point
  the tool at it with an `ENV`, or copy it into the home in a setup command.
- Repositories are cloned as uid 1000 from mirrors on the project's volume, before the
  services and the setup commands. Git runs with
  `safe.directory=*` (set through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0`),
  so ownership warnings do not occur. The agent's git identity and push credential are
  configured by Exradius.

## The environment

- The project's `env`, with `${secret:NAME}` resolved from the store.
- Every secret a service's `env` references, under its own name.
- `HOME=/home/runner`, `EXRADIUS_WORK_DIR=/work`, `EXRADIUS_STATE_DIR` (the agent
  runtime's state).
- The image's own `ENV` lines (`PATH`, `JAVA_HOME`, `COREPACK_HOME`, ...).
- The proxy variables Exradius adds after the project's own: `HTTP_PROXY`, `HTTPS_PROXY`,
  `NO_PROXY` (and lower case), `YARN_HTTP_PROXY`, `YARN_HTTPS_PROXY`, and
  `JAVA_TOOL_OPTIONS` with Java's proxy properties (appended to the project's own value).
  Do not set or unset them in `env`. `NO_PROXY` names `localhost`, `127.0.0.1`, `::1` and
  the services, not the run's own address (network.md). Services get the same variables.
- No model key, no repository credential, no token of Exradius: the egress proxy adds
  those outside the pod. The secrets in the environment are the ones `env` and the
  services' `env` reference, and the agent can read them.

## Memory

- `resources.memory` is the workstation's request and limit, and a service's `memory` the
  service's. A process that goes over it is killed (exit 137) and the run fails with
  "exceeded its declared memory". Without a declaration there is no limit: the run
  competes for the node's memory with every other run.
- The cluster places the run where its declared memory is free; when no node has room the
  task waits as "waiting for cluster capacity" until another run ends.
- Memory is used by processes only: the home, `/tmp` and the caches are on disk.

## The network

- Every request leaves through Exradius's egress proxy; nothing has a direct route out
  (network.md). Setup reaches any public host; the agent only its model, the
  repositories, its connections and `network.allow`. Private addresses are refused in
  every phase unless the install allows them. Only HTTP and HTTPS leave the pod.
- The services and the workstation share one network namespace, the pod's: each service
  is reached by its name, which resolves to `127.0.0.1` (`postgres:5432` and
  `localhost:5432` are the same), as compose publishes ports on a developer's machine.
  Two services cannot listen on the same port. Parallel runs never share services: each
  run is its own pod.
- No ports are published and nothing outside reaches the run, no Docker daemon is
  available inside the workstation (the first limit above), and the run has no Kubernetes
  credentials.
- Services start before the setup commands and stay up for the whole task. They run as
  their image's user with a short list of capabilities (schema.md): an entrypoint that
  needs more exits at start (pitfalls.md).
- Public names resolve; a connection that does not go through the proxy is dropped.
- Nothing starts the application. The agent does, in its own shell, when a task needs
  it, with what setup and `notes` give it (running-the-app.md).

## The order of a run (and of Verify)

1. Read `exradius.yaml` at the branch head; refuse the task if it has errors or the store
   lacks a key.
2. Build the workstation and service images that come from Dockerfiles (cached by
   content), pull the others. Wait for a node with room for the declared memory.
3. Clone each repository into `/work/<name>` at its branch, through the proxy's git
   route (no submodules, no LFS objects).
4. Start the services in order (with their `command` and `memory`); wait for each to be
   ready (120 seconds from its start).
5. Run each setup command in its own container from the workstation image, under the
   conditions above (cache folders mounted, memory limit applied), cwd `/work`. A non-zero exit fails the run with the output's tail.
   There is no time limit per command, but every run pays for them, so keep them to
   what the agent needs (dependency installs, database creation, cache copies).
6. Switch the network to the agent phase, then start the agent in the same conditions
   (the same `/work`, home, `/tmp` and caches the setup commands left); the runtime is
   mounted read-only at `/opt/exradius/runtime`. Verify lists each connection's tools
   here instead.

## What the local check does not cover

The check reproduces the image, the user, the filesystem, the memory limits, the services
and the two network phases, and runs the commands given with `--run` and `--run-file` as
the agent would. Each of those is its own container: what one leaves running does not
reach the next, where in a run the agent's shell keeps it. It does not cover: the agent runtime and the model; pushing, and git's credential route;
the connections' tool lists (it only checks that each launcher is in the image); the
cluster's CPU architecture unless `--platform` is given; what the admin's install allows
(`egress.allow`, `egress.allowPrivate`) unless given with `--allow` and
`--allow-private`; image builds' network (they use Docker's own); name resolution (the
check resolves no public name, a run does); the cluster's disk (a storage class that is
a network file share may not hold Unix sockets). Only a Verify in Exradius, then a real
task, covers those.
