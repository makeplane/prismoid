# ADR-082 · Stream tickets, and release PRs with release-please

**Status:** accepted · 2026-11-05 · the code-quality pass before the repository goes public.
**Amends** ADR-031 (the stream's `?token=` concession is gone; the 10 s re-proof stays) and the
root CLAUDE.md door list (a new door, below; the owner adds the bullet).

Two unrelated decisions from the same audit (security I, releases M) share this record because
they landed in one lane.

## Part A · Stream tickets

### Context

A browser's `EventSource` cannot set an `Authorization` header. Since the stream was built, the
web put its long-lived `cs_` session token in the stream URL (`/v1/tasks/{id}/events?token=…`,
the same for conversations), and `require_principal` honoured `?token=` on those routes only. The
access log and the span redacted the value, but a URL also lands in proxies, browser history and
any tool between the browser and the API that we do not control, and the token in it is good for
days. The screen socket (ADR-065) had already solved the same problem for itself with a
single-use ticket.

### Decision

1. **A stream ticket, `et_`.** `POST /v1/tasks/{id}/events/ticket` and
   `POST /v1/conversations/{id}/events/ticket` (an Actor; the subject is fetched through its one
   fetch path first, so a stranger meets its 404) answer `{ticket, expires_in}`. The ticket is the
   32-byte `secrets` family, prefixed `et_` (unused by any other credential, ticket or slug:
   `cs ck cn cb cr rt bc oi si bt st dt` are taken), stored only as its SHA-256 in
   `stream_tickets`, with the **credential row** that minted it (`auth_sessions.id` or
   `api_keys.id` and which of the two, never the token), the actor, the subject (`task` or
   `conversation`, a vocabulary CHECK) and `expires_at = now() + 30 s`.
2. **Single-use across replicas.** The door redeems with one `DELETE … WHERE token_hash = $1 AND
   expires_at > now() RETURNING …`, committed before anything else is checked, so two racing
   streams cannot both open on one ticket on any number of API replicas, and a ticket presented
   at the wrong stream is spent too. Postgres `now()` is the clock for the mint and the check.
   This is stricter than the screen ticket, whose spent cache is per process; the cost is one
   indexed write per stream open, which a stream that lives for minutes can afford.
3. **The redeem proves the credential row.** `operations/stream_tickets.prove` re-reads the row by
   id (`AuthSession.live_principal`, `ApiKey.live_principal`), checks it still belongs to the
   ticket's actor, that the account is active and that a workspace is left to act in, and binds
   the session as the door binds a request (an API key is pinned to its workspace). Every refusal
   is one 401, "this stream ticket is invalid or has expired".
4. **The stream keeps its re-proof, by row.** Every stream (SSE task, SSE conversation, the task
   WebSocket) now holds the credential row it opened with, whether that came from the header or a
   ticket, and re-proves it with `prove` every `stream_reauth_seconds` together with the subject's
   own fetch and the workspace fence. A logout, a password reset, a revoked key, a suspension or a
   lost membership still severs an open stream within one interval. The stream no longer holds
   the raw token in memory for its lifetime.
5. **`?token=` authenticates nothing.** `require_principal` reads the `Authorization` header and
   nothing else, on every route. The stream routes take the header (the CLI, curl, API keys) or
   `?ticket=`; the task WebSocket the same. The ticket is read from the query inside the door,
   never declared as a parameter, so no endpoint argument carries it into a span. `ticket=` was
   already in the redaction list (access log, uvicorn's WebSocket line, the span's URL
   attributes); `token=` stays there for old clients; `et_` joins the Logfire scrub patterns,
   anchored at a word start so an identifier like `get_…` is not scrubbed.
6. **The web reopens, the browser does not.** A ticket opens one stream once, so the browser's
   native retry would carry a spent ticket. `use-task-stream` and `use-conversation-stream` close
   the source on error and reopen it with a fresh ticket from the newest durable seq they hold
   (`?since=`), backing off from one second to thirty (the conversation keeps its polling
   fallback). `Last-Event-ID` still works for any client that keeps one connection. The minimal
   watch page and the transcript replay follow suit (the replay is a `fetch`, so it sends the
   header).

### Alternatives rejected

- **Reuse the screen ticket's signed, stateless format.** No table, but single-use only per
  process, and a stream ticket would carry claims nobody reads. The table is the simpler correct
  thing here.
- **A cookie for the stream.** Needs CSRF thinking and a cross-origin cookie for the SPA on another
  origin; the ticket keeps the one bearer model.
- **Keep `?token=` for scripts.** Scripts can set a header; keeping it would keep the leak path
  open for anyone who copies the old curl line.
- **A `fetch`-based SSE client in the web** (headers allowed). A new dependency or our own parser,
  and the reconnect contract to rebuild; the ticket keeps `EventSource`.

### Consequences

- A new table, `stream_tickets` (identity tenancy, like `auth_sessions`), migration
  `stream_tickets`. Expired, unredeemed rows are swept in batches of 100 by later mints
  (`SKIP LOCKED`, so mints never queue on each other).
- Each stream open costs one POST before it. Reconnects now come from the hooks, not the browser.
- The OpenAPI contract gains the two mint routes and `StreamTicketOut`, and loses the hidden
  `token` query parameter (and the 422 it implied) from every route.
- Proposed root CLAUDE.md Auth addition (the owner's to make): see the lane report.
- Follow-up: `operations/screen._principal` is the same check as `stream_tickets.prove`; the
  screen split can reuse it.

## Part B · Release PRs with release-please

### Context

No tags, no release automation; the version was a static `0.1.0` in `backend/pyproject.toml`,
`prismoid.__version__` and the OpenAPI `info.version`, and `CHANGELOG.md` was one hand-written
"Unreleased" section.

### Decision

1. **release-please** (`googleapis/release-please-action@v4`, CI only) in manifest mode
   (`release-please-config.json`, `.release-please-manifest.json`), one package at the root,
   `release-type: simple`, tags `vX.Y.Z` with no component, `bump-minor-pre-major` (a breaking
   change bumps the minor while the version is 0.x). Its sections follow the conventional commits
   the repository already uses (`feat` Added, `fix` Fixed, `perf`/`refactor`/`revert` Changed;
   docs, tests, CI and chores hidden). release-please reads only the commits on `main` itself and
   never looks inside a merge commit (the first run after a merge-commit PR saw one unparseable
   `Merge pull request ...` and skipped), so pull requests are squash-merged with the PR title as
   the commit title and an empty body: the title is the changelog line.
2. **Static version files, bumped by the release PR.** `version.txt` (the simple strategy's own
   file), `backend/pyproject.toml` (`project.version`), `backend/uv.lock` (the `prismoid` package's
   version, so a locked sync stays fresh) and `prismoid.__version__` (an `x-release-please-version`
   annotation). No runtime or build dependency is added. `hatch-vcs` was the alternative: it
   derives the version from the tag at build time, but it would be a new build dependency, an
   install outside a git checkout (the Docker build context) needs `SETUPTOOLS_SCM_PRETEND_VERSION`,
   and the version would no longer be readable in the tree.
3. **The OpenAPI document versions the contract, not the product.** `info.version` is now the
   protocol version `X-Prismoid-Protocol` answers (`2`), so a release never changes the frozen
   contract or the web's generated types, and the CI contract check stays green on release PRs.
4. **`.github/workflows/release.yml` opens release PRs only and never publishes.** On a push to
   main it opens or updates the one release PR (`skip-github-release`). Tagging is a separate,
   manual `workflow_dispatch` with `tag` checked, after the release PR has merged: it creates the
   tag and a **draft** GitHub release with the CHANGELOG section (`draft` + `force-tag-creation`),
   which a person publishes or discards. No package, image or asset is built; images stay the
   existing Branch Build's job. `runs-on: blacksmith-4vcpu-ubuntu-2404`; actionlint clean.
   (Amended 2026-11-17, an owner ruling: the push trigger is removed and the workflow runs only by
   `workflow_dispatch`. A run with `tag` unchecked opens or updates the release PR, as a push did;
   a push to main now starts nothing, so a release PR exists only when someone sets out to cut a
   release, and it refreshes only when someone runs the workflow again.)
5. **CHANGELOG.** The hand-written section becomes `0.1.0 (baseline)`; release-please inserts each
   new version above it.

### Owner steps and decisions

- **Baseline tag.** Before the first run, tag the commit this lands on as `v0.1.0` (or add
  `"bootstrap-sha": "<that sha>"` to the config). Without either, the first release PR's changelog
  walks the whole history.
- **`RELEASE_PLEASE_TOKEN`.** A PR opened with `GITHUB_TOKEN` does not trigger CI. Add a
  fine-grained token secret to have CI run on release PRs (the workflow falls back to
  `GITHUB_TOKEN` without it).
- **Draft vs published releases**, and whether Branch Build's own release step should then edit
  the draft rather than write its own notes.
- The `uv.lock` filter compares `@.name.value`, not `@.name`: release-please's TOML parser wraps
  every value as `{start, end, value}`. A `release-pr --dry-run` showed the plain `@.name` form
  matches nothing; the `.value` form bumps exactly the `prismoid` package's line. `force-tag-creation`
  is exercised only by the first tagging run.

### Dependency

`googleapis/release-please-action` (v4, CI only, Apache-2.0): a row in HANDOFF §3.
