# Worked examples

Three real projects, as their config repositories hold them (the agents' identity email is
replaced by `dev-bot@example.com`). Each one failed Verify once before it passed; the
choices below are why the files look the way they do. Each config passes
`scripts/check.sh` with its code checked out.

## Bank of Anthos

Config repository: https://github.com/musabeytekin/exradius-bank (private). Project: one repository, Java (Spring Boot, Maven) and Python (Flask, uv) services, Postgres.

`exradius.yaml`:

```yaml
version: 1

project:
  slug: bank
  name: Bank of Anthos
  description: Sample retail bank. Java (Spring Boot) ledger services, Python (Flask) account services, two Postgres databases.

secret_store: bank-platform

repositories:
  - name: bank-of-anthos
    url: https://github.com/musabeytekin/bank-of-anthos.git
    branch: main
    credential: "${secret:GITHUB_TOKEN}"
    push: true

workstation:
  dockerfile: docker/workstation.Dockerfile
  setup:
    - cp -r /opt/m2 ~/.m2

services:
  postgres:
    image: postgres:16
    env:
      POSTGRES_USER: app
      POSTGRES_DB: app
      POSTGRES_PASSWORD: "${secret:DB_PASSWORD}"
    ports: [5432]
    ready:
      command: [pg_isready, -h, 127.0.0.1, -U, app, -d, app]

env:
  DATABASE_URL: "postgres://app:${secret:DB_PASSWORD}@postgres:5432/app"

notes: |
  Java services live under src/ledger and src/ledgermonolith; run their tests with
  `mvn test` inside the service directory (dependencies are pre-fetched in ~/.m2).
  Python services live under src/accounts and src/frontend; run their tests with
  `uv run pytest tests` inside the service directory.
  The postgres service is empty; the schemas are in src/accounts/accounts-db/initdb
  and src/ledger/ledger-db/initdb.

connections:
  github:
    catalog: github
    config:
      token: "${secret:GITHUB_TOKEN}"

agents:
  developer:
    name: Developer
    model: claude-sonnet-5-5
    identity:
      name: Developer
      email: dev-bot@example.com
    connections: [github]
    role: |
      You are a software developer on this team. You implement the changes a ticket asks
      for in the project's repositories, following the conventions you find there and
      keeping changes small and focused. Run the project's existing checks when they are
      available. Explain what you changed and what a reviewer should look at. After
      pushing, open a pull request against the repository's main branch with the GitHub
      tool and link it in your result. Do not leave this to the reviewer.
  scout:
    name: Scout
    role: |
      You investigate questions about this codebase and report findings with file paths
      and evidence. You never change code.
```

`docker/workstation.Dockerfile`:

```dockerfile
# Developer image for Bank of Anthos: what an engineer on the project has installed.
# JDK 21 + Maven 3.9 (base image), Python 3.14 + uv, Maven dependencies pre-fetched.
FROM maven:3.9-eclipse-temurin-21

RUN apt-get update \
 && apt-get install -y --no-install-recommends git bash curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# uv and a managed Python 3.14 (the version the services pin in .python-version)
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
RUN uv python install 3.14 && chmod -R a+rX /opt/uv

# Pre-fetch the Maven dependencies of every module so a run does not download them.
# Runs copy /opt/m2 into their own home (read-only root filesystem).
RUN git clone -q --depth 1 https://github.com/musabeytekin/bank-of-anthos.git /tmp/boa \
 && cd /tmp/boa && mvn -B -q -Dmaven.repo.local=/opt/m2 dependency:go-offline \
 && mvn -B -q -Dmaven.repo.local=/opt/m2 -DskipTests package -pl src/ledger/ledgerwriter >/dev/null 2>&1 || true \
 && chmod -R a+rX /opt/m2 && rm -rf /tmp/boa

# python3 on PATH, as on any engineer's machine (uv's managed interpreter).
RUN ln -s "$(ls -d /opt/uv/python/cpython-3.14*/bin)/python3.14" /usr/local/bin/python3 \
 && ln -s /usr/local/bin/python3 /usr/local/bin/python && python3 --version
```

Choices:

