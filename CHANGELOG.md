# Changelog

All notable changes to Prismoid are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html), tagged `vX.Y.Z`. Each version's section
is written by release-please from the conventional commits merged since the previous release, in
the release pull request that bumps the version (`.github/workflows/release.yml`); edit the wording
there before merging it. Every new section appears above the one before it.

## 0.1.0 (first public release)

The version this repository starts at, written by hand. What is experimental or not built yet is
listed in [STATE.md](STATE.md); what comes next, in [ROADMAP.md](ROADMAP.md).

### Added

- **Tasks that plan, run and verify.** Each turn is decided by one model run, the Lead, which ends
  in a reply, a plan, one batch of questions or a proposed task. A plan's steps run in parallel
  where they can, on four step agents (researcher, writer, maker, judge), inside sandboxed per-task
  computers (Machines). The answer is delivered first; a separate job then re-checks every artifact
  and signs its verdict with Ed25519.
- **Durable execution on Postgres** with DBOS: a task survives a worker crash or restart, a step
  resumes from its call log instead of starting over, and every DBOS call goes through one module
  (`backend/prismoid/jobs/durable.py`).
- **Questions, approvals and steering.** A task asks one numbered batch of questions when it needs
  to, parks at no cost while it waits, and accepts steering, pause, resume, cancel, follow-ups and
  forks at safe points.
- **Machines and their drivers:** `docker` (zero setup, for development), `sandbar` (a standalone
  sandbox daemon in `sandbar/`, with a Docker backend and an experimental Firecracker backend; the
  server stack runs it) and `device` (the user's own Mac). A Machine is remembered across a task's
  steps and turns, sleeps when idle, and keeps per-step egress allowlists; a step that needs no
  computer runs without one.
- **sandbar's file door runs as the agent.** An agent's file reads and writes run inside the
  sandbox as the agent's own user, so the kernel decides what it may touch; a read is bounded in
  size, the browser's profile is unreadable to the agent, and a file route takes only clean
  absolute paths.
- **Prismoid fully local (`prismoid up`, macOS):** an embedded Postgres reached over a Unix socket only,
  files on disk instead of object storage, and the Mac itself as the Machine, with every agent
  command inside a Seatbelt sandbox profile.
- **A self-hosted server stack** (`deploy/`): Docker Compose behind Caddy, with Silo or any S3 for
  storage and sandbar for Machines, built from this tree on the host.
- **A live screen** for browser Machines, with a control lease so a person can take over and hand
  back.
- **Artifacts of many kinds:** documents (with full-fidelity renditions and PDF), spreadsheets,
  decks, collections, web pages and code, each with revision history; a long-form lane for
  documents of dozens of pages; a QA gate that looks at the rendered page.
- **The model plane:** models and prices are catalog data (`prismoid models add`, `prismoid models seed`);
  Anthropic, OpenAI, Google and any OpenAI-compatible server; per-provider concurrency limits; a
  usage ledger in native units and US dollars; fleet mode, where every enabled model answers.
- **Web access for agents:** `web.fetch` behind an egress guard that re-checks every redirect, and
  `web.search` across configurable engines (Brave, Tavily, Exa, Perplexity, Parallel, SearXNG and
  provider-native search).
- **Connectors:** users attach remote MCP servers, with encrypted credentials, a catalog,
  one-click OAuth and a per-tool policy that can require the user's approval.
- **Skills and bots:** built-in and user-authored skills (publishing requires green evals), and
  bots, user-authored agents stored as data, with conversations, channels (webhook and Telegram)
  and templates.
- **Routines:** standing work on a schedule or a trigger; every run is an ordinary task with a
  budget, and every skipped run is recorded with its reason.
- **Workspaces and Projects:** workspaces as the tenant boundary, projects as shared folders with
  owner, editor and viewer roles, invite links and live presence.
- **Accounts and access:** argon2id passwords, opaque hashed session and API tokens, password
  reset by emailed single-use code, single-use stream tickets so no credential rides a URL, and an
  operator console built as a separate app. Outside dev, signup stays closed until the operator
  opens it; an open signup can cap founded workspaces, and an emailed code proves a new address
  before it sends invites. The first platform admin is created on the server with
  `prismoid admin bootstrap`.
- **Metering:** usage is metered in native units and US dollars on every task, and task and
  Project budgets park work at their ceiling.
- **The inbox on your phone:** with a Firebase key set, inbox items push to the recipient's own
  phones (FCM), and the phone app finishes a connector's sign-in itself.
- **Logs on stdout as JSON records** outside dev (`PRISMOID_LOG_FORMAT`), carrying trace ids; spans
  leave only by OTLP.
- **The web app:** a React Router single-page app on Plane's Propel design system and the shared
  `@prismoid/blocks` package, in a pnpm and Turborepo workspace.
- **Tooling:** `just check` (lint, strict types, import layers, audit greps, a quality ratchet,
  unit tests and the web checks), per-folder rules files, an automated reviewer, evals and
  problem books, and drills for performance, load, chaos, endurance and backup/restore.
