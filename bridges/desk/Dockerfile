# docker build --build-context kit=../kit -t exradius-desk .   (from bridges/desk)
# The kit (bridges/kit) comes in as a named build context (docs/design.md §13); it lands at
# /kit, where pyproject.toml's path source (../kit from /app) finds it. The frontend is built
# in its own stage and served by the service from its package.
FROM node:24-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.1 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never

COPY --from=kit pyproject.toml uv.lock README.md /kit/
COPY --from=kit exradius_bridge_kit /kit/exradius_bridge_kit

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --no-editable
COPY exradius_desk ./exradius_desk
COPY --from=web /web/dist ./exradius_desk/static
RUN uv sync --frozen --no-dev --no-editable

RUN useradd --system --uid 10001 desk
USER 10001
ENV PATH="/app/.venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
CMD ["exradius-desk"]
