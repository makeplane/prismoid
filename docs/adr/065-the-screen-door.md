# ADR-065: The screen door

*Status: accepted, 2026-10-02 · amended 2026-10-13 (the upstream credential is the driver's) and 2026-10-15 (a control stream for the lease holder)*

*Numbered 064 until 2026-10-02: Phase 1c's safe-point steering ADR keeps 064, so commits before
the renumber say ADR-064 for this one.*

## Context

A task's navigator works in a real browser on the task's Machine (ADR-063), and nobody can see
it. Computer use needs people at that browser twice: the team watches the navigator finish work
in a web app with no API, and a member takes over once to sign in where the agent has no
credentials, behind a mask. The first design of the screen was for
sessions, which no longer exist; the live unit is the task, and its browser lives on the task's
Machine. On 2026-09-29 the owner chose the streaming design (a self-delimiting frame
framing, short-lived signed tickets, dropping input on the host side) and to keep Percy's
CDP screencast and its privacy mask.

## Decision

- **The relay runs in the API process** (approach A). One upstream per Machine per process,
  opened by its first viewer and closed 5 s after its last, is fanned out without re-encoding.
  Each viewer gets a one-slot buffer, a byte budget and a rate cap, and input is dropped
  host-side unless the lease names that socket's actor and connection. Every hop speaks
  one framing, `[u32 header_len][JSON header ≤ 4096 B][payload]`, at most 4 MiB a
  message, with the payload's length `n` in every header so a byte stream delimits itself. In
  dev the upstream is a hijacked Docker exec of a bridge inside the browser image; on sandbar it
  is `GET /v1/sandboxes/{id}/display`, a plain HTTP upgrade.
- **Two tickets, one key, two domains.** `POST /v1/tasks/{id}/screen/ticket` mints an `st_`
  screen ticket for the browser's socket (`GET /v1/tasks/{id}/screen?ticket=st_…`); the API mints
  a `dt_` display ticket for sandbar's display route. Both are
  `<prefix><base64url(claims)>.<base64url(Ed25519 signature)>`, signed with the API's screen key
  (`COMPUTER_API_SCREEN_SIGNING_KEY`, `<kid>:<base64 seed>`) over `percy-screen-v1\n` or
  `sandbar-display-v1\n` followed by the claims bytes, so neither kind can be replayed as the
  other. A ticket lives at most 30 s and is checked with ±5 s of skew. It is single-use: its
  `jti` is spent when it is redeemed. A ticket issued before the verifier started (plus the
  skew) is refused, so a restart cannot reopen a replay window. The screen ticket names the
  credential row its minting request authenticated with (`cred`, `cred_kind`), never the token,
  and the socket re-proves that row by id every `stream_reauth_seconds`.
- **An admission ticket is not a credential.** The opaque-hashed-tokens rule still governs every
  credential (`cs_`, `ck_` and the single-purpose doors), and a ticket does not weaken it. A
  ticket is minted only by a request that already holds a live credential. It is verified by its
  signature, never looked up. It admits one connection to one task's screen, or one sandbox's
  display, and nothing else. It lives 30 s, it is spent at its first use, and it is never stored:
  not hashed, not in plaintext, nowhere. Its only state is a `jti` cache in memory. The
  long-lived credential never appears in a URL, because every connect and every reconnect mints
  a new ticket.
- **The display ticket replaces the first design's display token.** sandbar holds only public
  keys, by `kid` (`SANDBAR_DISPLAY_KEYS`), so a sandbar compromise cannot mint a ticket. The
  display route admits only a ticket, never the master token, and every other route refuses a
  ticket. The API never holds the master token.
- **The control lease is six columns on `machines`** (`control_actor_id`, `control_conn`,
  `control_private`, `control_since`, `control_until`, `control_cap_at`): no new table, on a
  tenant root. Every lease write is one short compare-and-set transaction on the Machine row
  that never takes the task row; events, `screen.state` and gate resolution follow after the
  commit. Times are Postgres `now()`; expiry is computed on read and enacted by a
  `machine_sweep` arm. A non-private lease ends after 60 s with no input. A private one ignores
  silence, ends 30 s after its holder's socket closes, and ends at a hard 10-minute cap that the
  holder can renew. A leased Machine is never slept or evicted, and Sleep now and Reset answer
  409 while someone drives it.
- **The CONTROL gate.** A browser call that meets a live lease parks its step on a new
  `GateKind.CONTROL`, and `browser.handoff(reason)` parks it the same way when the agent asks
  for a person. The lease's ending accepts the gate; nobody accepts one by hand; Skip and the
  TTL (`control_wait_s`, validated under the park hold) decline it. The lease is checked before
  each browser call dispatches: Phase 1c's "before a tool call dispatches" safe point, in Plan
  3's own code. A call already in flight is never interrupted.
- **Where the page is, the server says** (amended 2026-10-04). A handoff asks a person to type a
  secret into a page, in the agent's words, which a page can influence. So the sidecar states the
  page's origin in its screen `status` (its tab's URL as Chromium reports it, re-sent on every
  navigation and tab move), the relay forwards only an exact http(s) origin, and the pane shows
  it as an address line whenever it is open. The handoff's banner and inbox knock name the host
  in the sidecar's words beside the agent's quoted `reason`.