- `workstation.dockerfile`: the stock `maven` image has the JDK and Maven but no Python, no uv and no pre-fetched dependencies, so a Dockerfile is needed.
- `FROM maven:3.9-eclipse-temurin-21`: JDK 21 and Maven 3.9 are what the services' `pom.xml` files build with; the image is Ubuntu-based (glibc).
- `apt-get install git bash curl ca-certificates`: git and bash are run requirements; the maven image does not guarantee git.
- `COPY --from=ghcr.io/astral-sh/uv:...`: uv from its official image, no install script; the Python services are uv projects. (uv 0.9.7 is enough for these lockfiles; Saleor needed a newer one.)
- `UV_PYTHON_INSTALL_DIR=/opt/uv/python` + `uv python install 3.14` + `chmod -R a+rX`: the Python the services pin in `.python-version`, installed once in the image, readable by uid 1000, never downloaded by a run.
- Maven `dependency:go-offline` into `/opt/m2`: dependencies pre-fetched at build time so runs do not download them; `|| true` because the pre-fetch is a speed-up, not a requirement.
- `ln -s .../python3.14 /usr/local/bin/python3`: the first Verify failed with `python3: not found`; uv's interpreter is not on PATH by itself.
- `setup: cp -r /opt/m2 ~/.m2`: Maven writes into its local repository, `/opt` is read-only at run time, so the cache is copied into the run's home. This works because the run's passwd entry makes Java's `user.home` `/home/runner`; with the image's own `ubuntu` user it would look in `/home/ubuntu`.
- `services.postgres` with `pg_isready -h 127.0.0.1`: a TCP check, so readiness is not reported during the image's init-time socket-only server.
- `env.DATABASE_URL` uses the same `${secret:DB_PASSWORD}` as the service: one store key, consistent on both sides.
- `credential` + `push: true` on the repository and the `github` catalog connection share `GITHUB_TOKEN`: one token to clone, push and open pull requests.
- `notes`: where tests live and how to run them, which a new engineer would be told on day one; the schemas' location because the database starts empty.
- Two agents: `developer` (pushes, opens a pull request, `model` pinned) and `scout` (read-only by its role, no connections).

## Outline

Config repository: https://github.com/musabeytekin/exradius-outline. Project: one repository, TypeScript on Node with Yarn 4, Postgres and Redis.

`exradius.yaml`:

```yaml
version: 1

project:
  slug: outline
  name: Outline
  description: Team knowledge base. TypeScript on Node, Postgres and Redis.

secret_store: outline-platform

repositories:
  - name: outline
    url: https://github.com/musabeytekin/outline.git
    branch: main
    credential: "${secret:GITHUB_TOKEN}"
    push: true

workstation:
  dockerfile: docker/workstation.Dockerfile
  setup:
    - cd outline && yarn install --immutable
    - cd outline && NODE_ENV=test yarn sequelize db:create && NODE_ENV=test yarn sequelize db:migrate

services:
  postgres:
    image: postgres:16
    env:
      POSTGRES_USER: user
      POSTGRES_PASSWORD: "${secret:DB_PASSWORD}"
      POSTGRES_DB: outline
    ports: [5432]
    ready:
      command: [pg_isready, -h, 127.0.0.1, -U, user, -d, outline]
  redis:
    image: redis:7
    ports: [6379]
    ready: {tcp: 6379}

env:
  NODE_ENV: test
  DATABASE_URL: "postgres://user:${secret:DB_PASSWORD}@postgres:5432/outline-test"
  REDIS_URL: redis://redis:6379
  URL: http://localhost:3000
  SMTP_HOST: smtp.example.com
  SMTP_USERNAME: test
  SMTP_FROM_EMAIL: hello@example.com
  SMTP_REPLY_EMAIL: hello@example.com

notes: |
  Run the server tests with `yarn test:server` (needs the test database, created by the
  setup commands) and the app tests with `yarn test:app`. Lint with `yarn lint`.
  The repository's .env.test supplies SECRET_KEY and UTILS_SECRET and points DATABASE_URL
  at 127.0.0.1; the DATABASE_URL set here wins and points at the postgres service.

connections:
  github:
    catalog: github
    config:
      token: "${secret:GITHUB_TOKEN}"

agents:
  developer:
    name: Developer
    model: claude-sonnet-5-5
    identity:
      name: Developer
      email: dev-bot@example.com
    connections: [github]
    role: |
      You are a software developer on this team. You implement the changes a ticket asks
      for, following the conventions you find in the repository and keeping changes small
      and focused. Run the relevant tests and the linter before you finish. Explain what
      you changed and what a reviewer should look at. After pushing, open a pull request
      against the repository's main branch with the GitHub tool and link it in your
      result. Do not leave this to the reviewer.
  scout:
    name: Scout
    role: |
      You investigate questions about this codebase and report findings with file paths
      and evidence. You never change code.
```

