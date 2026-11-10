# The Claude Code runtime bundle (docs/runtime-bundles.md §2.1).
#
# The image's root is the bundle, which run pods see at /opt/exradius/runtime:
#   bin/exradius-runner   launcher: runs exradius.runner on the bundle's own Python
#   bin/claude            native Claude Code binary, pinned below (no Node)
#   python/               relocatable CPython (python-build-standalone, glibc >= 2.17)
#                         with exradius and the Claude Agent SDK installed
#   git-hooks/            the hooks dispatcher under every hook name (core.hooksPath)
#   VERSION               exradius version, runtime name, Claude Code version
#   .exradius/copy        copies the entries above into a directory, with the static busybox
#                         beside it: runs.bundleMount: copy, for nodes without image volumes
#
# Run pods mount it read-only at /opt/exradius/runtime as an image volume (docs/kubernetes.md
# §7.2), so any glibc-based project image can run the agent. Compiled wheels are limited to
# manylinux_2_28 (glibc 2.28), which sets the oldest supported project image glibc.
#
# Build the bundle directory alone with `make -C deploy bundle-claude-code`.

ARG EXRADIUS_VERSION=0.2.0

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
    /bundle/python/bin/python3 -I -m exradius.runner.gitconfig /bundle/git-hooks; \
    printf 'exradius %s\nruntime claude_code\nclaude-code %s\n' "$EXRADIUS_VERSION" "$CLAUDE_CODE_VERSION" > /bundle/VERSION; \
    chmod -R a+rX,go-w /bundle; chmod 755 /bundle/bin/exradius-runner

# The bundle image has no libc, so its copy command is a script for a static busybox. tar
# keeps modes and times (the .pyc files check their sources' times) and, as the run user,
# no owners. The entries are the layout above.
FROM busybox:1.37.0-musl@sha256:f6a95d611e0a8780fabdb2aec43e31275b2720dfefa3e41b16835bfc42454478 AS copy
RUN set -eu; mkdir -p /out/.exradius; cp /bin/busybox /out/.exradius/busybox; \
    printf '%s\n' \
      '#!/.exradius/busybox sh' \
      '# Copies the bundle into $1 (runs.bundleMount: copy, docs/kubernetes.md §7.2).' \
      'set -eu -o pipefail' \
      'b=/.exradius/busybox' \
      'cd /' \
      '$b tar -cf - bin python git-hooks VERSION | $b tar -C "$1" -xof -' \
      'echo "copied the runtime bundle: $($b head -1 VERSION), $($b du -sm "$1" | $b cut -f1) MB"' \
      > /out/.exradius/copy; \
    chmod 755 /out/.exradius/copy /out/.exradius/busybox

# `docker build --target bundle --output ...` exports just the directory.
FROM scratch AS bundle
COPY --from=build /bundle /

# The image a run pod mounts as an image volume at /opt/exradius/runtime: its root is the
# bundle (docs/kubernetes.md §7.2).
FROM scratch
ARG EXRADIUS_VERSION
LABEL exradius.role="runtime-bundle" \
      exradius.runtime="claude_code" \
      exradius.version="${EXRADIUS_VERSION}"
COPY --from=build /bundle /
COPY --from=copy /out/ /
