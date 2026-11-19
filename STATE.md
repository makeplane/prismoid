# STATE - what is true right now

_Read this first, before any change (the root `CLAUDE.md` opens with that rule). When a workstream
lands, update this page in the same change: what is built, what left "experimental", what is still
missing. This page holds only what is true today. The reasons live in the decision records
([docs/adr/](docs/adr/README.md)), and the release notes in [CHANGELOG.md](CHANGELOG.md)._

## What Prismoid is

Prismoid is a self-hosted digital worker. You give it an outcome; it plans the work into steps, runs
each step with an AI agent inside a sandboxed computer (a Machine), publishes what the agents made,
then checks every artifact again and signs the verdict. It is free software under `AGPL-3.0-only`.

## What is built and supported

**Two services, one repo** (ADR-007):

- `backend/`: one Python 3.12 package, `prismoid`, run as two processes: the API (FastAPI: REST, SSE,
  WebSocket) and the worker (durable jobs on Postgres through DBOS, reached only via
  `jobs/durable.py`, ADR-069). The engine (`prismoid/ai`), the `prismoid` CLI and the migrations live here.
- `web/`: the product web app and the operator console, single-page React Router apps from one
  codebase, typed from the API contract in `docs/api`. With `packages/blocks` it forms a pnpm and
  Turborepo workspace (ADR-071) on Plane's Propel design system (ADR-074).
- `sandbar/`: a standalone sandbox daemon in Go, its own subproject with its own rules file.

**Where it runs** ([docs/architecture.md](docs/architecture.md), "Deployment shapes"):

| Shape | How | Machines |
|---|---|---|
| Dev stack | `just up` + `just dev`, on macOS or Linux with Docker | `docker` |
| Local mode | `prismoid up` on one Mac: embedded Postgres, files on disk, no Docker (ADR-077) | `device` |
| Single host | `deploy/compose.prod.yaml` behind Caddy ([deploy/DEPLOY.md](deploy/DEPLOY.md), [PROVISION.md](deploy/PROVISION.md)) | `sandbar` on its Docker backend |

**Machine drivers** (`PRISMOID_WORKER_FRAME_DRIVER`, ADR-063):

| Driver | Status |
|---|---|
| `docker` | Supported for development and trusted users. Containers share the host kernel |
| `sandbar` | Supported on servers with its Docker backend: the daemon owns the Docker socket and gives each sandbox its own network. The live screen shows the browser and lets a person take it over (ADR-065) |
| `device` | Supported on macOS through `prismoid up`: the person's own Mac, every agent command under a Seatbelt profile, driven by its owner only and never shared into a Project (ADR-078) |

**What a task can do:** have each turn decided by one model run, the Lead, which ends in a reply, a
plan, one batch of questions or a proposed task (ADR-092) · run a plan's steps in parallel on four
step agents (researcher, writer, maker, judge, ADR-091) and deliver the answer first, with
verification signing each artifact afterwards (ADR-019) and fleet mode for every model at once
(ADR-010) · survive a worker restart or a park mid-step, resuming from the step's call log
(ADR-089) · ask one numbered batch of questions, park
for approvals, and take steering, pause, cancel, follow-ups and forks at safe points (ADR-064) ·
keep one Machine across its steps and turns, asleep when idle, with per-step egress allowlists,
and run on paper when a step needs no computer (ADR-063) · publish documents (with full-fidelity
renditions and PDF), spreadsheets, decks, collections, web pages and code, with revisions · use
any model in the catalog, metered in native units and US dollars (ADR-024) · read the web through
`web.fetch`, `web.search` and the computer's browser, with `[S#]` citations back to the pages it
read (ADR-046, ADR-047, ADR-066) · call
remote MCP servers as connectors with a per-tool policy (ADR-020, ADR-054). Around tasks: skills,
bots, conversations, webhook and Telegram channels, templates (ADR-045, ADR-056 to ADR-060),
routines (ADR-048), workspaces as the tenant and projects as shared folders (ADR-061, ADR-062).

**Accounts and spend:** outside dev, signup stays closed until the operator opens it
(`PRISMOID_AUTH_SIGNUP_ENABLED`); an open door can cap the workspaces a person founds, and a new
address is proved by an emailed code before it sends invites (ADR-086). Spend is metered in dollars
on every task; task and Project budgets park work at their ceiling. A workspace has no spend
allowance here (`operations/allowance.py`, ADR-103): that seam is where a distribution meters one.