`docker/workstation.Dockerfile`:

```dockerfile
# Developer image for Outline: Node 26 (per .nvmrc) with corepack and Yarn 4.18.0 ready.
# The node:26 image no longer ships corepack, so it is installed here, and the exact
# Yarn release is prepared into a shared, read-only COREPACK_HOME so runs never download it.
FROM node:26-bookworm

ENV COREPACK_HOME=/opt/corepack
RUN npm install -g corepack@latest \
 && corepack enable \
 && corepack install -g yarn@4.18.0 \
 && chmod -R a+rX /opt/corepack
```

Choices:

- `FROM node:26-bookworm`: the Node major from the repository's `.nvmrc`, Debian variant (glibc).
- corepack installed with npm, `corepack enable`, `corepack install -g yarn@4.18.0`: the first Verify failed because `node:26` ships no corepack, so the `packageManager: yarn@4.18.0` in `package.json` was never honoured.
- `COREPACK_HOME=/opt/corepack` + `chmod -R a+rX`: the exact Yarn release prepared read-only in the image, so runs never download it and nothing writes to `/opt` at run time.
- No pre-fetched `node_modules`: `yarn install --immutable` in setup takes about 50 s on the run's disk and always matches the checked-out lockfile.
- `setup` step 2 runs `sequelize db:create` and `db:migrate` with `NODE_ENV=test`: the server tests need a migrated test database on the postgres service.
- `DATABASE_URL` points at `outline-test` on host `postgres`: the repository's `.env.test` points at 127.0.0.1; the project `env` wins and points at the service.
- `SECRET_KEY` and `UTILS_SECRET` are not in `env` or the store: `.env.test` supplies them for test runs; only `DB_PASSWORD` and `GITHUB_TOKEN` are real store keys.
- SMTP and `URL` values are plain text: Outline's config validation requires them, their values do not matter for tests and are not secret.
- `redis` with `ready: {tcp: 6379}`: the image has no HEALTHCHECK; a listening port is the readiness signal.

## Saleor

Config repository: https://github.com/musabeytekin/exradius-saleor. Project: two repositories (Python API with uv, TypeScript dashboard with pnpm), Postgres and Valkey.

`exradius.yaml`:

```yaml
version: 1

project:
  slug: saleor
  name: Saleor
  description: Headless e-commerce platform. Python GraphQL API and a separate TypeScript dashboard.

secret_store: saleor-platform

repositories:
  - name: saleor
    url: https://github.com/musabeytekin/saleor.git
    branch: main
    credential: "${secret:GITHUB_TOKEN}"
    push: true
  - name: saleor-dashboard
    url: https://github.com/musabeytekin/saleor-dashboard.git
    branch: main
    credential: "${secret:GITHUB_TOKEN}"
    push: true

workstation:
  dockerfile: docker/workstation.Dockerfile
  setup:
    - cd saleor && uv sync --locked
    - cd saleor-dashboard && pnpm install --frozen-lockfile

services:
  postgres:
    image: postgres:15-alpine
    env:
      POSTGRES_USER: saleor
      POSTGRES_PASSWORD: "${secret:DB_PASSWORD}"
      POSTGRES_DB: saleor
    ports: [5432]
    ready:
      command: [pg_isready, -h, 127.0.0.1, -U, saleor, -d, saleor]
  cache:
    image: valkey/valkey:8.1-alpine
    ports: [6379]
    ready: {tcp: 6379}

env:
  DATABASE_URL: "postgres://saleor:${secret:DB_PASSWORD}@postgres:5432/saleor"
  CACHE_URL: redis://cache:6379/0
  CELERY_BROKER_URL: redis://cache:6379/1
  SECRET_KEY: local-test-key
  DEFAULT_FROM_EMAIL: noreply@example.com

notes: |
  API (saleor/): run tests with `uv run pytest saleor/<app> -n 0` inside the saleor folder
  (the suite defaults to pytest-xdist; -n 0 keeps a single worker). Lint with `uv run ruff check`.
  The test settings create their own test database on the postgres service.
  Dashboard (saleor-dashboard/): `pnpm run test`, `pnpm run check-types`, `pnpm run lint`.
  The two repositories are released separately; open one pull request per repository you change.

connections:
  github:
    catalog: github
    config:
      token: "${secret:GITHUB_TOKEN}"

agents:
  developer:
    name: Developer
    model: claude-sonnet-5-5
    identity:
      name: Developer
      email: dev-bot@example.com
    connections: [github]
    role: |
      You are a software developer on this team. You implement the changes a ticket asks
      for, following the conventions you find in each repository and keeping changes small
      and focused. Run the relevant tests and linters before you finish. Explain what you
      changed and what a reviewer should look at. After pushing, open a pull request
      against each changed repository's main branch with the GitHub tool and link them in
      your result. Do not leave this to the reviewer.
  scout:
    name: Scout
    role: |
      You investigate questions about this codebase and report findings with file paths
      and evidence. You never change code.
```

