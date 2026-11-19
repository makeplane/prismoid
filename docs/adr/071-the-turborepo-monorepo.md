# ADR-071 · The Turborepo monorepo

**Status:** accepted · 2026-10-30 · the owner's ask of 2026-10-29 ("convert the repo into a turbo
repo mono repo structure … use pnpm on the frontend, pnpm 12, set up all packages as catalog,
similar to [Plane's]"). The owner ruled on 2026-10-30 that the services stay at the root ("keep them
in the root"). **Amends** nothing: ADR-007's layout stands.

## Context

The web app was a standalone pnpm 10 project: its own lockfile, its own `pnpm.overrides`, version
ranges spread across one `package.json`. Plane's own monorepo runs one pnpm workspace at
the root, with every version in the workspace catalog and Turborepo running the JS tasks. Prismoid now
does the same, without moving any service.

## Decision

1. **The services stay where ADR-007 put them:** `backend/` (uv), `web/` (pnpm) and `sandbar/`
   (Go). No file moved.
2. **One pnpm workspace at the root, pinned to pnpm 12** (`packageManager: pnpm@12.16.1`;
   corepack and `pnpm/action-setup` both read it). `pnpm-workspace.yaml` lists `web` as its one
   package. `backend/` and `sandbar/` are not packages; they keep their own toolchains (uv, Go
   modules), and the justfile drives them as before. There is one lockfile, `pnpm-lock.yaml`, at
   the root.
3. **The catalog holds every JS version.** Each dependency in a workspace `package.json` is
   `catalog:`, and the version lives once in `pnpm-workspace.yaml` (`catalogMode: prefer`,
   `cleanupUnusedCatalogs: true`). Versions are exact, pinned to what the lockfile resolved, like
   Plane's. The security overrides moved from `web/package.json` into the workspace file, each
   bounded to the major its parent declares, with a comment naming the advisory.
   `onlyBuiltDependencies` became pnpm 11+'s `allowBuilds`.
4. **Turborepo runs the JS tasks.** `turbo.json` declares `build`, `dev`, `typecheck`, `lint`,
   the four guard scripts, `test`, and `check`, which depends on all the gate tasks. The root
   `package.json` exposes them as `pnpm build|dev|check|lint|typecheck|test`. `just web-check` and
   `just web-build` call `turbo run … --filter=web`, so the push gate goes through Turbo's graph and
   cache. `test` also hashes `docs/api/**`, so a contract change is never served from a cached
   pass. `APP_TARGET` and `VITE_PRISMOID_API_URL` are `globalEnv`: they change the bundle, so they
   change the hash.
5. **The web image builds from the repo root.** The lockfile and catalog live there, so
   `web/Dockerfile` runs with context `.`. It copies the workspace manifests, runs
   `pnpm install --frozen-lockfile --filter web`, and then copies `web/`.
   `web/Dockerfile.dockerignore` keeps that context to what the build copies. The backend,
   sandbard and frame images keep their own directories as context.

## What moved with it

- CI: `ci.yml` installs the workspace at the root with no pnpm version in the workflow (the
  action reads the pin). An image build of the web app uses context `.` and
  `file: web/Dockerfile`.
- `deploy/compose.prod.yaml`: the web services use context `..` and `dockerfile: web/Dockerfile`.
- `.gitignore` ignores `.turbo/`, and `web/README.md` installs with pnpm at the root.

## Consequences

- External scripts that ran `docker build web/` or `cd web && npm install` must build from the
  root with `-f web/Dockerfile` and install with `pnpm install` at the root (npm cannot read
  `catalog:`). A deployment that only pulls images is untouched.
- A branch that edits `web/package.json` dependencies must move the version into the catalog.
- When shared JS code appears, it becomes a workspace package (Plane's `packages/*`).
- `turbo` is a dev-only JS dependency (HANDOFF §3 row).
