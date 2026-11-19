# ADR-020 · Connectors: external MCP servers as user-attached tool surfaces

**Status:** accepted · 2026-08-08 · amended by ADR-054 (2026-08-30: the hosted catalog UI,
one-click connect, per-tool policy + approval gates, prompt-based auto-attach, disable/enable,
and the needs_reauth inbox knock land there; delegation, stdio, RFC 7009, and per-step scoping
stay deferred)

## Context

Agents can only use built-in frame tools. Users want their tasks to reach the outside —
Gmail, Notion, Linear — the way Perplexity's connectors do. Under the hood every such surface
is a **remote MCP server**; the runtime we already ship (`pydantic-ai-slim`) names MCP in its
§3 role and exposes a first-class client (`MCPToolset`) behind an extra we had simply not
installed. The first build's environment list reserved `{base}/v1/connectors/oauth/callback` from the start — this
ADR makes the stub real.

## Decision

**One dependency move.** `pydantic-ai-slim[anthropic,openai,google]` grows the `mcp` extra,
which pulls `fastmcp-slim[client]>=3.3` carrying the official `mcp` SDK (`>=1.24,<2`).
Chosen over: a direct `mcp` pin (the runtime's own extra keeps versions co-resolved with the
agent loop); FastMCP's `auth='oauth'` helper (interactive-only: localhost callback server +
browser launch — unusable in a server); LangChain-style tool adapters (second brain, already
rejected). `authlib` arrives transitively and is **never imported** — the `just audit` ban
stands; our OAuth legs are four typed httpx calls parsed with `mcp.shared.auth` models, and a
dependency would not remove that code, only hide it.

**Server-side OAuth 2.1, hand-driven in `operations/connectors.py`.** Discovery (RFC 9728
protected-resource metadata from the `WWW-Authenticate` hint or well-knowns → RFC 8414
authorization-server metadata with path-insertion variants and the OIDC fallback → the MCP
spec's default endpoints last), dynamic client registration (RFC 7591; pre-registered
client_id/secret accepted for DCR-less servers), PKCE S256, `resource` (RFC 8707) on the
authorize and token legs, refresh-token grant with rotation-safe persistence. The SDK's
`OAuthClientProvider` was rejected for this path: it is an `httpx.Auth` whose flow awaits a
redirect handler and a callback handler inside one request lifecycle — a web app's two-request
shape would use it only to hide the persistence we must write anyway. The deterministic
halves (PKCE, discovery URL derivation, authorize-URL and token-request assembly) are pure
functions in `schemas/connectors.py` (the ADR-001 seam-2 pattern), unit-tested without I/O.

**The callback is the fourth unauthenticated route.** A browser returning from an IdP carries
no bearer token, so `GET /v1/connectors/oauth/callback` joins healthz/login/signup in the
Rule-14 exemption list (CLAUDE.md + HANDOFF §2 amended in the same commit that lands the
route). Its auth is the `state` parameter: an opaque `cn_`-prefixed token, sha256 at rest,
**single-use** (atomic UPDATE … RETURNING claim), 10-minute TTL, bound to one connector row.
The route stays in the OpenAPI schema (the op-count invariant counts it).