`docker/workstation.Dockerfile`:

```dockerfile
# Developer image for Saleor: Python 3.12 + uv for the API, Node 24 + pnpm 11 for the
# dashboard, and the Postgres client the test suite uses.
FROM python:3.12-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg git bash gettext libpq-dev postgresql-client \
 && curl -fsSL https://deb.nodesource.com/setup_24.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

# uv (API) and pnpm 11.13.1 via corepack (dashboard), prepared read-only so runs never download them.
COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /uvx /usr/local/bin/
ENV COREPACK_HOME=/opt/corepack
RUN npm install -g corepack@latest \
 && corepack enable \
 && corepack install -g pnpm@11.13.1 \
 && chmod -R a+rX /opt/corepack
```

Choices:

- Two `repositories`: the API and the dashboard release separately; both are cloned under `/work`, each with push allowed, and the role asks for one pull request per repository.
- `FROM python:3.12-bookworm`: the API's `requires-python`; Node 24 from NodeSource for the dashboard on top, because one workstation serves both repositories.
- `libpq-dev postgresql-client gettext`: what the API's test suite and its Django commands call (`psql`, `msgfmt`).
- `COPY --from=ghcr.io/astral-sh/uv:0.13.7`: the first Verify failed with uv 0.9.7, which cannot parse the `exclude-newer = "3 weeks"` in Saleor's `pyproject.toml`; 0.13.7 can.
- corepack + `pnpm@11.13.1` into `COREPACK_HOME=/opt/corepack`: the dashboard's `packageManager`, prepared read-only as for Outline.
- `setup`: `uv sync --locked` and `pnpm install --frozen-lockfile` fail instead of silently updating lockfiles, so a mismatch shows up in Verify.
- `postgres:15-alpine` and `valkey/valkey:8.1-alpine`: Alpine is fine for services (only the workstation must be glibc); the images and versions of the repository's `.devcontainer/docker-compose.yml`.
- Service named `cache`, not `redis`: the name is the host name in `CACHE_URL` and `CELERY_BROKER_URL`, as in that compose file.
- `SECRET_KEY: local-test-key` as plain text: a test-only value, not a secret, so it stays out of the store.
- `notes`: `-n 0` for pytest because the suite defaults to pytest-xdist workers, which multiply memory use in a run.

## Patterns the three share

- A Dockerfile per workstation, `FROM` the official image of the main language at the version the repository pins, Debian/Ubuntu variant.
- Tool versions taken from the repository (`.nvmrc`, `packageManager`, `.python-version`, `requires-python`, the developer's uv), prepared in the image under `/opt` with `chmod -R a+rX` and an `ENV` that points at them.
- Setup commands that install dependencies with the lockfile frozen (`--immutable`, `--locked`, `--frozen-lockfile`) and prepare databases; nothing that needs root.
- Services from stock images with a TCP-based `ready` check; secrets only for values that are really secret, referenced the same way in the service and in `env`.
- `developer` (push, pull request, `github` connection) and `scout` (read-only) agents.
