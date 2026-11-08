# exradius-backend: the api and worker Deployments (docs/runtime-bundles.md §2.5).
#
# No Node and no Claude Agent SDK: agents run in run pods with a runtime bundle mounted
# (deploy/runtime-claude-code.Dockerfile). The api and the worker need git (config
# repository mirrors). The processes run as uid 1000 (user exradius), so the pods meet Pod
# Security "restricted"; /opt/exradius is that user's so that Tilt can sync source files
# into a running container (the chart runs it with a read-only root otherwise).

FROM python:3.14-slim-bookworm

LABEL exradius.role="backend"

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates openssh-client \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 1000 exradius \
 && useradd --uid 1000 --gid 1000 --home-dir /opt/exradius --no-create-home --shell /usr/sbin/nologin exradius

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
RUN uv sync --frozen --no-dev \
 && chown -R 1000:1000 /opt/exradius

USER 1000:1000
