# ---- Stage 1: Builder ----
FROM python:3.13-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

WORKDIR /app

# Install dependencies first (layer caching -- only rebuilds when deps change)
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

# Copy full project and install
COPY . /app
# Remove data that will be fetched live from Gitea (D-12, D-14)
RUN rm -rf /app/data/stacks /app/data/docs /app/data/vault
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# ---- Stage 2: Runtime ----
FROM python:3.13-slim

WORKDIR /app
COPY --from=builder /app /app

# Run as an unprivileged user: the app binds only 5774 (>1024) and needs no
# root-owned paths, but holds API keys to ~17 services, so root is pure
# unnecessary attack surface. chown so the live docs/stacks/vault refresh can
# still write under /app/data.
RUN useradd --system --no-create-home app && chown -R app:app /app
USER app

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 5774

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:5774/health')" || exit 1

ENV MCP_TRANSPORT=streamable-http
ENV MCP_PORT=5774

# Injected by CI (CalVer+SHA); surfaces in /health for deploy verification
ARG APP_VERSION=dev
ENV APP_VERSION=$APP_VERSION

CMD ["python", "server.py"]
