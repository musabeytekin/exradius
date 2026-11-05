# The Claude Code runtime bundle (docs/runtime-bundles.md §2.1).
#
# The image holds only /opt/exradius/runtime:
#   bin/exradius-runner   launcher: runs exradius.runner on the bundle's own Python
#   bin/claude            native Claude Code binary, pinned below (no Node)
#   python/               relocatable CPython (python-build-standalone, glibc >= 2.17)
#                         with exradius and the Claude Agent SDK installed
#   VERSION               exradius version, runtime name, Claude Code version
#
# The worker copies it into a Docker volume and mounts that read-only into run containers,
# so any glibc-based project image can run the agent. Compiled wheels are limited to
# manylinux_2_28 (glibc 2.28), which sets the oldest supported project image glibc.
#
# Build the bundle directory alone with `make -C deploy bundle-claude-code`.

ARG EXRADIUS_VERSION=0.1.0

FROM debian:bookworm-slim AS build
ARG TARGETARCH
ARG EXRADIUS_VERSION
ARG PYTHON_VERSION=3.14.8
ARG CLAUDE_CODE_VERSION=2.1.318
ARG CLAUDE_CODE_SHA256_AMD64=708c77b204db30086ff6f1ed34577bda03e1be74ced80557cbf7b54249ebf0bd
ARG CLAUDE_CODE_SHA256_ARM64=53a0d7e219d7b0804ecba5dbc002745e89f786cc946d2de85a6470d5b2d1fb06

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
ENV UV_PYTHON_INSTALL_DIR=/build/pythons UV_LINK_MODE=copy UV_NO_CACHE=1

# Native Claude Code, from Anthropic's release bucket (the installer's download base),
# checked against the release manifest's checksum.
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) platform=linux-x64; sum=$CLAUDE_CODE_SHA256_AMD64 ;; \
      arm64) platform=linux-arm64; sum=$CLAUDE_CODE_SHA256_ARM64 ;; \
      *) echo "unsupported architecture $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    mkdir -p /bundle/bin; \
    curl -fsSL -o /bundle/bin/claude \
      "https://downloads.claude.ai/claude-code-releases/$CLAUDE_CODE_VERSION/$platform/claude"; \
    echo "$sum  /bundle/bin/claude" | sha256sum -c -; \
    chmod 755 /bundle/bin/claude; \
    /bundle/bin/claude --version

# The bundle's own interpreter.
RUN set -eu; \
    uv python install "$PYTHON_VERSION"; \
    python=$(uv python find "$PYTHON_VERSION"); \
    # uv links the minor version to the patch release: move the real directory.
    mv "$(realpath "$(dirname "$(dirname "$python")")")" /bundle/python; \
    rm -rf /build/pythons; \
    /bundle/python/bin/python3 --version

# exradius and the runner dependency group (the Agent SDK, which brings mcp, httpx and
# pydantic) into that interpreter; the server-side dependencies stay out (exradius is
# installed without its own dependencies). The SDK's own copy of the CLI is deleted: the
# runtime passes bin/claude as cli_path.
WORKDIR /src
COPY backend/pyproject.toml backend/uv.lock ./
COPY backend/exradius ./exradius
RUN set -eu; \
    case "$TARGETARCH" in amd64) arch=x86_64 ;; arm64) arch=aarch64 ;; esac; \
    uv export --frozen --only-group runner --no-emit-project --no-hashes -o /tmp/requirements.txt; \
    uv pip install --python /bundle/python/bin/python3 --break-system-packages \
      --python-platform "$arch-manylinux_2_28" -r /tmp/requirements.txt; \
    uv pip install --python /bundle/python/bin/python3 --break-system-packages --no-deps .; \
    installed=$(/bundle/python/bin/python3 -c 'from importlib.metadata import version; print(version("exradius"))'); \
    [ "$installed" = "$EXRADIUS_VERSION" ] || { echo "EXRADIUS_VERSION=$EXRADIUS_VERSION but the package is $installed" >&2; exit 1; }; \
    site=$(/bundle/python/bin/python3 -c 'import sysconfig; print(sysconfig.get_path("purelib"))'); \
    rm -rf "$site/claude_agent_sdk/_bundled/claude"; \
    /bundle/python/bin/python3 -m compileall -q "$site" /bundle/python/lib >/dev/null || true; \
    /bundle/python/bin/python3 -I -c 'import exradius.runner.main, exradius.runtimes.claude_code, claude_agent_sdk'

COPY deploy/runtime-claude-code/exradius-runner /bundle/bin/exradius-runner
RUN set -eu; \
    printf 'exradius %s\nruntime claude_code\nclaude-code %s\n' "$EXRADIUS_VERSION" "$CLAUDE_CODE_VERSION" > /bundle/VERSION; \
    chmod -R a+rX,go-w /bundle; chmod 755 /bundle/bin/exradius-runner

# `docker build --target bundle --output ...` exports just the directory.
FROM scratch AS bundle
COPY --from=build /bundle /

FROM scratch
ARG EXRADIUS_VERSION
LABEL exradius.role="runtime-bundle" \
      exradius.runtime="claude_code" \
      exradius.version="${EXRADIUS_VERSION}"
COPY --from=build /bundle /opt/exradius/runtime
