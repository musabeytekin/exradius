# exradius-backend: the api and worker services (docs/runtime-bundles.md §2.5).
#
# No Node and no Claude Agent SDK: agents run in run containers with a runtime bundle
# mounted (deploy/runtime-claude-code.Dockerfile). The worker needs git (mirrors) and the
# Docker SDK; the API needs git for repository checks.

FROM python:3.14-slim-bookworm

LABEL exradius.role="backend"

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates openssh-client \
 && rm -rf /var/lib/apt/lists/*

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
