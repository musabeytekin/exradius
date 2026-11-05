# exradius-runner: the base image of every run container (Python 3.14, Node, git and the
# Claude Agent SDK, which bundles the Claude Code CLI). The api and worker services run
# from the same image with a different command, so the API can also start stdio MCP
# servers for "test connection".
#
# Project images must be built FROM this image so that `exradius-runner` stays available.

FROM node:22-bookworm-slim AS node

FROM python:3.14-slim-bookworm

# Marks runner images for the admin panel's image pick list (GET /api/system/images).
# Labels are inherited, so project images built FROM this one carry it too.
LABEL exradius.runner="true"

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates curl openssh-client \
 && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
 && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
 && node --version && npm --version

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/exradius/.venv \
    PATH=/opt/exradius/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /opt/exradius
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY backend/ ./
RUN uv sync --frozen --no-dev

# Run containers execute as this unprivileged user (EXRADIUS_RUN_USER, default 1000:1000).
RUN useradd --create-home --uid 1000 --shell /bin/bash runner \
 && mkdir -p /work /exradius/state && chown runner:runner /work /exradius/state \
 && git config --system --add safe.directory '*'

USER runner
WORKDIR /work
ENTRYPOINT ["exradius-runner"]
