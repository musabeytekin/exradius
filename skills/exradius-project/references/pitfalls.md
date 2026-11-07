# Pitfalls, each with its fix

Each of these failed a real project's Verify at least once, or would have. They are what
`scripts/check.sh` exists to catch on the developer's machine. Read the lockfiles and
version files first; most failures are a tool in the image older or newer than what the
repository asks for.

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

## Caches the run must own: copy them into the home in `setup`

**Symptom:** a tool insists on writing into its cache (Maven's local repository, Gradle's
caches, Go's module cache) and fails on a read-only path under `/opt`, or every run
downloads all dependencies again.

**Fix:** pre-fetch into a world-readable directory in the Dockerfile, then copy it into
the home with a setup command:

```dockerfile
RUN git clone -q --depth 1 https://github.com/acme/app.git /tmp/app \
 && cd /tmp/app && mvn -B -q -Dmaven.repo.local=/opt/m2 dependency:go-offline \
 && chmod -R a+rX /opt/m2 && rm -rf /tmp/app
```

```yaml
workstation:
  setup:
    - cp -r /opt/m2 ~/.m2
```

The copy goes to the run's disk (seconds for a few hundred MB). Read-only caches that a
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

## Installs need disk, not RAM

**Symptom:** a setup command is killed with exit 137, or the server runs out of disk
during `yarn install`, `pnpm install`, `uv sync`, `mvn` or `cargo build`.

**Cause:** exit 137 is the kernel killing the process, usually for memory. The run's home
and `/tmp` are on disk, so caches and Yarn's staging no longer use RAM, but the package
manager's own process memory remains (Outline's `yarn install` peaks around 2.3 GB) and
parallel installs of several projects add up. On disk, a large JavaScript install takes
about 0.8 GB in the home and as much again in `/tmp`.

**Fix:** keep setup to what the agent needs; reduce parallelism where the tool allows it
(`yarn config set networkConcurrency`, `pnpm --network-concurrency`, `-n 0` for
pytest-xdist in tests, `MAVEN_OPTS=-Xmx...`); tell the admin how much memory and disk a
run of this project needs. Locally, Docker Desktop's memory limit applies; check.sh names
it when a command is killed.

## BuildKit-only Dockerfile syntax

**Symptom:** the local `docker build` works, Verify's "Build workstation" fails with
"the --mount option requires BuildKit".

**Cause:** Exradius builds through the Docker Engine API's classic builder.

**Fix:** no `RUN --mount=type=cache`, no `RUN <<EOF` heredocs, no `# syntax=` directive.
check.sh builds with the classic builder by default to catch this.

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
home and `/tmp`.

## Architecture-specific downloads

**Symptom:** passes locally on an Apple Silicon Mac, fails on the server (or the reverse)
on a binary downloaded in the Dockerfile (`curl .../tool-linux-arm64`).

**Fix:** select the binary by `$(uname -m)` or `dpkg --print-architecture`, prefer
`COPY --from=<multi-arch image>`, and run `check.sh --platform linux/amd64` once before
handing over.
