# syntax=docker/dockerfile:1
# Production image for the API and the worker.
#
# Multi-stage: the build stage has uv and compiles the dependency tree; the runtime stage ships only the
# venv and the source on a slim base — no build tools, no cache, a smaller attack surface. And NON-ROOT,
# because an application process running as root is one container-escape bug away from root on the host,
# and nothing this app does needs it. The same image runs both roles — `run api` or `run worker` — so
# there is one artifact to build, sign, and promote, not two that can drift.
#
# Base images are pinned by digest so a rebuild of the same commit gets the same bytes; the tag
# in each reference records what the digest was resolved from (Docker ignores it once a digest is
# present), and Dependabot proposes the bumps. No HEALTHCHECK: the worker role has no HTTP surface,
# so each role's probe lives in compose.

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.0@sha256:78a7ff97cd27b7124a5f3c2aefe146170793c56a1e03321dd31a289f6d82a04f /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
# Dependencies first, in their own cached layer - the app changes far more often than the lockfile
# does.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS runtime
# A dedicated unprivileged user; the app owns its tree but starts and stays non-root.
RUN groupadd --system prismoid && useradd --system --gid prismoid --create-home --home-dir /app prismoid
WORKDIR /app
COPY --from=build --chown=prismoid:prismoid /app /app
ENV PATH="/app/.venv/bin:${PATH}" PYTHONUNBUFFERED=1 PRISMOID_ENV=prod
USER prismoid

EXPOSE 8100
# Default to the API; override the command to run the worker (`docker run <img> run worker`). Rule 5
# keeps them separate processes — the API enqueues, the worker executes — from the one image.
ENTRYPOINT ["prismoid"]
CMD ["run", "api", "--host", "0.0.0.0", "--port", "8100"]
