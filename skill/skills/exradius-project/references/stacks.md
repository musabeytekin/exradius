# Stacks: what worked, one row each

For each stack, the image and tool choices that passed the local check on a real project.
Read the rows of the project's languages, then pitfalls.md for the cache paths and
network.md for the hosts. Versions are examples: take the project's own from CI and its
lockfiles. A row that says "one project" has been proven once; the check is the judge.

## Languages

| Stack | Workstation image | What the image and `env` need |
|---|---|---|
| Node with npm | `node:<version>-bookworm` (or `-trixie`) | nothing more; `npm ci` in setup. `NODE_USE_ENV_PROXY: "1"` in `env` on Node 24 or later when a tool downloads with Node's own `fetch` (it ignores `HTTPS_PROXY` otherwise) |
| Yarn 2 or later | the same | corepack with the exact Yarn into `COREPACK_HOME=/opt/corepack` (pitfalls.md) |
| pnpm | the same | pnpm 12 or later: `RUN npm install -g pnpm@<exact version from packageManager> && pnpm --version` (the first call unpacks its native binary, which a read-only root cannot do later). pnpm 11 or older: corepack, as Yarn |
| Bun | the image of the other main language | `COPY --from=oven/bun:<version>-debian /usr/local/bin/bun /usr/local/bin/bun`; `bun ci` is the frozen install |
| Python with uv | `python:<version>-bookworm`, or any image plus uv | uv copied from `ghcr.io/astral-sh/uv:<version>`; a uv-managed Python installed into `/opt/uv/python` (pitfalls.md). With an old pinned uv, install a minor (`uv python install 3.12`), not a patch release it may not know. `UV_PYTHON` and `UV_PYTHON_DOWNLOADS: never` in `env` |
| Python with pip | `python:<version>-bookworm` | no virtualenv exists at run time: `python -m venv ~/.venv` as the first setup command and `ENV PATH=/home/runner/.venv/bin:$PATH` in the Dockerfile, or a `.venv` in the repository called as `.venv/bin/python` |
| Java with Maven | `maven:<version>-eclipse-temurin-<java>`, or `eclipse-temurin:<java>-jdk` when the project has the wrapper | `./mvnw` downloads Maven itself: cache `~/.m2/wrapper` next to `~/.m2/repository` |
| Java with Gradle | `eclipse-temurin:<java>-jdk` | `./gradlew --version` as a setup command fetches the wrapper's distribution while the network is open (its host redirects to GitHub's release storage, which does not belong in `network.allow`) |
| Go | `golang:<version>-bookworm` | golangci-lint by `COPY --from=golangci/golangci-lint:<version> /usr/bin/golangci-lint /usr/local/bin/`; gcc for `-race` is in the image |
| Rust | `rust:<version>-bookworm` | `RUN rustup component add rustfmt clippy` (the image has neither); `CARGO_HOME: /home/runner/.cargo` in `env`; `CARGO_BUILD_JOBS` to bound memory (ten parallel rustc and one linker went over 4 GB) |
| Ruby | `ruby:<version>` | `BUNDLE_PATH: /home/runner/.local/share/bundle` (the image's `GEM_HOME` is read-only at run time) and `BUNDLE_APP_CONFIG: .bundle` in `env` (the image's value hides the repository's `.bundle/config`, which may carry build flags); the Bundler of `BUNDLED WITH` installed in the image; `BUNDLE_FROZEN=true bundle install` is the frozen install |
| PHP | `php:<version>-cli` | the extensions CI installs (`docker-php-ext-install`); Composer by `COPY --from=composer:2 /usr/bin/composer /usr/local/bin/`; `COMPOSER_CACHE_DIR: /home/runner/.cache/composer` in `env` |
| .NET | `mcr.microsoft.com/dotnet/sdk:<version>` | `DOTNET_CLI_TELEMETRY_OPTOUT: "1"`, `MSBUILDDISABLENODEREUSE: "1"` in `env`; `dotnet restore` is the install (there is usually no lockfile), then `dotnet tool restore` when the repository has a tool manifest and `--no-restore` on later commands; `dotnet test --filter` is the narrow form; native libraries its packages load (`libfontconfig1` for SkiaSharp) in the image |
| Elixir | `hexpm/elixir:<elixir>-erlang-<otp>-debian-<suite>-<date>` | Hex and rebar3 installed in the image under `ENV MIX_HOME=/opt/mix` (`mix local.hex` cannot write at run time), `HEX_HOME` in the home; `build-essential` for dependencies that compile C |

Each of these was one project unless pitfalls.md has a section for it.

## More than one runtime

- **A second runtime** (Node next to PHP, Python or Elixir; an end-of-life Node for an old
  frontend): copy it from its official image at the exact version, which is multi-arch:

  ```dockerfile
  COPY --from=node:24.11.1-bookworm-slim /usr/local/bin/node /usr/local/bin/node
  COPY --from=node:24.11.1-bookworm-slim /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
  RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
   && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx && node --version && npm --version
  ```

  Add `/usr/local/include/node` when a dependency compiles a native add-on.
- **Two build systems for one code base** (Maven and Gradle): setup uses the one CI and
  the README lead with; the other still needs its registry in `network.allow` and a line
  in `notes` that its first build downloads.
- **A task runner the guide makes mandatory** (mise, just, task): it usually wraps the
  commands and, like mise, downloads tools on first use, which a run cannot. Either
  install it in the image with its tools and auto-install off, or leave it out, give
  the wrapped commands in `notes` and say the runner is not there.

## Things that need a place in the image

| Need | What worked |
|---|---|
| native npm add-ons with no prebuilt binary for the architecture (`hasInstallScript` in `package-lock.json` finds them) | `build-essential` and `python3` in the image; when the compile is slow, build once in the image and point the package at it (node-sass: `SASS_BINARY_PATH`) |
| a browser for unit tests (karma, vitest browser mode) | Debian's `chromium` with `CHROME_BIN=/usr/bin/chromium` and the runner's no-sandbox flag: there is no Google Chrome for `linux/arm64`. Playwright's browser: a setup command, with `~/.cache/ms-playwright` as a cache entry |
| lockfile dependencies written as `git+ssh://` | `RUN git config --system url."https://github.com/".insteadOf ssh://git@github.com/` |
| a program the application validates at start (ffmpeg, ImageMagick) | the package the project's own development image installs, by `dpkg --print-architecture` when it comes from another apt repository |
| data a development image carries | `COPY --from=<image>@sha256:<digest> /path /path` |

## Services

| Service | What worked |
|---|---|
| Postgres | `ready: {command: [pg_isready, -h, 127.0.0.1, -U, <user>, -d, <db>]}` |
| MySQL | `ready: {command: [mysqladmin, ping, -h, 127.0.0.1, --silent]}` |
| Redis 8 (the Debian image) | exits at start: its entrypoint drops privileges with a capability services do not have. `env: {SKIP_DROP_PRIVS: "1"}`, or `redis:<version>-alpine`, or Valkey |
| Valkey | `docker.io/valkey/valkey:<version>-alpine`, `ready: {command: [valkey-cli, ping]}` |
| ClickHouse | `ready: {command: [clickhouse-client, --host, 127.0.0.1, --query, "SELECT 1"]}`: not `wget`, which goes to the proxy (pitfalls.md) |
| MinIO and its forks | `command: [server, /data]` for the stock image; a fork whose entrypoint prefixes its own binary to every argument cannot run a bootstrap script as `command`: a service Dockerfile with its own `ENTRYPOINT` |
