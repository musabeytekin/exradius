# Worked examples

Four real projects that run on Exradius, with the choices that made their files look the
way they do. The first three failed Verify once before they passed; Plane, set up by this
skill, passed first time. The files are brought up to this skill release: each declares
`resources.memory`, a `workstation.cache` and a `network.allow`, the roles say which
tickets an agent takes, and `model` is left to the install's default. The agents'
identity email is replaced by `dev-bot@example.com`.

Read the one closest to the project at hand, not all four:

| Project | Read it for |
|---|---|
| Bank of Anthos | Java with Maven, Python with uv in one repository; a pre-fetched Maven repository; a mirror host in `network.allow` |
| Outline | one Node repository with Yarn 4; a test database created in setup; values the repository's own `.env.test` supplies |
| Saleor | two repositories in one project (uv API, pnpm dashboard); services named as the app expects |
| Plane | a pnpm + Turbo monorepo with a Django API; four services, one with a bootstrap `command`; long `notes` for a large codebase |

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

resources:
  memory: 4g

repositories:
  - name: bank-of-anthos
    url: https://github.com/musabeytekin/bank-of-anthos.git
    branch: main
    credential: "${secret:GITHUB_TOKEN}"
    push: true

workstation:
  dockerfile: docker/workstation.Dockerfile
  setup:
    - test -e ~/.m2/repository/.seeded || (cp -r /opt/m2/. ~/.m2/repository/ && touch ~/.m2/repository/.seeded)
  cache:
    - ~/.m2/repository
    - ~/.cache/uv

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
  UV_LINK_MODE: copy

network:
  allow:
    - repo.maven.apache.org
    - maven-central.storage-download.googleapis.com
    - pypi.org
    - files.pythonhosted.org

notes: |
  Java services live under src/ledger and src/ledgermonolith; run their tests with
  `mvn test` inside the service directory (dependencies are in ~/.m2/repository, kept
  between runs).
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
    identity:
      name: Developer
      email: dev-bot@example.com
    connections: [github]
    role: |
      You take tickets that ask for a change to the code: bug fixes, features,
      refactors, dependency updates. You implement the change following the conventions
      you find in the repository, and keep it small and focused.
      Before you finish, run the tests of every service you touched (the notes say how)
      and say in your result which you ran and what they showed; if one cannot run here,
      say that rather than skipping it.
      After pushing, open a pull request against the repository's main branch with the
      GitHub tool and link it in your result. Do not leave this to the reviewer.
  scout:
    name: Scout
    role: |
      You take tickets that ask a question about the code or its behaviour: where
      something is, why something happens, what a change would affect. You investigate and
      report findings with file paths and evidence. You never change code, push or open
      pull requests.
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
# The first run copies /opt/m2 into its ~/.m2/repository cache (read-only root filesystem).
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
- `cache: [~/.m2/repository, ~/.cache/uv]`: Maven's local repository and uv's cache are kept between runs, so only the first run downloads. Java finds `~/.m2` because the run's passwd entry makes its `user.home` `/home/runner`; with the image's own `ubuntu` user it would look in `/home/ubuntu`.
- The setup command seeds the Maven cache once from the image's pre-fetch: `/opt/m2` holds the artifacts at its top level (it was the `maven.repo.local`), so its contents go into `~/.m2/repository`; the marker file skips the copy in later runs. (The earlier `cp -r /opt/m2 ~/.m2` put them in `~/.m2` itself, where Maven does not look.)
- `resources.memory: 4g`: a Maven build of one service plus the Python tests stay well under it; Exradius places the run where 4 GiB are free and enforces it as the limit.
- `UV_LINK_MODE: copy`: uv cannot hard-link from the cache folder into a `.venv` in `/work` (separate mounts) and copies anyway; this only silences its warning.
- `services.postgres` with `pg_isready -h 127.0.0.1`: a TCP check, so readiness is not reported during the image's init-time socket-only server.
- `env.DATABASE_URL` uses the same `${secret:DB_PASSWORD}` as the service: one store key, consistent on both sides.
- `credential` + `push: true` on the repository and the `github` catalog connection share `GITHUB_TOKEN`: one token to clone, push and open pull requests.
- `network.allow`: Maven Central and PyPI, so an agent can add a dependency and Maven can resolve the plugins a test run fetches on first use. The Google mirror is there because this build tries it before Maven Central: the first tasks had 215 refused requests to it on the task page's Network tab, each a slow failure before the fallback.
- `notes`: where tests live and how to run them, which a new engineer would be told on day one; the schemas' location because the database starts empty.
- The developer's `role` opens with the tickets it takes, because Exradius reads roles to pick an agent when a task names none; then when the work is done (the tests ran, or the result says why not) and how it is delivered (a pull request, opened by the agent).
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

