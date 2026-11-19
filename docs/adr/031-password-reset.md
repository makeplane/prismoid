# ADR-031 · Forgotten passwords: an emailed single-use code, and the product's first outbound mail

**Status:** accepted · 2026-08-16

## Context

Auth shipped complete except for the one door every real product needs: a user who forgot
their password had no way back in — nothing in the API, the CLI, or the web app, and no
outbound email anywhere in the system (`InboxItem` explicitly disclaims sending). Locally the
gap is absolute: you cannot even cheat, because nothing ever prints a recovery secret.

## Decision

**The `cn_` pattern, applied to the front door.** ADR-020 already solved "authenticate a
request that cannot carry a bearer token": an opaque prefixed token from the 32-byte
`secrets` family, sha256 at rest, TTL-bound, single-use via an atomic UPDATE…RETURNING claim.
The reset code is that shape with a `pr`-style prefix spelled `cr_` — chosen so the Logfire
scrubber's `c[skn]_` class widens by one character (`c[sknr]_`) instead of growing a second
pattern. A six-digit "verification code" was rejected: low entropy demands argon2 hashing plus
attempt counters, i.e. more machinery for strictly less security than 32 bytes the user
copy-pastes from an email anyway.

**Two unauthenticated routes** (Rule 14 amended, same commit): `POST /v1/auth/forgot-password`
— always 202, mints into the new `password_resets` table (row per request; re-request
supersedes the pending code; ≤5/user/hour durable throttle plus the login fixed-window speed
bump), then emails the code. `POST /v1/auth/reset-password` — claims atomically, re-hashes
with argon2id, **revokes every live session** (new `AuthSession.revoke_all_for`), sends a
"your password was changed" notice, and answers one indistinguishable 401 for
invalid/expired/replayed/superseded. No user enumeration on either leg.

**Email is a backend switch, not a dependency.** `computer/mailer.py` joins the layer contract
as a sibling of `storage.py`. `COMPUTER_EMAIL_BACKEND=console` (the dev default) prints the
rendered message to the API stdout — that IS delivery on a laptop, a deliberate and scoped
Rule-9 exception: structured logs stay scrubbed, the database stores only hashes, and the
plaintext appears exactly where the developer is looking (`/tmp/computer-api.log` under
`just dev`; the print is `flush=True` because nohup makes stdout block-buffered — found live).
`smtp` is stdlib `smtplib` over STARTTLS in a worker thread — no new dependency, no §3 row
needed. `send()` never raises; a failed courtesy email must not fail the request.

**The trail without a new event system.** There is no non-task audit store and this ADR does
not invent one: the `password_resets` rows are the durable record (`created_at` /
`consumed_at` / `superseded_at`, swept hourly by `sweep_password_resets` after a 30-day
grace, the session-sweeper shape), and Logfire lines carry reset ids and counts — never codes,
never full addresses (recipients are masked).

**Surfaces:** CLI `computer forgot-password` / `computer reset-password` (prompted, hidden +
confirmed input, `--json`, clears the stored token after a reset); web `/forgot-password` and
`/reset-password` off the login card (`AuthCard` idiom, zod + 422 field mapping, `?code=`
prefill supported).

## Consequences

Six new integration pins in `test_auth.py` prove: the console carries the code, reset rotates
the hash and kills sessions, single-use, supersede-on-re-request, expiry + sweep, quiet
throttle, and nothing plaintext at rest. The web contract regenerated. The reset TTL and
throttle are conf (`COMPUTER_AUTH_PASSWORD_RESET_*`). Anything shared MUST set
`COMPUTER_EMAIL_BACKEND=smtp` — the console backend on a shared host would print users'
reset codes into whoever reads the server logs.

## Post-review amendments (2026-08-17)

A senior review of the working tree hardened the seams; each item names its failure.

- **The smtp sentence became executable.** `EmailSettings.backend` is `Literal["console","smtp"]`
  (lowercased; a typo'd backend used to boot cleanly and silently drop every reset email while
  202-ing), and a `Settings` boot validator **refuses console with `COMPUTER_ENV` staging/prod**
  — the shipped prod compose would otherwise have printed live reset codes into `docker logs`.
  En route: `COMPUTER_ENV` (the spelling compose and `just dev` use) now actually reaches
  `settings.environment` via an alias; before, it was a variable nothing read.
  `deploy/compose.prod.yaml` wires `COMPUTER_EMAIL_*` from `deploy/.env`.