**Notifications and the phone app:** the inbox pushes each new item to the recipient's phones
through FCM once the operator sets `PRISMOID_PUSH_CREDENTIALS_JSON` (ADR-100); without it push is
off and its routes answer 404. The phone app registers its device (`/v1/push/devices`) and finishes
its own connector sign-in (ADR-020, amended 2026-11-18).

**Logs:** with `PRISMOID_LOG_FORMAT=json` every line on stdout is a JSON record carrying its trace
and span ids; spans leave only by OTLP (ADR-101).

## The single-host stack on sandbar: what is proven

**Proven** on 2026-11-12, before the Lead and the step agents (ADR-089 to ADR-092) replaced the
planner, on a Linux Docker Engine (OrbStack's, arm64), with `deploy/compose.prod.yaml` built from
this tree, SearXNG for search and one model key: a fresh box's first boot, `/healthz` and the engine
floor check; the sandbox conformance suite against sandbar (every clause but native sleep, since
sandbar sleeps a Machine by archive); `prismoid dev frame-selftest --egress`; `just smoke`; computer
tasks that browse and run a shell on a Machine, asking for approval before a new host; a Machine
that sleeps after its idle time with a checkpoint and wakes cold with `/work` intact; a sandbar
restart, after which the next step's allowlist is pushed before its first browser call; and web
search through SearXNG. With a provider search engine, "search the web for this week's Go release
notes and write me a memo" searched, read the release notes and delivered them.

**Not yet proven:** the same walk on the current engine, Docker CE on a Linux host, `deploy/deploy.sh` on Linux, and the AWS walk in
[deploy/PROVISION.md](deploy/PROVISION.md) ("On AWS (EC2)") on a real instance.

**Known limits:** a sandbar restart in the middle of a step leaves that step's sandbox with an
empty allowlist until the next step (it fails closed: the browser is refused); a link click to a
new host is refused with no approval ask (only opening a page asks); `deploy.sh`'s health gate
cannot pass a dry run with Caddy's `local_certs`.

## Experimental, or not yet

- **sandbar's Firecracker backend** is experimental: evaluate it, do not serve users with it
  (sandbar/README.md, support matrix).
- **gVisor for sandbar's Docker backend** (`SANDBAR_DOCKER_RUNTIME=runsc`) is documented as
  optional hardening, not yet proven with Prismoid's stack.
- **The email channel** is refused until inbound mail carries a sender-authentication verdict
  (SPF, DKIM, DMARC). Webhook and Telegram channels are available.
- **`prismoid up` on Linux:** local mode is macOS only.
- **Public container images:** none are published; every image builds on your machine.
- **sandbar on more than one host:** a deployment runs one sandbar daemon.
- **The whole-desktop screen:** on sandbar the screen is the browser. The web's VNC pane and the
  relay's control stream are built, waiting for a driver whose door serves one.
- **More than one worker:** add API replicas freely; a second worker waits on the two-worker
  chaos drill (ADR-009).

## How to verify

| When | Run |
|---|---|
| Before every commit | `just check`: lint, strict types, import layers, audit greps, the quality ratchet, unit tests and laws (every source file's copyright header among them, ADR-102), the web checks. CI runs it on every push and pull request; the Copyright workflow checks the headers as its own status |
| Before a workstream merges | `just test`: the whole backend suite, integration tests included (needs `just up`; refuses while `just dev` runs) |
| A sandbar change | `just sandbar-check` |
| A user-visible change | One real run: `just smoke` or the CLI flow. An agent, prompt or skill change adds `just evals` |
| The drills | Nightly in CI: evals, `just perf`, `just e2e`, the frame integration tests. Weekly: `just chaos`. On demand: `just load`, `just endurance`, `just backup-restore`, `just fresh-clone`, `just range` |

Red means stop and fix it before new work.

## Read next

[README.md](README.md) (setup) · [CONTRIBUTING.md](CONTRIBUTING.md) · `HANDOFF.md` §1-2 (the
vocabulary and the rules that outrank convenience) · [docs/architecture.md](docs/architecture.md)
· [docs/engine-tour.md](docs/engine-tour.md) · [docs/security/threat-model.md](docs/security/threat-model.md).