**The first at-rest cipher.** OAuth tokens must be reversible, so hashing (the house norm for
credentials) cannot apply; `operations/cipher.py` introduces Fernet (from `cryptography`,
already §3 — its row's role is widened) behind the signing module's key ladder: config →
key file → generate-once 0600 + warn. MultiFernet-shaped from day one and a `key_id` column
on every ciphertext row, so rotation is an operational act, not a migration — the open item
recorded at `signing.py:99` learned from. Fernet over raw AESGCM: misuse-resistant (no nonce
management), versioned, boring.

**Ownership is personal.** `connectors.owner_actor_id → actors.id ondelete=CASCADE` — tokens
die with the user; only the owner's tasks may attach the connector. Space-sharing is a real
credential-delegation decision, deferred deliberately (see Consequences). Per ADR-014, this
per-actor table names its tenancy path: workspace tenancy adds a `workspace_id` column here;
nothing else changes.

**Attach is explicit; tools mount at the one seam.** `TaskCreateIn.connectors` (ids or slugs,
mirroring `skills` refs) → validated at the door → `tasks.connector_ids` → follow-ups merge,
forks filter to still-live (the disabled-seat rule: a dead connector must not orphan a
branch). At step time `operations` resolves rows into frozen `ConnectorSpec` values whose one
secret field (`authorization: SecretStr | None`, the full header value) collapses bearer and
OAuth into one shape the engine cannot distinguish or leak; `TaskDeps.connectors` carries
them across the persistence-free boundary, and `ai/agents/base.build()` — the only agent
constructor — mounts one prefixed `MCPToolset` per spec, holding only the inventoried tools;
every call passes the `tool_record` capability, which journals it as the existing durable
`tool.called` (arg **keys**, never values — Rule 9). (Amended 2026-11-14: the warden is gone.) Zero new wire event variants.

**Egress posture, stated plainly.** MCP tool calls execute in the worker process and do not
traverse the frame egress proxy — by design, not by accident. The controls are: the SSRF
guard at create/authorize time (https-only, localhost only under a dev flag, private/
link-local/metadata ranges refused, no URL userinfo, redirects never followed on the auth
legs), the grant (only inventoried tools mount, only on tasks the owner attached them to),
and the probe-time sanitization of server-supplied text (length caps, control characters
stripped) before it reaches any prompt.

## Consequences

- One HANDOFF §3 row for the carried SDK + the amended runtime and `cryptography` rows; this
  ADR is their "why this, not the alternative".
- The redirect URI is built in `operations/connectors.callback_url()` from `PUBLIC_BASE_URL` —
  not `api/links.py`, which operations may not import (layers); the URL-topology law is
  satisfied by the settings-derived f-string (no scheme literal).
- The full surface ships at once: API (`/v1/connectors` CRUD + authorize + probe + the
  callback), CLI (`computer connectors add|authorize|list|show|probe|remove` with a
  `default_connectors.yaml` name catalog — data, not a deploy — and `do --connector`), and web
  (Settings → Connectors panel with the browser-consent poll; a composer ConnectorsDial on the
  landing box and the follow-up, where connectors are a fresh-turn dial like skills).
- `COMPUTER_CONNECTORS_*` settings join the environment keys (`ENCRYPTION_KEY`, `KEY_PATH`,
  `ALLOW_INSECURE_LOCALHOST`, `PROBE_TIMEOUT_S`); the Logfire scrub patterns grow `cn_` and
  the Fernet `gAAAAA` prefix.
- A retried step may re-execute a side-effectful connector tool (send an email twice) — the
  same posture `shell` has always had; per-tool idempotency is future work, recorded here so
  it is a known trade, not a surprise.
- Mid-run auth expiry costs one honestly-journaled failed tool call; the row is stamped
  `needs_reauth` at the next mount. The refreshing-`httpx.Auth`-with-write-back design
  (a persistence callable on `TaskDeps`) is the recorded deferral if that window ever hurts.
- **Deferred, deliberately:** space-shared connectors (credential delegation needs its own
  decision) · stdio/local transports (running user-configured processes on the worker is a
  separate security story) · prompt-based auto-attach (Phase 2 — rides the skill router:
  `operations/skills.route` + `planning._auto_pins`) · RFC 7009 token revocation on delete ·
  per-step connector scoping (StepSpec untouched) · DNS-rebinding-grade SSRF (resolve-time
  pinning) · inbox notification on `needs_reauth` · a hosted connector catalog UI (the CLI
  ships a name→URL yaml only).

**Amended 2026-11-04 (security review, an owner ruling).** The
DNS-rebinding deferral is closed. Outside local mode the parse-time door also refuses the
`localhost` name over https, and every MCP and OAuth client the connectors door builds runs on
`ai/tools/pin.py`'s `PinnedTransport`: each request (a followed redirect included) is resolved
once, every answer goes through `schemas/egress.refuse_ip`, and the connection goes to the
vetted address with the name on Host and SNI. A refusal names the host, never the address it
resolved to. Local mode (`prismoid up`, `PRISMOID_ENV=local`) sets
`PRISMOID_CONNECTORS_ALLOW_INSECURE_LOCALHOST` for its children, so a loopback MCP server on the
person's own machine keeps working; elsewhere the flag stays a dev opt-in, off by default.

**Amended 2026-11-15 (ADR-092):** The skill router is deleted: skills are pinned per step from the Lead's plan (`ProposedStep.skills`, resolved at their head at the commit) and from a user-dialed kind's format skill; nothing auto-routes a task's skills.

**Amended 2026-11-18: the phone app finishes its own sign-in (RFC 8252).** The
browser-binding cookie (2026-11-05) cannot work from the phone: the app starts the flow over its
own HTTP client and shows the consent page in the system's sign-in browser, which never saw the
cookie, so every OAuth connect from the app was refused. `connect` and `authorize` now take an
optional `app: {code_challenge, return_scheme}`: the S256 hash of a secret only the app holds, and
which app build it is (`prismoid`, `prismoid-dev`, `prismoid-staging` - a fixed list,
`schemas/connectors.APP_RETURN_SCHEMES`; the redirect target is never read from a request). The
flow keeps the challenge instead of the binding. Its callback exchanges nothing: it seals the code
into the flow for five minutes, mints a single-use `ca_` return code (stored hashed), and sends the
browser to `{scheme}://connectors/return?connector_id=…&ok=1&return_code=…` (a refusal goes there
too, `ok=0`, the row carrying the reason). `POST /v1/connectors/{id}/oauth/finish
{return_code, code_verifier}` exchanges it only for the owner (`Connector.get_for`), with both
matching, inside the window; any miss spends the code. A shared consent link still lands nothing:
whoever approves it hands the code to their own app, which neither owns the connector nor holds
the secret, and the owner never sees the return code. The answers say `app_sign_in: true` when the
app flow was taken, so the app can tell an older server apart. The web's flow, cookie and landing
are unchanged.