- **The upstream credential is the driver's business** (amended 2026-10-13, A5 of the driver
  design). The screen's doors are a table by driver in `ai/frames/screen.py` (`DOORS`, one row per
  driver: Docker's hijacked exec, sandbar's display route), and each door obtains its own upstream
  credential. The browser's `st_` ticket is unchanged. sandbar's door still mints a `dt_` per open;
  the signer stays in `operations/tickets.py`, because it holds the API's key and `ai/` imports no
  operations (Rule 6), and it is handed to the doors as a minter. A door whose provider admits by
  its own ticket asks its provider for one per open.
  **Amended 2026-10-15 (a control stream):** a door may serve the lease holder on a control stream
  of its own (`ScreenDoor.kinds`). The holder's input is RFB (VNC) on that stream, in `vnc`
  messages, in order and never dropped; the relay opens it only for the live lease's own pane socket
  once it may drive (a private lease's settle past) and closes it, sending `vnc{closed}`, when the
  lease stops naming that socket; a control dropped under the same lease is reopened with backoff.
  On such a door the watch carries no input, so nobody else has an input path. A door may also end
  a person's hold at its provider (`hand_back`): every lease ending hands the screen back
  (`after_end`, a warm Machine only, never over a newer lease holding the row). The pane draws the
  desktop with `@novnc/novnc` (HANDOFF §3), loaded on demand, on a raw channel the one screen
  socket feeds, so no driver ticket reaches the browser, and the relay sends the upstream's cursor
  to panes only.

Rejected: **B**, the relay in the worker, which would put an internet-facing socket in the
process that holds the sandbar master token and every provider key. **C**, viewers connecting
straight to sandbar: it makes sandbar internet-facing now.

## Consequences

- The `jti` cache is per API process, so with several replicas one screen ticket could be
  redeemed once on each replica within its 30 s. It stays bound to one live credential row and
  one connection, which is accepted at design-partner scale. Valkey (sticky routing and a shared
  cache, the trigger ADR-061 already names) closes it.
- The private mask is exact in one API process, which is what production runs (one `api`
  service, `uvicorn` without workers). Across replicas, the holder's relay masks at the claim and
  holds the holder's input for the 300 ms settle, and every other replica masks when its hub
  delivers `screen.state`; that NOTIFY is best-effort and the hub does not reconnect, so with a
  lost NOTIFY or a dead LISTEN the bound is each viewer's 10 s re-proof, not 300 ms. Accepted as
  designed while there is one replica (2026-10-06); before a second, every relay watching a
  leased Machine re-reads the lease on a short timer, or the holder's relay waits for every
  replica's acknowledgement, and the hub gains a reconnect that forces a re-read after a gap.
- In dev the API process needs the Docker socket to open the hijacked exec, as the worker has
  it today. Production holds only the signing key and reaches the display through sandbar.
- Staging and production refuse to boot without `COMPUTER_API_SCREEN_SIGNING_KEY` while
  computer use is on, the mailer's precedent (ADR-031). Dev mints a key per process, which is
  sound because dev runs one API process. A malformed key fails the boot in every environment,
  and the message never echoes it.
- A ticket rides the socket's URL, so it can land in an access log. It is spent at connect, so
  a logged ticket admits nobody.
- Every viewer's bytes pass through an API process. The budgets bound each viewer; Valkey's
  sticky routing removes duplicate upstreams per replica; approach C is the long-run shape.
- No new dependency. Ed25519 comes from `cryptography` (already used by `ai/verification/signing.py`)
  and from Go's standard library; sandbar's route is a plain HTTP upgrade, not a WebSocket; the
  CLI's `--shot` uses a minimal standard-library WebSocket client (`runner/ws.py`), because
  `websockets` is only a transitive dependency. Accepted as designed (2026-10-06): the client is
  one-shot and talks only to the user's own API (connect, a hello and a hint, the first frame,
  close), every path is covered by its own suite, and promoting `websockets` would be a new direct
  dependency (an ADR and a HANDOFF §3 row) to replace ~300 lines that have no open defect.
- The privacy mask protects the moment of typing, frame by frame, not the logins afterwards. The
  claim menu says so: everyone in the Space can use the browser's logins.
- A new driver adds its door to `ai/frames/screen.py` (`DOORS`), and reconciling a headful display
  with the CDP screencast is that driver's own record.