- **Reset revokes API keys too** (`ApiKey.revoke_all_for`, same transaction as the session
  revocation): any session can mint a `ck_` key, so sessions-only revocation left an attacker
  who knew the old password with a working credential after the victim "took the account back".
  The courtesy email, web success card, and Rule 14 now say sessions **and** API keys.
- **Sends left the request path.** Awaited inline, the SMTP round trip made a known email's 202
  wait on the relay while an unknown email returned from one SELECT — a user-enumeration
  stopwatch (`_DUMMY_HASH`'s lesson), plus an up-to-10 s stall and a post-commit 500 path.
  Sends are fire-and-forget tasks held in `operations/auth._outbound` (drained at API shutdown
  and by the tests' `flush_outbound_email`), deliberately NOT a DBOS job — the plaintext code
  must never ride job arguments (Rule 9). Residual: the known-email path still does three more
  DB writes than the unknown path (~ms, the same residue login's dummy-hash accepts).
- **A failed send un-mints its row.** The row committed before the send and `recent_count`
  counted it, so five 202s during an SMTP outage consumed the whole 5/hour cap and locked the
  user out for up to an hour AFTER mail recovered. Delivery failure now deletes the row (its
  code was undeliverable; the warning line keeps the trail).
- **Mint is serialized** (`SELECT … FOR UPDATE` on the user row): under READ COMMITTED two
  interleaved requests each saw "nothing pending" and both minted — two simultaneously-live
  codes, breaking supersede's one-key invariant; parallel bursts could overshoot the mail cap.
- **Limiter buckets are namespaced** (`reset:{email}|{ip}` / `reset-complete|{ip}`): sharing
  login's bucket locked the user who just burned ten failed logins out of the recovery flow,
  and `forgot-password`'s throttle exit now goes quiet instead of 429ing out of the documented
  always-202 contract. The old constant `"password-reset"` key collapsed to ONE fleet-wide
  bucket behind any proxy — `COMPUTER_API_FORWARDED_ALLOW_IPS` (new) makes uvicorn honor
  X-Forwarded-For so per-IP means the client, not the proxy. The `_attempts` map also evicts
  stale buckets past a floor: forgot-password made it unauthenticated-attacker-growable.
- **`mailer.send` "never raises" is now true of the whole body** (`except Exception`, message
  construction inside the guard, console print guarded): a non-UTF-8 stdout or a bad header
  raised AFTER `reset_password`'s commit — password rotated, sessions revoked, endpoint 500.
- **Telemetry**: the worker's Logfire scrub pattern joined the API's `c[sknr]_` class (it still
  said `c[skn]_`, so `cr_` was scrubbed in one process and not the other), and
  `instrument_fastapi` gets a `request_attributes_mapper` that blanks the four credential
  routes' parsed arguments — `email` matches no default scrub key, so forgot-password spans
  shipped the plaintext address of exactly the accounts mid-recovery.
- **Live streams are severed too** (closed same day; it shipped first as a "known residual").
  A stream authenticates once at connect and used to outlive its credential — revocation flips
  rows, not sockets, so an attacker's open EventSource kept receiving the victim's task events
  after the reset. Now the ONE generator both transports frame (`stream._events`) re-proves the
  connect-time credential AND the task authorization every `COMPUTER_API_STREAM_REAUTH_SECONDS`
  (default 10 s, the no-dead-air bound) and ends on the first failed proof; the client's
  reconnect meets `require_actor` holding a dead credential and gets its 401. Chosen over a
  revocation-notify connection registry deliberately: the periodic re-proof also catches what a
  registry never would — session expiry crossing mid-stream, a space membership removed, the
  task deleted — is deadline-based (a busy stream re-proves on the same cadence as a quiet
  one, and the pending queue read is never cancelled mid-race, so no event can be lost to the
  check), works unchanged across API replicas, and fails CLOSED (severing is cheap; the
  transport's reconnect contract replays from the cursor). Cost: two indexed reads per stream
  per interval. The interval is the honest bound on how long a revoked credential keeps
  receiving — ≤10 s, not "until reconnect".

New pins: reset-revokes-API-keys, concurrent-requests-mint-one, failed-send-never-consumes-the-
cap, hammered-forgot-password-stays-quiet-and-never-locks-login, stream-severed-on-failed-proof,
re-proof-flips-after-revocation (integration); the boot gate + `COMPUTER_ENV` alias, backend
case/typo behavior, and mailer's never-raises edges (unit).