resources:
  memory: 6g

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
  cache:
    - ~/.yarn/berry/cache

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

network:
  allow:
    - registry.yarnpkg.com

notes: |
  Run the server tests with `yarn test:server` (needs the test database, created by the
  setup commands) and the app tests with `yarn test:app`; one file with
  `yarn test:server <path>`. Lint with `yarn lint`.
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
    identity:
      name: Developer
      email: dev-bot@example.com
    connections: [github]
    role: |
      You take tickets that ask for a change to the code: bug fixes, features,
      refactors, dependency updates. You implement the change following the conventions
      you find in the repository, and keep it small and focused.
      Before you finish, run the tests that cover what you touched and `yarn lint`, and
      say in your result which you ran and what they showed; if one cannot run here, say
      that rather than skipping it.
      After pushing, open a pull request against the repository's main branch with the
      GitHub tool and link it in your result. Do not leave this to the reviewer.
  scout:
    name: Scout
    role: |
      You take tickets that ask a question about the code or its behaviour: where
      something is, why something happens, what a change would affect. You investigate and
      report findings with file paths and evidence. You never change code, push or open
      pull requests.
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
- No pre-fetched `node_modules`: `yarn install --immutable` in setup always matches the checked-out lockfile.
- `cache: [~/.yarn/berry/cache]`: Yarn 4's global cache (`enableGlobalCache` is the default and the repository's `.yarnrc.yml` does not turn it off), kept between runs so only the first run downloads the archives.
- `resources.memory: 6g`: `yarn install` peaks around 2.3 GB (a run was killed with exit 137 when the home was still in RAM), and the server tests run Jest workers on top.
- `setup` step 2 runs `sequelize db:create` and `db:migrate` with `NODE_ENV=test`: the server tests need a migrated test database on the postgres service.
- `DATABASE_URL` points at `outline-test` on host `postgres`: the repository's `.env.test` points at 127.0.0.1; the project `env` wins and points at the service.
- `SECRET_KEY` and `UTILS_SECRET` are not in `env` or the store: `.env.test` supplies them for test runs; only `DB_PASSWORD` and `GITHUB_TOKEN` are real store keys.
- SMTP and `URL` values are plain text: Outline's config validation requires them, their values do not matter for tests and are not secret.
- `redis` with `ready: {tcp: 6379}`: the image has no HEALTHCHECK; a listening port is the readiness signal.
- `network.allow: [registry.yarnpkg.com]`: Yarn 4's default registry, the one host `yarn install` used; with it an agent can run `yarn add` or `yarn up`. Yarn 4 ignores `HTTPS_PROXY`; Exradius sets `YARN_HTTPS_PROXY` in every run, so nothing about the proxy is in this file.
- `notes` give the single-file form of the test command: the server suite takes minutes, and an agent that knows only the full command either waits for it or skips it.

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

resources:
  memory: 4g

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
  cache:
    - ~/.cache/uv
    - ~/.local/share/pnpm/store

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
    memory: 512m
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
  pnpm_config_store_dir: /home/runner/.local/share/pnpm/store
  UV_LINK_MODE: copy

network:
  allow:
    - pypi.org
    - files.pythonhosted.org
    - registry.npmjs.org

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
    identity:
      name: Developer
      email: dev-bot@example.com
    connections: [github]
    role: |
      You take tickets that ask for a change to the code of the API or the dashboard:
      bug fixes, features, refactors, dependency updates. You implement the change
      following the conventions you find in each repository, and keep it small and focused.
      Before you finish, run the tests and linters for what you touched (the notes say
      how, per repository) and say in your result which you ran and what they showed; if
      one cannot run here, say that rather than skipping it.
      After pushing, open a pull request against each changed repository's main branch
      with the GitHub tool and link them in your result. Do not leave this to the reviewer.
  scout:
    name: Scout
    role: |
      You take tickets that ask a question about the code or its behaviour: where
      something is, why something happens, what a change would affect. You investigate and
      report findings with file paths and evidence. You never change code, push or open
      pull requests.
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
- `cache` for both package managers, uv's and pnpm's stores; `pnpm_config_store_dir` pins the pnpm store to the cache folder (otherwise pnpm may put it in `/work/.pnpm-store`, deleted with the run).
- `resources.memory: 4g` and `memory: 512m` on postgres: one pytest worker and the dashboard's install fit with headroom; the check applies both as the containers' limits.
- `network.allow`: PyPI for the API and the npm registry for the dashboard, the registries of the two package managers in setup, so dependencies can be added in either repository.

