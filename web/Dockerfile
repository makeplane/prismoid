# syntax=docker/dockerfile:1
# The web app — a static SPA (react-router ssr:false), built once and served by nginx.
#
# The API URL is baked at BUILD time (Vite inlines VITE_PRISMOID_API_URL into the bundle), so the
# image is target-specific: build with the URL a BROWSER will use to reach the API. Default is the
# single-box compose value; a real deploy passes its own:
#   docker build -f web/Dockerfile --build-arg VITE_PRISMOID_API_URL=https://api.example.com -t web .
#
# TWO applications, one Dockerfile (app/routes.ts): APP_TARGET=admin builds the operator
# console — a separate bundle for a separate domain; the default builds the product. The
# public image never contains a byte of admin code, and vice versa:
#   docker build -f web/Dockerfile --build-arg APP_TARGET=admin -t web-admin .
#
# The context is the repo ROOT (ADR-071): the lockfile and the catalog live in the pnpm workspace
# there. Dockerfile.dockerignore beside this file keeps the context to what the build copies.

# Base images are pinned by digest so a rebuild of the same commit gets the same bytes; the tag
# in each reference records what the digest was resolved from (Docker ignores it once a digest is
# present), and Dependabot proposes the bumps.
FROM node:22-alpine@sha256:0e1754758c2df4cfa3e68d59e2e1b7374bb7fd9ea6328eaaacf15cc934af8202 AS build
WORKDIR /app
RUN corepack enable
COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./
COPY patches patches
COPY web/package.json web/
COPY packages/blocks/package.json packages/blocks/
# web... = web and the workspace packages it depends on (@prismoid/blocks, ADR-074).
RUN pnpm install --frozen-lockfile --filter web...
COPY packages packages
COPY web web
WORKDIR /app/web
ARG VITE_PRISMOID_API_URL=http://localhost:8100
ENV VITE_PRISMOID_API_URL=${VITE_PRISMOID_API_URL}
ARG APP_TARGET=app
# The admin build lands in build-admin/ (react-router.config.ts); normalize so the runtime
# stage copies one path whichever app this image is.
RUN if [ "$APP_TARGET" = "admin" ]; then pnpm build:admin && rm -rf build && mv build-admin build; else pnpm build; fi

# nginx-unprivileged: the same nginx, with the master process running as uid 101 instead of root
# and its pid and temp files under /tmp, so the container never holds root. It cannot bind below
# 1024, which is why the server listens on 8080.
FROM nginxinc/nginx-unprivileged:1.31-alpine@sha256:b5047b9b76845eac124358373c6852377c52e4b05834a47647e955dd91cb8f1e AS runtime
# SPA fallback: every route serves index.html so client-side routing works on a hard refresh.
COPY web/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/web/build/client /usr/share/nginx/html
EXPOSE 8080
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD wget -qO- http://127.0.0.1:8080/ >/dev/null || exit 1
