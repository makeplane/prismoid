# web

The Prismoid web app: a React Router v7 single-page app (`ssr: false` in `react-router.config.ts`,
so there is no Node server at runtime). Every route loads its data in the browser from the Prismoid
API, and a task's live view rides the API's event stream (SSE or WebSocket).

One codebase builds **two apps**:

| App | Routes | Dev server | Build output |
|---|---|---|---|
| The product | `app/routes.app.ts` | `pnpm dev` on :5173 | `build/client` |
| The operator console | `app/routes.admin.ts` | `pnpm dev:admin` on :5174 | `build-admin/client` |

`APP_TARGET=admin` picks the console at build time (`app/routes.ts`), so neither bundle carries the
other's code.

## Setup

The web app is one package in the repo's pnpm workspace. Install from the **repo root** (pnpm 12,
pinned in the root `package.json`; `corepack enable` provides it, and npm cannot read the
`catalog:` versions):

```sh
pnpm install --frozen-lockfile
```

The API URL comes from `VITE_PRISMOID_API_URL` and defaults to `http://localhost:8100`, the backend's
dev port. To point elsewhere, copy `.env.example` to `.env` and change it. The backend's
`PRISMOID_API_CORS_ORIGINS` must list the web app's origin (`backend/.env.example` already allows
:5173 and :5174).

## Commands

From the repo root:

| Command | What it does |
|---|---|
| `just dev` | API, worker and this dev server together (see the root README) |
| `just web-dev` | Only this dev server, on :5173 |
| `just web-check` | The web half of `just check`: typecheck, oxlint, the style guards, the contrast audit and the unit tests, for this app and `packages/blocks` |
| `just web-build` | Production build to `build/client` |
| `just web-local` | The build `prismoid up` serves (API and web on one origin, `http://127.0.0.1:8100`) |
| `just export-contract` then `just web-gen` | Re-export the API contract to `docs/api/` and regenerate the typed client |

Inside `web/`: `pnpm dev`, `pnpm dev:admin`, `pnpm build`, `pnpm build:admin`, `pnpm typecheck`,
`pnpm test` (vitest), `pnpm lint` (oxlint), `pnpm format` (oxfmt).

## The API types

`app/lib/api/types.gen.ts` and `app/lib/api/events.gen.ts` are generated from
`docs/api/openapi.json` and `docs/api/events.schema.json` by `pnpm gen` (`just web-gen`). Never edit
a `*.gen.ts` by hand: change the backend, run `just export-contract` (it reads the app's own
OpenAPI schema), then `just web-gen`, and commit both.

## Guards that run in `just web-check`

- `lint:tokens`: no raw palette classes, hex, `rgb(`, `oklch(` or `bg-white` outside the token
  layer in `app/app.css`.
- `lint:radius`: corner radius comes from the `rounded-<step>` tokens only.
- `lint:dash`: no em dash anywhere in `app/`.
- `lint:contrast`: a contrast audit of the colour tokens (`scripts/contrast-audit.mjs`).

The visual and layering rules behind them are in [`DESIGN.md`](DESIGN.md); the folder rules for
contributors (and AI coding tools) are in [`CLAUDE.md`](CLAUDE.md).

## Layout

```
app/
├── routes/        # one folder per route; composition only
├── components/    # kit/ (the domain primitives), layout/, tasks/, artifact/, composer/, ...
├── hooks/         # React hooks; each a thin shell over a pure, tested module
├── lib/           # pure logic and the API client (lib/api/)
├── app.css        # the design tokens
└── routes.ts      # picks routes.app.ts or routes.admin.ts
```

Components come from `@makeplane/propel` (a design system) and `@prismoid/blocks`
(`packages/blocks`), imported by subpath.

## Docker

`web/Dockerfile` builds the static app and serves it with nginx on port **8080**, falling back to
`index.html` for client-side routes (`nginx.conf`). The build context is the repo root, and the API
URL is baked in at build time:

```sh
docker build -f web/Dockerfile --build-arg VITE_PRISMOID_API_URL=https://api.example.com -t prismoid-web .
docker build -f web/Dockerfile --build-arg APP_TARGET=admin -t prismoid-web-admin .
docker run -p 8080:8080 prismoid-web
```

The single-host deploy in `deploy/` builds and runs it for you.