## Plane

Config repository: https://github.com/musabeytekin/exradius-plane (private). Project: one repository, a pnpm + Turbo monorepo of TypeScript/React apps and a Django API with uv; Postgres, Valkey, RabbitMQ and S3-compatible storage.

`exradius.yaml`:

```yaml
version: 1

project:
  slug: plane
  name: Plane
  description: Open-source project management. TypeScript/React web apps (pnpm + Turbo monorepo) and a Python Django API; Postgres, Valkey, RabbitMQ and S3-compatible storage.

secret_store: plane

resources:
  memory: 6g

repositories:
  - name: plane
    url: https://github.com/musabeytekin/plane.git
    branch: preview
    credential: "${secret:GITHUB_TOKEN}"
    push: true

workstation:
  dockerfile: docker/workstation.Dockerfile
  setup:
    - cd plane && pnpm install --frozen-lockfile
    - cd plane/apps/api && uv sync --locked
    - mkdir -p plane/apps/api/plane/static-assets/collected-static
  cache:
    - ~/.local/share/pnpm/store
    - ~/.cache/uv

services:
  postgres:
    image: postgres:15.7-alpine
    env:
      POSTGRES_USER: plane
      POSTGRES_PASSWORD: plane
      POSTGRES_DB: plane
    ports: [5432]
    ready:
      command: [pg_isready, -h, 127.0.0.1, -U, plane, -d, plane]
  redis:
    image: valkey/valkey:7.2.11-alpine
    ports: [6379]
    ready: {tcp: 6379}
  rabbitmq:
    image: rabbitmq:3.13.6-management-alpine
    env:
      RABBITMQ_DEFAULT_USER: plane
      RABBITMQ_DEFAULT_PASS: plane
      RABBITMQ_DEFAULT_VHOST: plane
    ports: [5672]
    ready: {tcp: 5672}
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
    env:
      MINIO_ROOT_USER: access-key
      MINIO_ROOT_PASSWORD: secret-key
    ports: [9000]
    ready:
      command: [mc, ls, local/uploads]

env:
  DJANGO_SETTINGS_MODULE: plane.settings.test
  DEBUG: 0
  SECRET_KEY: exradius-local-test-key
  DATABASE_URL: postgresql://plane:plane@postgres:5432/plane
  POSTGRES_USER: plane
  POSTGRES_PASSWORD: plane
  POSTGRES_HOST: postgres
  POSTGRES_DB: plane
  POSTGRES_PORT: 5432
  REDIS_HOST: redis
  REDIS_PORT: 6379
  REDIS_URL: redis://redis:6379/
  RABBITMQ_HOST: rabbitmq
  RABBITMQ_PORT: 5672
  RABBITMQ_USER: plane
  RABBITMQ_PASSWORD: plane
  RABBITMQ_VHOST: plane
  AWS_REGION: ""
  AWS_ACCESS_KEY_ID: access-key
  AWS_SECRET_ACCESS_KEY: secret-key
  AWS_S3_ENDPOINT_URL: http://minio:9000
  AWS_S3_BUCKET_NAME: uploads
  USE_MINIO: 0
  EMAIL_HOST: test-smtp.invalid
  CORS_ALLOWED_ORIGINS: "http://localhost:3000,http://localhost:3001,http://localhost:3002,http://localhost:3100"
  WEB_URL: http://localhost:8000
  APP_BASE_URL: http://localhost:3000
  ADMIN_BASE_URL: http://localhost:3001
  ADMIN_BASE_PATH: /god-mode
  SPACE_BASE_URL: http://localhost:3002
  SPACE_BASE_PATH: /spaces
  LIVE_BASE_URL: http://localhost:3100
  LIVE_BASE_PATH: /live
  LIVE_SERVER_SECRET_KEY: secret-key
  NODE_OPTIONS: --max-old-space-size=4096
  pnpm_config_store_dir: /home/runner/.local/share/pnpm/store
  UV_LINK_MODE: copy

network:
  allow:
    - registry.npmjs.org
    - pypi.org
    - files.pythonhosted.org

notes: |
  One repository, /work/plane, default branch `preview` (pull requests target preview,
  not main). AGENTS.md at the root is the team's own guide; follow it.

  Web (pnpm + Turbo monorepo: apps/web, apps/admin, apps/space, apps/live, packages/*):
  dependencies are installed by setup. Checks, from /work/plane:
  `pnpm check:format`, `pnpm check:lint`, `pnpm check:types` (types need built packages:
  `pnpm turbo run build --filter=<package>...` first), or one package with
  `pnpm turbo run <task> --filter=<package>`. Unit tests are vitest: `pnpm turbo run test
  --filter=<package>`. Fix formatting with `pnpm fix:format`. Translations live in
  packages/i18n/src/locales.

  API (Django, apps/api, uv; the venv is apps/api/.venv, made by setup): run tests from
  apps/api with `uv run pytest plane/tests/unit` (or `-m unit`, `-m contract`, a path,
  `-k ...`); add `-n 0` if you pass -n. pytest uses plane.settings.test, --nomigrations and
  --reuse-db and creates test_plane on the postgres service itself. Lint with
  `uv run ruff check .`; check migrations with
  `uv run python manage.py makemigrations --check --dry-run`. Never edit a migration that
  is already on preview; add a new one. The repo's docker-compose-test.yml is not usable
  here (no Docker daemon); the services below replace it with the same images and values.

  Services: postgres (plane/plane), redis (Valkey), rabbitmq (vhost plane) for Celery,
  minio (S3 with bucket `uploads`). All credentials are throwaway test values.

connections:
  github:
    catalog: github
    config:
      token: "${secret:GITHUB_TOKEN}"

agents:
  developer:
    name: Developer
    connections: [github]
    role: |
      You take tickets that ask for a change to Plane's code, web apps or API: bug fixes,
      features, refactors, dependency updates. You implement the change following
      AGENTS.md and the conventions you find in the code, and keep it small and focused.
      Before you finish, run the checks for what you touched:
      for apps/api the relevant pytest tests and `uv run ruff check .` (plus
      `makemigrations --check` when models changed); for the web apps and packages
      `check:format`, `check:lint`, `check:types` and the vitest tests of the affected
      packages. Say in your result which you ran and what they showed; if one cannot run
      here, say that rather than skipping it. After pushing, open a pull request against
      `preview` with the GitHub tool and link it in your result. Do not leave this to the
      reviewer.
  scout:
    name: Scout
    role: |
      You take tickets that ask a question about the code or its behaviour: where
      something is, why something happens, what a change would affect. You investigate and
      report findings with file paths and evidence. You never change code, push or open
      pull requests.
```

