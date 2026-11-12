# Pitfalls, each with its fix

Each of these failed a real project's Verify or a real task at least once, or would have.
They are what `scripts/check.sh` exists to catch on the developer's machine. Read the
lockfiles and version files first; most failures are a tool in the image older or newer
than what the repository asks for.

## Node images ship no corepack (Yarn Berry, pnpm missing)

**Symptom:** `yarn: not found`, or Yarn 1 running against a Yarn 4 project ("This project's
package.json defines packageManager yarn@4..."), or `pnpm: not found`.

**Cause:** recent Node releases no longer bundle corepack (the `node:26` images have none),
so the `packageManager`
field in `package.json` is never honoured. Even where corepack exists, `corepack enable`
writes shims into `/usr/local/bin` and downloads the package manager into the home on
first use; at run time `/usr/local/bin` is read-only and the home is empty.

**Fix:** in the Dockerfile, install corepack, enable it, and prepare the exact version from
`packageManager` into a shared, world-readable `COREPACK_HOME`:

```dockerfile
ENV COREPACK_HOME=/opt/corepack
RUN npm install -g corepack@latest \
 && corepack enable \
 && corepack install -g yarn@4.18.0 \
 && chmod -R a+rX /opt/corepack
```

Use `pnpm@<version>` the same way. The version comes from `packageManager` in the root
`package.json` (`"packageManager": "yarn@4.18.0+sha512..."`). With npm only
(`package-lock.json`), the image's npm must support the lockfile's `lockfileVersion`
(3 needs npm 7 or later).

## uv older than the lockfile's syntax

**Symptom:** `uv sync --locked` fails with a parse error in `uv.lock` or `pyproject.toml`,
for example on `exclude-newer = "3 weeks"` (relative durations), or "unsupported lock
version", or "The lockfile needs to be updated" although the developer's own `uv` is happy.

**Cause:** the image pins an older uv than the one that wrote the lockfile. Saleor's
`pyproject.toml` uses a relative `exclude-newer`, which uv 0.9.7 cannot parse; 0.13.7 can.

**Fix:** copy a uv at least as new as the developer's (`uv --version` on their machine, or
the `required-version` in `[tool.uv]`, or the version CI installs):

```dockerfile
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /usr/local/bin/
```

Also check the `revision`/`version` line at the top of `uv.lock`, `requires-python`, and
`.python-version`. When uv manages Python, install it in the image so runs never download
it: `ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python` then
`RUN uv python install 3.14 && chmod -R a+rX /opt/uv`.

The same class of failure applies to Poetry (`poetry.lock` `lock-version`), pnpm
(`lockfileVersion` in `pnpm-lock.yaml`), Bundler (`BUNDLED WITH`), Go (`go` and `toolchain`
lines in `go.mod`), Rust (`rust-toolchain.toml`), Gradle (wrapper version) and Node itself
(`.nvmrc`, `.node-version`, `engines.node`, `.tool-versions`).

## `python3` not on PATH

**Symptom:** a setup command, a test script, node-gyp or a Maven plugin fails with
`python3: not found` (exit 127).

**Cause:** the base image has no system Python (`maven:*`, `eclipse-temurin:*`, `golang:*`),
and a Python installed by uv lives under `UV_PYTHON_INSTALL_DIR`, not on PATH.

**Fix:** link the interpreter onto PATH, as an engineer's machine has it:

```dockerfile
RUN ln -s "$(ls -d /opt/uv/python/cpython-3.14*/bin)/python3.14" /usr/local/bin/python3 \
 && ln -s /usr/local/bin/python3 /usr/local/bin/python && python3 --version
```

or `apt-get install -y python3` on Debian and Ubuntu images. The local check's "Check
workstation image" step lists the tools it finds on PATH; compare with what the setup
commands, the README and CI call.

## Tools that read the home from passwd

**Symptom:** Maven downloads everything again although `~/.m2` was prepared, or writes to
`/home/ubuntu/.m2` and fails (read-only root); Gradle or another JVM tool ignores `$HOME`.

