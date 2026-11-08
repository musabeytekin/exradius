# docker build --build-context kit=../exradius-bridge-kit -t exradius-jira .
# The kit comes in as a named build context until it has a remote (docs/design.md §8); it
# lands at /exradius-bridge-kit, where pyproject.toml's path source (../exradius-bridge-kit
# from /app) finds it.
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

COPY --from=kit pyproject.toml uv.lock README.md /exradius-bridge-kit/
COPY --from=kit exradius_bridge_kit /exradius-bridge-kit/exradius_bridge_kit

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable
COPY exradius_jira ./exradius_jira
RUN uv sync --frozen --no-dev --no-editable

RUN useradd --system --uid 10001 bridge
USER bridge
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
CMD ["exradius-jira"]