`docker/workstation.Dockerfile`:

```dockerfile
# Developer image for Plane: what an engineer on the project has installed.
# Node 22.22.0 (.node-version / .mise.toml) with pnpm 11.10.0 (packageManager) for the web
# monorepo; uv 0.12.21 (apps/api required-version) and Python 3.12.12 (CI's patch release)
# for the Django API. Tools are prepared read-only under /opt so runs never download them.
FROM node:22.22.0-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends git bash ca-certificates curl postgresql-client \
 && rm -rf /var/lib/apt/lists/*

# pnpm, the exact release from package.json's packageManager, via corepack.
ENV COREPACK_HOME=/opt/corepack
RUN npm install -g corepack@latest \
 && corepack enable \
 && corepack install -g pnpm@11.10.0 \
 && chmod -R a+rX /opt/corepack

# uv pinned to [tool.uv] required-version, and the managed Python the API needs.
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /uvx /usr/local/bin/
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
RUN uv python install 3.12.12 && chmod -R a+rX /opt/uv

# python3 on PATH is the API's 3.12, not the image's Debian 3.11.
RUN ln -s "$(ls -d /opt/uv/python/cpython-3.12.12*/bin)/python3.12" /usr/local/bin/python3 \
 && ln -s /usr/local/bin/python3 /usr/local/bin/python && python3 --version
```

Choices:

- `FROM node:22.22.0-bookworm`: the exact Node of `.node-version` and `.mise.toml`; the web monorepo is the bigger half of the project, so Node is the base and Python is added.
- pnpm 11.10.0 via corepack into `COREPACK_HOME=/opt/corepack`: the root `packageManager`, prepared read-only as for Outline.
- uv 0.12.21 (the API's `[tool.uv] required-version`) and Python 3.12.12 (CI's patch release) installed by uv under `/opt/uv/python`, linked as `python3` so it wins over Debian's 3.11.
- `postgresql-client`: the API's test settings and management commands call `psql`.
- `branch: preview`: Plane's default branch; pull requests target it, and the role says so.
- `cache: [~/.local/share/pnpm/store, ~/.cache/uv]`, `pnpm_config_store_dir` and `UV_LINK_MODE: copy`: the two package managers' stores are kept between runs. Measured on Exradius's own Verify: `pnpm install` 88 s on the first run, 54 s on the second, `uv sync` 16 s to 9 s, 1.2 GB cached. With the local check on an Apple Silicon Mac: 72 to 79 s with 1175 packages downloaded, then 48 to 56 s with all 1175 reused and none downloaded; the rest is the copy into `/work` (no hard links across bind mounts) and pnpm's lockfile policy check. `pnpm_config_store_dir` pins the store, because pnpm otherwise may choose `/work/.pnpm-store`, which is deleted with the run.
- `resources.memory: 6g`: the pnpm install and the TypeScript type checks are the peak (`NODE_OPTIONS=--max-old-space-size=4096` caps Node's heap at 4 GB, plus the agent); before this key existed, Plane was run one task at a time by hand for this reason.
- `minio` from the stock `pgsty/silo` image with a `command`: the code repository's `docker-compose-test.yml` overrides the entrypoint with a shell script that starts the server and creates the `uploads` bucket. The image's entrypoint passes a first argument of `/bin/sh` through unchanged, so the same script works as the container command, and the image's entrypoint is kept (until skill 0.2.0 this needed a `docker/minio.Dockerfile`). `ready: mc ls local/uploads` waits for the bucket, not only the port.
- `postgres:15.7-alpine`, `valkey/valkey:7.2.11-alpine`, `rabbitmq:3.13.6-management-alpine`: the images and versions of `docker-compose-test.yml`, credentials as in that file; all are throwaway test values, so the only store key is `GITHUB_TOKEN`.
- The many `env` entries: the API's settings read each service's host, port and credentials separately, and the values in the repository's env examples point at `localhost`.
- `mkdir -p .../collected-static` in setup: the API's `STATIC_ROOT` (`settings/common.py`); the directory is not in the repository, so setup creates it for the tests.
- `network.allow`: the npm registry and PyPI, for the two package managers in setup. The local check confirmed them: its Network step showed `reached in setup: registry.npmjs.org, nodejs.org, files.pythonhosted.org, pypi.org`, and with the three listed, `uv run ruff check .`, the API's 566 unit tests and `pnpm check:format` all passed in the agent phase. Two hosts were left off on purpose: `nodejs.org` (`reached in setup only`: something `pnpm install` fetches once, which the agent does not need again) and `telemetry.vercel.com` (`refused in the agent phase`, 4 requests: Turbo's telemetry, whose refusal breaks nothing). Nothing for the services: they are on the run's `localhost`.
- The role names the checks per area instead of "run the tests": in a monorepo the agent otherwise runs everything or nothing.
- `notes`: AGENTS.md is the team's guide; which checks exist for the web packages and the API, that type checks need built packages first, and that `docker-compose-test.yml` cannot be used inside a run (no Docker daemon) while the services replace it.

## Patterns the four share

- A Dockerfile per workstation, `FROM` the official image of the main language at the version the repository pins, Debian/Ubuntu variant.
- Tool versions taken from the repository (`.nvmrc`, `packageManager`, `.python-version`, `requires-python`, the developer's uv), prepared in the image under `/opt` with `chmod -R a+rX` and an `ENV` that points at them.
- Setup commands that install dependencies with the lockfile frozen (`--immutable`, `--locked`, `--frozen-lockfile`) and prepare databases; nothing that needs root.
- A `workstation.cache` entry for every package manager in setup, and `resources.memory` from the heaviest step.
- Services from stock images (with a `command` where compose has one) and a TCP-based `ready` check; secrets only for values that are really secret, referenced the same way in the service and in `env`.
- `network.allow` with the registries of the package managers in setup, plus whatever the check showed the build reaching first (a mirror); nothing listed "just in case".
- `notes` that give each area's check commands as they work in a run, with the fast form for one file or package, and say what cannot run there.
- `developer` (push, pull request, `github` connection) and `scout` (read-only) agents, each role opening with the tickets the agent takes and, for the developer, ending with when the work is done and how it is delivered.