**Cause:** Java takes `user.home` from the passwd entry of the current uid, not from
`$HOME`. Many images have their own user with uid 1000 (`ubuntu` with `/home/ubuntu` in
`maven:*` and `eclipse-temurin:*`, `node` with `/home/node` in `node:*`).

**Fix:** none needed in the image: Exradius replaces `/etc/passwd` so uid 1000 is `runner`
with home `/home/runner` (see run-conditions.md), and `check.sh` does the same. Do not
write anything for the run into the image user's home (`/home/node`, `/home/ubuntu`): it is
read-only at run time and not the run's home. Prepare caches elsewhere (`/opt/...`).

## Every run downloads all dependencies again: declare `workstation.cache`

**Symptom:** every run spends a minute or more in `pnpm install`, `yarn install`,
`uv sync`, `mvn` or `go mod download`, fetching the same packages (Plane: about 70 s of
`pnpm install`; Outline longer for `yarn install`).

**Cause:** the run's home is new and empty in every run, so the package manager's
download store is too.

**Fix:** list the store under `workstation.cache` (schema.md). Exradius keeps the folder
per project and mounts it into every run; the local check does the same from
`.exradius-cache/` next to `exradius.yaml` (git-ignored; delete it to start cold). Declare
one entry per package manager the setup commands use, at the path the tool uses as uid
1000 with `HOME=/home/runner`:

| Tool | `cache` entry | Notes |
|---|---|---|
| pnpm | `~/.local/share/pnpm/store` | also set `pnpm_config_store_dir: /home/runner/.local/share/pnpm/store` in `env` (pnpm 11; `npm_config_store_dir` for pnpm 10 and older): when pnpm thinks it cannot hard-link from the home into `/work` it puts the store at `/work/.pnpm-store`, which is deleted with the run |
| Yarn Berry (2+) | `~/.yarn/berry/cache` | the global cache (`enableGlobalCache`, the default since Yarn 4). With `enableGlobalCache: false` the cache is `.yarn/cache` inside the repository: nothing to declare |
| Yarn 1 | `~/.cache/yarn` | |
| npm | `~/.npm` | `npm ci` still deletes and rewrites `node_modules`; the downloads come from the cache |
| uv | `~/.cache/uv` | uv warns "Failed to hardlink files; falling back to full copy": set `UV_LINK_MODE: copy` in `env` to silence it |
| pip | `~/.cache/pip` | |
| Poetry | `~/.cache/pypoetry` | |
| Maven | `~/.m2/repository` | replaces copying a pre-fetched `/opt/m2` in setup |
| Gradle | `~/.gradle/caches` | and `~/.gradle/wrapper` for the wrapper's distribution; Gradle reads the home from passwd, which the run sets to `/home/runner` |
| Go | `~/.cache/go-build` (build cache) and the module cache | the `golang:*` images set `GOPATH=/go`, so the module cache is `/go/pkg/mod` (absolute, fine as an entry); elsewhere `~/go/pkg/mod` |
| Cargo | `~/.cargo/registry` and `~/.cargo/git` | the `rust:*` images set `CARGO_HOME=/usr/local/cargo`, read-only at run time: set `CARGO_HOME: /home/runner/.cargo` in `env` (cargo stays on `PATH` from `/usr/local/cargo/bin`) |

Confirm a path in the built image when in doubt, as uid 1000 from `/work`: `pnpm store
path`, `yarn config get cacheFolder`, `npm config get cache`, `uv cache dir`,
`pip cache dir`, `go env GOCACHE GOMODCACHE`. Then check it the sure way: after the first
local check, every folder under `.exradius-cache/` must have grown (the check prints the
total in "Read config"), and the second check's install must report no downloads (pnpm:
`reused N, downloaded 0`). An entry that stays empty is at the wrong path. Cache download stores only, not build
output or `node_modules`: the folders are shared by concurrent runs of the project, and
only package managers' stores are built for that. Hard links do not cross bind mounts,
so pnpm and uv copy from the cache into `/work` instead of linking: still no download,
but the disk use in `/work` is a full copy. Nothing cleans the caches; tell the admin
where they are (the admin README's resources line).

## Caches the run must own: copy them into the home in `setup`

**Symptom:** a tool insists on writing into its cache (Maven's local repository, Gradle's
caches, Go's module cache) and fails on a read-only path under `/opt`, or every run
downloads all dependencies again.

**Fix:** usually a `workstation.cache` entry (above): the first run downloads, later runs
reuse. When even the first run must not download (a slow or restricted registry),
pre-fetch into a world-readable directory in the Dockerfile, then seed the cache from it
with a setup command, once:

```dockerfile
RUN git clone -q --depth 1 https://github.com/acme/app.git /tmp/app \
 && cd /tmp/app && mvn -B -q -Dmaven.repo.local=/opt/m2 dependency:go-offline \
 && chmod -R a+rX /opt/m2 && rm -rf /tmp/app
```

```yaml
workstation:
  setup:
    - test -e ~/.m2/repository/.seeded || (cp -r /opt/m2/. ~/.m2/repository/ && touch ~/.m2/repository/.seeded)
  cache:
    - ~/.m2/repository
```

`-Dmaven.repo.local=/opt/m2` puts the artifacts at the top of `/opt/m2`, and Maven reads
`~/.m2/repository`: copy the contents there (`/opt/m2/.`), not `/opt/m2` to `~/.m2`, which
leaves them where Maven never looks. The first run copies to the cache folder on the
run's disk (seconds for a few hundred MB); the marker skips it afterwards. Without a
`cache`, drop the marker and copy in every run. Read-only caches that a
tool only reads (corepack's `COREPACK_HOME`, uv's Python installs) can stay in `/opt` with
an `ENV` pointing at them. A pre-fetch that clones the code into the image goes stale as
the code changes; it is a speed-up, never a requirement, and the setup command must still
work when dependencies changed.

## Alpine (musl) workstations

**Symptom:** the image check fails ("no glibc found" / "Alpine Linux"), or the agent runtime
does not start.

**Fix:** use the Debian or Ubuntu variant of the same image (`node:22-bookworm` instead of
`node:22-alpine`, `python:3.12-bookworm`, `eclipse-temurin:21-jdk` instead of
`-alpine`). Services can stay on Alpine images (`postgres:15-alpine`,
`valkey/valkey:8.1-alpine` are fine).

## Installs need disk, not RAM; declare the memory they do need

**Symptom:** a setup command is killed with exit 137, or the server runs out of disk
during `yarn install`, `pnpm install`, `uv sync`, `mvn` or `cargo build`.

**Cause:** exit 137 is the kernel killing the process, usually for memory. The run's home
and `/tmp` are on disk, so caches and Yarn's staging no longer use RAM, but the package
manager's own process memory remains (Outline's `yarn install` peaks around 2.3 GB) and
parallel installs of several projects add up. On disk, a large JavaScript install takes
about 0.8 GB in the home and as much again in `/tmp`.

**Fix:** keep setup to what the agent needs; reduce parallelism where the tool allows it
(`yarn config set networkConcurrency`, `pnpm --network-concurrency`, `-n 0` for
pytest-xdist in tests, `MAVEN_OPTS=-Xmx...`); declare the peak in `resources.memory` (and
a service's own in its `memory`), so Exradius places the run on a node with room for it
(it waits as "waiting for cluster capacity" until one has). A declared memory is also the
limit: a process over it is killed
and the run fails with "exceeded its declared memory (6g)", so leave headroom over the
measured peak (`docker stats` during the check). check.sh applies it the same way
(`--memory` and `--memory-swap`) and says when a step went over it. Undeclared, the run
has no limit and Docker Desktop's memory is what kills it locally; check.sh names it when
a command is killed.

## A service that needs a different command: `command`, not a Dockerfile

**Symptom:** a compose file starts a service with `command:` (MinIO's `server /data`, a
Redis with `--appendonly yes`, a database with extra flags), or with an `entrypoint:` that
runs a small bootstrap (Plane's MinIO creates its bucket), and the stock image alone does
not do it.

**Fix:** give the service a `command` (schema.md): it replaces the image's `CMD` and keeps
its `ENTRYPOINT`, like compose's `command:`. Prefer it to a service Dockerfile. When the
compose file overrides the entrypoint with a shell script, check what the image's
entrypoint does with a first argument of `/bin/sh` (`docker run --rm --entrypoint cat
<image> <entrypoint path>`): most official entrypoints (Postgres, MinIO and its forks,
Redis) run an unknown first argument as a command, so the script works as
`command: [/bin/sh, -c, "..."]`. Plane's MinIO bootstrap, from its
`docker-compose-test.yml`:

```yaml
  minio:
    image: docker.io/pgsty/silo:RELEASE.2026-09-16T00-00-00Z
    command:
      - /bin/sh
      - -c
      - >-
        mkdir -p /export/uploads
        && (silo server /export --console-address ':9090' &)
        && sleep 3
        && mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"
        && (mc mb local/uploads -p || true)
        && tail -f /dev/null
```

Secrets go into the service's `env` and are read as `$NAME` inside the script; they are
not allowed in `command`. A service Dockerfile is still needed when the entrypoint itself
must change (it ignores or rewrites the arguments), or for files the image must contain
(an extension, seed data).

## A `tcp` check on a service that listens only on 127.0.0.1

**Symptom:** "Start service cache" fails after 120 s with "did not become ready within 120s
waiting for port 6379", while the service's log says it is ready.

**Cause:** in a run, a `tcp` check is a connection to the run's own address (the
kubelet's TCP probe), not to `127.0.0.1`. A service started with `--bind 127.0.0.1` (or a
config that listens on localhost only) never accepts it. The workstation itself still
reaches it: services and the workstation share `localhost`.

**Fix:** a `ready` `command` instead (`[valkey-cli, ping]`, `[pg_isready, -h, 127.0.0.1]`),
or let the service listen on all addresses.

## Two services on one port

**Symptom:** the second of two services exits before it is ready ("Address already in
use"), or the file check refuses "5432 is also a port of service db".

**Cause:** a run's services share one `localhost`, as compose publishes them on a
developer's machine: two Postgres, or Postgres and something else on 5432, collide.

**Fix:** give the second one another port (Postgres: `command: [postgres, -p, "5433"]` and
`PGPORT`, Redis: `command: [redis-server, --port, "6380"]`) and point the app at it.

## Files that are only local

**Symptom:** the local build works, the server's build fails on `COPY some-file`.

**Cause:** Exradius builds from the committed, pushed tree of the config repository.
Untracked or git-ignored files in the build context exist only on the developer's machine.

**Fix:** commit everything the Dockerfile copies; check.sh warns about uncommitted and
ignored files in a build context.

## Service readiness too early (Postgres)

**Symptom:** the first setup command that connects to the database fails with "connection
refused" or "the database system is starting up", although the service was "ready".

**Cause:** the `postgres` image runs a temporary server on its Unix socket while it
initialises, so `pg_isready` without a host answers before the real server listens.

**Fix:** check over TCP: `ready: {command: [pg_isready, -h, 127.0.0.1, -U, app, -d, app]}`.
For Redis or Valkey, `ready: {tcp: 6379}`. For MySQL,
`[mysqladmin, ping, -h, 127.0.0.1, --silent]`.

## Secrets and env that disagree

**Symptom:** authentication to a service fails in setup.

**Fix:** use the same `${secret:NAME}` in the service's env and in the project `env` that
builds the URL (`postgres://app:${secret:DB_PASSWORD}@postgres:5432/app`). Values that are
not secret (a test-only `SECRET_KEY`, a user name, `NODE_ENV`) belong in `env` as plain
text, not in the store. Quote values YAML would read as booleans or dates.

## The repository's own `.env` files

**Symptom:** the app connects to `127.0.0.1` instead of the service, or a test-only key is
missing.

**Fix:** read `.env.example`, `.env.test`, `.env.sample` and compose files: values the
repository already supplies for tests need nothing; values pointing at `localhost` must be
overridden in `env` with the service's host name (`postgres`, `redis`). Check which file
wins in the app's loader (Outline's `.env.test` supplies `SECRET_KEY`; the
`DATABASE_URL` in `env` wins over it).

## Setup commands that write outside the run's directories

**Symptom:** `npm install -g`, `pip install --user` into an image path, `apt-get`, `sudo`,
`corepack enable` or `chown` fail with "read-only file system" or "permission denied".

**Fix:** move them into the Dockerfile. Setup commands may write only under `/work`, the
home, `/tmp` and the `workstation.cache` folders (the root filesystem is read-only; see
the limits in run-conditions.md).

## Tests that start containers themselves

**Symptom:** a test suite fails with "Cannot connect to the Docker daemon", "Could not
find a valid Docker environment" (Testcontainers), or a script runs `docker compose up`.

**Cause:** there is no Docker daemon inside a run, and none can be added.

**Fix:** declare what those tests start as `services` (same images and env, the host names
the tests can be pointed at), and set the variables that make the suite use them instead
of starting their own (many suites have a profile or an env var for an existing
database). Tests that only work by starting containers stay
for CI; say so in `notes`, with the command that runs the rest.

## Architecture-specific downloads

**Symptom:** passes locally, fails in the cluster's build (or the reverse) on a binary
downloaded in the Dockerfile (`curl .../tool-linux-arm64`), or a service image "could not
be pulled" because it has no build for the cluster's architecture.

**Cause:** the cluster is `amd64` or `arm64`, and the developer's machine may be the
other (an Apple Silicon Mac is `arm64`; many clusters are `amd64`, cloud installs often
`arm64`).

**Fix:** never hard-code an architecture: select the binary by `$(uname -m)` or
`dpkg --print-architecture`, prefer `COPY --from=<multi-arch image>`. Ask which
architecture the cluster has; when it differs from the machine's, run
`check.sh --platform linux/<arch>` once before handing over.

## The agent cannot download what setup could

**Symptom:** Verify passes; a task then reports that it could not add a dependency, or a
test fails with `403` and "is not on this task's network allowlist (network.allow)", or
Maven or Gradle fails resolving a plugin on first use. The task page's Network tab lists
the refused hosts (Bank of Anthos: 215 refusals of Google's Maven Central mirror, which
its build tries first).

**Cause:** setup reaches any public host, the agent only the hosts in `network.allow`
(besides its model, the repositories and its connections). Whatever was not downloaded
by a setup command is out of reach afterwards unless its host is listed.

**Fix:** list the package registries of every package manager in setup, and the hosts the
tests and builds reach at run time (network.md has the defaults per tool). Take them from
the check, which prints the hosts setup really used, and prove the commands the agent
will run with `check.sh --run '<command>'`: they run with the agent's network. Maven and
Gradle resolve some plugins only when a goal first runs (`dependency:go-offline` does not
fetch everything a test run needs), so list their registry even when setup pre-fetches:
the `--run` of the test command shows whether anything is still missing.

## A tool that ignores the proxy

**Symptom:** a setup command hangs and ends with "connection timed out", "Network is
unreachable", `ETIMEDOUT`, or a host name that does not resolve, while the same command
works on a laptop.

**Cause:** nothing in a run has a direct route out; requests go to the proxy named by
`HTTPS_PROXY`, `HTTP_PROXY` and `JAVA_TOOL_OPTIONS`. Most tools read them. Exradius adds
`YARN_HTTPS_PROXY` for Yarn 2+ and a resolver flag for Maven, the two it met so far. A
tool that reads none of them connects directly and waits until it gives up.

**Fix:** give the tool its own proxy setting where it looks for one, pointing at the
variable rather than a fixed address: in a setup command
(`sometool config set proxy "$HTTPS_PROXY"`), or an `env` entry the tool reads. Never set
`HTTPS_PROXY` itself in `env`. The local check reproduces this (its containers have no
direct route either) and adds a hint when a command fails this way.

## An internal registry or mirror on a private address

**Symptom:** a build step or a setup command fails with "resolves to a private or
cluster-internal address", although the host is in `network.allow`.

**Cause:** private addresses (10.x, 172.16 to 172.31.x, 192.168.x and the like) are
refused in every phase, whatever the config says. Only the install can allow them.

**Fix:** nothing in the config. Name the host in the admin README's "Ask the admin"
section: it goes into the install's `egress.allowPrivate`. Check locally with
`--allow-private <host>`.

## SSH repository URLs, submodules, LFS

**Symptom:** "Clone" hangs or fails on `git@github.com:owner/repo.git`; or the clone
works and the build fails on an empty submodule folder or on LFS pointer files.

**Cause:** git reaches repositories through the proxy over HTTPS, so an SSH remote has no
way out. The clone fetches neither submodules nor LFS objects.

**Fix:** write every repository `url` as `https://host/owner/repo.git`, whatever the
developer's own remote says. For submodules add a setup command
(`cd <repo> && git submodule update --init --recursive`); their URLs in `.gitmodules`
must be HTTPS too (or rewrite them in the same command with
`git -c url.https://github.com/.insteadOf=git@github.com: submodule update --init`). For
LFS, install `git-lfs` in the Dockerfile and run `cd <repo> && git lfs pull` in setup.
Both work for public content during setup; a private submodule is better declared as a
repository of its own, with its credential.

## Notes that name commands nobody ran

**Symptom:** the agent reports "tests could not be run", runs the whole suite for ten
minutes on a one-line change, or ships untested because the documented command needs a
tool the image lacks.

**Cause:** `notes` were copied from the README. The README describes a developer's
machine; a run is a fresh clone with a read-only root, services on `localhost` and an
allowlisted network.

**Fix:** every command `notes` and a `role` name is run once by the check, as the agent
would: `check.sh --run 'cd api && uv run pytest tests/unit -q -x' --run 'cd web && pnpm
lint'`. Give the fast path (one test file, one package) as well as the full one, and the
flag that keeps memory down when a suite starts parallel workers. A command that cannot
work in a run is not in the notes; what replaces it is.

## Checks that need generated code

**Symptom:** type checks or tests fail in a fresh clone on missing generated files (a
Prisma client, GraphQL or protobuf types, compiled workspace packages), though they pass
on the developer's machine.

**Cause:** the developer generated them long ago; a run starts from a clean clone.

**Fix:** find the generating step in CI (it runs from a clean clone too) and add it to
setup after the install, or say in `notes` which command must run first when it is slow
and only some checks need it (Plane: type checks need `pnpm turbo run build
--filter=<package>...` first).

## Browsers for end-to-end tests

**Symptom:** Playwright or Cypress fails with missing shared libraries
(`libnss3.so: cannot open shared object file`), or tries to download a browser in the
agent phase and gets a `403`.

**Cause:** the browsers' system libraries need root to install, and the browser itself is
a download.

**Fix:** system libraries in the Dockerfile (`npx -y playwright@<version> install-deps
chromium`, with the version from the lockfile). The browser by a setup command
(`cd web && pnpm exec playwright install chromium`): it lands in the home, always matches
the lockfile, and setup's network is open. When the team does not expect agents to run
the end-to-end suite, leave it out and say so in `notes`.

## The Playwright connection has no browser

**Symptom:** Verify passes, but in a task every `browser_*` tool answers that the browser
is not installed, or the browser exits on a missing shared library, and the agent
delivers a user interface change it never saw.

**Cause:** the catalog's `playwright` connection starts the server only. Chromium's system
libraries need root, the browser is a download, and the agent phase is not allowed that
download. Verify's "List tools" step starts no browser, so nothing fails before a task.

**Fix:** both from the server's own package at the version the catalog pins (0.0.83), so
they match what the connection starts: another version looks for another build of the
browser. The libraries in the workstation Dockerfile:

```dockerfile
RUN npx -y -p @playwright/mcp@0.0.83 playwright install-deps chromium \
 && rm -rf /root/.npm /var/lib/apt/lists/*
```

The browser as a setup command (it lands in the home, about half a minute):

```yaml
workstation:
  setup:
    - npx -y @playwright/mcp@0.0.83 install-browser chromium
connections:
  playwright:
    catalog: playwright
```

Give the connection to the agents that change what users see, and say in `notes` how to
start the app and on which port, so the agent can open it on `localhost`. Prove it with
the check's `--run`: start the app and take a screenshot with the same Playwright
(`npx -y -p @playwright/mcp@0.0.83 playwright screenshot http://localhost:<port> /tmp/shot.png`).
