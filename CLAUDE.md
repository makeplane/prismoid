# Prismoid — standing rules
Read HANDOFF.md §1–2 and STATE.md before any change; those outrank convenience and elegance.
- Folder rules: each important folder has its own CLAUDE.md (AGENTS.md links to it) - read it before changing that folder; docs/engineering.md is the craft standard; `just quality` and `/review` (ADR-075) hold both.
- Vocabulary: Task (what the user asked) · Step (plan node) · Job (machine process, jobs/) ·
  effect (@durable.effect - the engine only via jobs/durable.py, ADR-069) · Workspace (the
  tenant) · Project (single nullable FK; ADR-067 lists the retired names) · Capture (soft intake) · Inbox
  (notifications) · publish (the only way artifacts exist) · Bot (a user-authored agent, as
  data — ADR-045: compiles to an AgentDef per task or per conversation, never enters REGISTRY; built-ins win name
  collisions; publish needs green selection evals, a 409) · Routine (standing work, ADR-048:
  a saved composer payload + a When; a fire's single occurrence is a run).
- Routines (ADR-048): a Routine EXECUTES NOTHING — every fire mints an ordinary Task via
  create_task (clarify=never, budget required) and every fire AND non-fire journals a
  routine_runs row wearing its reason (silent suppression is the named industry defect).
  Durability = three idempotent layers: unique (routine_id, occurrence) re-checked under the
  row lock + create_task client_token (a resumed/racing fire recovers its own mint) + the
  tick-scoped workflow id `routine-{id}-{occurrence}~{tick}` (dedupe within a tick; a
  terminally-errored fire re-offers next tick instead of wedging — amended 2026-08-27); the
  sweep is read-only and dumb, two arms (due + parked drain — no workflow ever sleeps on a
  queue slot); Postgres now() is the time authority (the gate reads _db_now); next_fire_at
  moves only by compare-and-advance in the fire gate, and token/API fires never move it.
  Fire math is cronsim (Debian-exact DST) in schemas/routines.py ONLY — never croniter.
  PRISMOID_ROUTINES_ENABLED=false stops dispatch immediately. Owner is human (RESTRICT);
  erasure/suspension pauses. tasks.origin_routine_id/origin_depth carry provenance — spend
  per routine is a ledger join, never a table. A fired turn is unattended (ADR-092): its Lead never
  asks or writes free text; it delivers to its owner (`deliver_to_owner`, `findings` feeds
  notify=findings beside a deliverable) or fails typed (the task fails; the escalation reaches the owner).
- Three data homes: rows db/models · values schemas · request/response api/serializers
  (*In = request, *Out = response; one file per resource).
- Endpoints: one file per resource; named functions (create_task, list_tasks, resolve_gate);
  signatures use ActorDep/SessionDep/PageDep aliases — never raw Depends in a view;
  ≤20 lines; auth → validate → one call → serialize; no tx in api/.
- One place per kind; three screens honest: api/urls.py, ai/agents/all.py, jobs/all.py.
- Views never touch agents; agents never contain business logic; operations compose both.
- Operations own transactions; enqueue after commit with deterministic ids ("task-{id}").
- ORM rows never reach agents, prompts, or responses. prismoid/ai is persistence-free;
  imports neither db, operations, jobs, nor api.
- Every provider call emits a UsageEvent (native units + cost_usd). Adding a model is data
  (catalog + prices), never a deploy; model strings only under ai/tools/providers/ (the audit
  skips every directory named providers; today only the search wires in search.py hold any).
- Artifacts only via frame.publish → manifest → harvest. Every tool call passes the tool_record
  capability; whether a call needs a person's yes is decided only by the approval rules
  (ai/rules/approvals.py), and an ask raises ApprovalRequired before the call acts (an approve
  gate); an ungranted tool is not mounted.
- The answer is primary; verification is its receipt (ADR-019): standard lane goes
  publish → step done → deliver, and the `verify_artifact` job signs the verdict after, on
  its own queue — never blocking, never failing the thread. Fleet keeps in-line verify
  (ADR-010), and the long-form lane's QA gate reviews before delivery (ADR-026). The judge
  sits as far from the maker as the host allows (another family, then another model of the
  same family, then the maker's own), and the receipt names which. Collections normalize at harvest (schemas/collection_repair, repairs on
  provenance); publish hand-off is static-runner + JSON args, never shell interpolation.
- Identity: Actor = thin attribution principal; User = human satellite (email, profile,
  prefs, GDPR). Sessions + credentials belong to Users; api_keys to Actors. External API
  says user/agent/API key — "actor" only inside event/audit payloads.
- Auth: argon2id; opaque hashed tokens (cs_/ck_); no JWT; no plaintext tokens at rest or
  in logs; every route requires an Actor except healthz/login/signup, the public config read
  (`GET /v1/auth/config`, ADR-086: the deployment's signup mode and legal/support links, nothing about any account), the password-reset
  pair (its emailed single-use hashed cr_ code is the auth — TTL'd, superseded by
  re-request; completing revokes every live session AND API key; sends ride background
  tasks off the request path, a failed send un-mints its row; the same shape proves an address: an emailed single-use hashed `ev_` code
  (Actor-authenticated; it rides the link's URL fragment, never a query) proves it, and only a proven address sends invites; outbound mail only via
  prismoid/mailer, whose console backend in dev prints the email to the API stdout and is
  REFUSED at boot in staging/prod — ADR-031), the connectors OAuth callback (its
  single-use hashed cn_ state token is the auth, and only in the browser that started the flow:
  its HttpOnly cb_ cookie must match the hash on the flow — ADR-020), and the routine fire door
  (POST /v1/routines/{id}/fire may authenticate with the routine's own long-lived hashed
  rt_ token instead of an Actor credential — ADR-048 §5: it starts runs and NOTHING else,
  owner-minted/rotated/revoked, burst-capped at the door; its payload is DATA behind
  PAYLOAD_LAW, never instruction), and ADR-056's three machine doors: the bot template
  preview (GET /v1/templates/bots/{slug} serves a published head only), the channel
  messages pair (its hashed bc_ channel token is the auth; it accepts a line and reads a
  feed, nothing else) and the email inbound webhook (the operator's shared header secret,
  then the address resolves the channel; every inbound line still walks the allowlist,
  the caps and the fence in accept_inbound), and the screen door (ADR-065): `POST
  /v1/tasks/{id}/screen/ticket` (an Actor) mints an Ed25519-signed, single-use, 30 s `st_`
  ticket bound to the credential row (never the token) and the connection, and the screen
  WebSocket accepts only that ticket, re-proving the credential row, the task and the editor
  role every stream_reauth_seconds; the long-lived token never rides a URL, and `ticket=`/
  `token=` query values are redacted from access logs and spans. sandbar's display route
  (`GET /v1/sandboxes/{id}/display`, ADR-065) accepts only a single-use 30 s `dt_` ticket
  the API signs (sandbar holds only the public keys, SANDBAR_DISPLAY_KEYS), never the master
  token, and no other sandbar route takes one; the API dials it (PRISMOID_API_SANDBAR_URL) by
  the Machine row's `driver`, never this process's frame driver, and a sandbar Machine has a
  screen only with PRISMOID_COMPUTER_USE_SANDBAR_SCREEN on (the API and the worker alike). A driver whose screen door serves a control stream (`ai/frames/screen.DOORS`) gives it to the lease holder's own pane socket only.
  And the blob door (ADR-077, local mode only): `GET /v1/blobs/{digest}` serves a stored file to the browser in place of an
  S3 presigned URL; its HMAC-SHA256 signature over (digest, bucket, name, inline, media, exp) is the auth - short-lived,
  keyed by `home/blob.key` (0600) - it 404s unless `PRISMOID_ENV=local` with `PRISMOID_WORKER_STORAGE_BACKEND=fs` (staging/prod refuse fs at boot), answers every bad, expired or absent
  signature with one 403, and its `sig=` query value is redacted from access logs and spans like `ticket=`.
  And the SPA catch-all (ADR-077, local mode only): `prismoid up`'s API serves the built web app at any GET that is not an API
  route, mounted only when `PRISMOID_LOCAL_WEB_DIR` is set; its files and `index.html`, nothing else, and an unknown `/v1/...` stays the API's 404.
  And the stream door (ADR-082): a browser opens `GET /v1/tasks/{id}/events`, `GET /v1/conversations/{id}/events` and the task
  WebSocket with a single-use, 30 s `et_` ticket in place of the Authorization header. `POST …/events/ticket` (an Actor who can
  read the subject) mints it, hashed at rest and bound to the credential row (never the token), the actor and that one subject.
  An atomic DELETE…RETURNING spends it on any replica, even when it is refused. The stream re-proves that credential row and the
  subject every stream_reauth_seconds, exactly like a header-authenticated one. No route reads a credential from `?token=`, and
  `ticket=`/`token=` values stay redacted from access logs and spans.
- The web door (ADR-046): `web.fetch` is the ONLY way an agent reads a URL — a worker-process
  tool (the connectors/media posture): parse-time vet in schemas/egress (shared with
  ADR-020's door) + resolve-and-pin + every redirect hop re-vetted; trafilatura reads pages,
  markitdown converts binaries ONLY in the rlimited sidecar (ai/tools/convert.py) and NEVER
  fetches; context-anchored (only URLs the task's context mentions); refusals are journaled
  results; summaries carry host+path, never query strings. Granted by
  ai/agents/grants.py + BOT_TOOLS. The search lane (ADR-047): `web.search` — engines as CONFIG all the way down:
  a self-describing WireSpec registry (ai/tools/providers/search.py, the ONE wire file) +
  five flat name=value grammars (ENGINES/KEYS/URLS/PRICES_PER_1K/MODELS) make enabling any
  registered engine pure env; build() knows no engine's name; a new wire shape = one function
  + one registry entry in that file. Eight wires: brave·tavily·exa·perplexity·parallel·
  searxng + provider-native anthropic/openai (ride worker keys when unset; LLM-mediated;
  legitimate as ENGINES behind the seam — §9 bans them only as the agent's hands). Each query
  a UsageEvent (Unit.SEARCH, family "web", price_version=0 = conf-priced); the QUERY never
  reaches a journal/label; failure falls to the next engine, exhausted chain returns an
  instructive string; empty chain ⇒ the tool does not exist. Engine URLs are operator infra
  (may be private — the egress guard governs model-authored URLs only). The browse lane is
  the operator's browser tool (ai/tools/browser.py, ADR-066).
- Connectors (ADR-020, amended ADR-054): a user's connection to a remote MCP server. Rows +
  tokens are Fernet ciphertext via operations/cipher (key_id for rotation); MCP client speak
  lives ONLY in ai/tools/connectors.py (operations imports only the MCP SDK's OAuth types,
  for the connect flow); specs cross into the engine on TaskDeps.connectors
  (one SecretStr); every call journals tool.called through tool_record (arg keys, never
  values); attach is per task (connector_ids mirrors skill_pins) - explicit, or INTENT
  ATTACH at create (ADR-054: the owner's LIVE auto_attach rows whose vocabulary the goal
  speaks; deterministic word-boundary matcher in schemas/connector_catalog.suggest, the ONE
  grammar the composer chip shares; never forks/routines; provenance on
  TaskCreated.connectors_auto); forks filter to still-live; worker-process MCP calls bypass
  frame egress BY DESIGN — the SSRF door + the grant are the policy. The catalog is DATA
  (schemas/connector_catalog.py, URLs allowed there by the audit's named exclusion; every
  seed probed live by `just connectors-catalog-probe` before landing).
  PER-TOOL POLICY (ADR-054): tool_policies JSONB overrides + the pure dial (tools on a connector
  you connected yourself run; on an intent-attached or bot-manifest connector a tool not labelled
  read-only asks; annotations captured at probe); `off` never enters the spec; the dial is one
  key of the approval rules (data in schemas/approvals.py, deny > ask > allow), which also ask,
  on an attended task, before a tool labelled destructive and before a write after outside text;
  an ask raises ApprovalRequired from the hook BEFORE the call and parks the step on a
  GateKind.APPROVE gate riding the ask_user spine (one gate per batch; deny-on-timeout is
  structural via the sweep's no-questions arm; kind-aware gate budgets; ask-tools ABSENT on
  can_ask_user=False agents); the gate's reason carries a CAPPED args preview for the owner
  deciding - the journal stays keys-only. Approval knocks the inbox (GATE_OPENED);
  needs_reauth transition edges knock CONNECTOR_BROKEN.
- Projects: simple folders. One owner (column) + members (editor|viewer).
  Project.get_for(min_role) is the only fetch path; viewer reads, editor creates/runs/resolves,
  owner settings/budget/members/transfer. Artifacts via Tasks (derived) — never a direct parent.
  The Project is the collaboration unit (ADR-062): members see every task; owner-minted `si_`
  join links (workspace members only; hashed, TTL'd, single-use - the join that grants membership
  spends it; accept requires an Actor); a Project
  task reads/writes no user memory, and private event fields are the creator's to read
  (schemas/events.redact_for, where events leave the server); the task row lock guards
  context_pack writes (a move and record fence the pack under it); a gate resolve takes the
  task row lock, so the first answer wins and the second is handed it.
- The Machine (ADR-063): a task's remembered computer - one `machines` row per task (tenant root),
  created lazily by the first tool call that needs the computer; serial steps and turns share it;
  seats (fleet seats, lane sections, scatter seats, concurrent siblings), verify, QA and figures keep
  throwaway frames; a serial step on the base image starts on paper, an in-memory frame with the
  same publish door and no Machine, and its first `LOCAL` call (a shell) or a park holding a draft
  takes the Machine with paper's files (`capabilities/machine_acquire.py`; ADR-063 amended
  2026-11-15); a re-entered or continued run replays its logged writes and publishes onto fresh
  paper, booting nothing; a prewarm starts it only for a committed plan that may need it (ADR-070 amended).
  Operations get a sandbox only via `machines.acquire`, `machines.paper` or `new_frame`; `ai/`
  sees frames only; only `operations/machines/` calls lifecycle verbs on a Machine's frame. A
  Machine with a control lease (ADR-065: six `control_*` columns, written only by short
  compare-and-sets on the Machine row) is never slept, evicted or reset under its driver; the
  screen relay reaches a Machine's frame only through `operations/machines/screen.py`, which
  never calls a lifecycle verb; a private lease sends nobody else a pixel. Sleep
  after 10 idle min (30 parked), `/work`-only checkpoint kept 14 days in the `snapshots` bucket,
  the `/browser` archive encrypted with `operations/cipher` and never copied into a fork;
  checkpoints are cache (a lost one is a rebuild). Egress per step under a versioned allowlist;
  warm time metered (`Unit.SECOND`, family `machine`) - `release` hands back a Segment and the
  caller bills it. Lock order is the Machine row, then the task row; a hand-back on its own
  connection (`release`, `widen`, `release_parked`, the reconciler's fixes) never takes the task
  row. The reconciler destroys only what Postgres disowns (no reap-all) and releases dead holds. `PRISMOID_MACHINES_ENABLED=false`
  rolls back to a frame per step.
- The device Machine (ADR-078): driver `device`, the person's own Mac, macOS only. The helper
  (`prismoid device helper`, prismoid/device/) enforces: Seatbelt around every agent command, its file door,
  the egress proxy, the Docker proxy; it trusts no server and imports only schemas, conf and
  egress_relay. Virtual paths are canonical: everything stored says `/work`, FramePaths renders real
  roots at the driver, and a cloud driver's prompts stay byte-identical. No blind re-run: a device
  step the worker or helper lost never starts a fresh attempt on its own, it parks ("Waiting for
  <device>") until a person resumes. Private-only: a device task never shares a Project with members
  (`device_task_private`), and only its owner drives. A task never moves between the device and a
  cloud Machine, and never boots early (ADR-070's rule reads the task's driver, not the worker's). Sleep is `none`: nothing is archived, what the task left running stops, and the frame
  goes only with Reset or its task; a grant is never deleted.
- Workspaces (ADR-061): the tenant. Every table is classified in db/models/tenancy.py
  (tenant root · ledger stamp · platform-or-workspace · tenant child · platform · identity) - an
  unclassified table fails the build. Every door binds the unit of work's workspace
  (tenant.bind / bind_platform); tenant reads ask tenant.workspace_of, which FAILS CLOSED. A
  reference never crosses workspaces (composite (project_id, workspace_id) FKs). Per-person surfaces are per
  workspace. The tenant is a Workspace, the shared folder a Project, a task's sandbox a Machine,
  the product Prismoid (ADR-067).
- Tasks move in/out/between projects (creator moves own; editor+ on destination; task.moved
  event). usage_events attribute at time of spend — a move never rewrites the ledger.
- Owner/creator FKs target actors with a human-invariant in the model; the API presents
  owners and members as users; add_member takes an email.
- Questions: ONE numbered batch per gate; three-action resolve (accept|decline|cancel,
  MCP-elicitation-aligned, flat schemas); answers schema-validated → /task/answers.json;
  TTL + on_timeout; ask_user = deferred tool, top-level step agents only.
- Steering applies at safe points (ADR-064): before each model request, before each tool
  dispatch, when a step's run is about to end, and at every frontier boundary; a tool already
  running always finishes, and cancel is the only hard stop. A steer is a durable, attributed
  command (`steers`, idempotent per task on `command_id`), accepted under the task row lock by
  compare-and-set on `tasks.plan_epoch`: a steer issued against an older epoch is a 409
  `stale_steer` naming what teammates did meanwhile, and every accepted steer and every turn start
  bumps the epoch. A call chosen before a steer is held, never run; an approval a steer overtakes
  is superseded; the unstarted steps of a standard plan re-plan at the next boundary (other lanes
  take the steer through their contracts); nothing delivers until every accepted steer of the turn
  is applied, rejected or retracted (a step that finished without one goes again in place, its
  output superseded and never taken back; a step whose attempt acted outside the task or may
  have (a call the tool catalog marks as touching the world or does not hold, such as a connector's
  tool or the browser; a call that mints rows outside the task; a shell with a route out) is never re-run,
  its steer is rejected, and neither is a step of a turn stored with an `action` or `navigate`
  route; `steering.acted_outside` and `steering.reruns`). Pause parks a
  step that can defer at its next tool dispatch on a question-less PAUSE gate that never times out
  (any other step pauses at the frontier); parks are capped deployment-wide by
  `PRISMOID_STEERING_MAX_PAUSE_PARKS`, and at the cap the pause holds at the frontier. Spend
  follows the epoch's actor. follow_up = the SAME task's next turn (ADR-018:
  settled → planning, `task.continued`, per-turn job id `task-{id}-t{n}`, full thread
  transcript + own prior artifacts mounted); fork = the only branch — a child task
  (parent_task_id) born held, carrying the whole conversation + the parent's dials.
- Streaming: two lanes, one socket — durable TaskEvents (record) + ephemeral deltas (live);
  every delta stream ends in a durable final/snapshot; streamed text is markdown,
  append-only, never HTML; binary artifacts stream progress facts, never bytes;
  no-dead-air ≤10 s; first delta <2 s. SSE is co-primary: only Lane-1 frames set id:
  (Last-Event-ID == since); stream routes never compressed or buffered. A live stream
  re-proves its credential + task authorization every stream_reauth_seconds (10 s) and
  severs on the first failed proof — revocation/expiry ends an open socket within one
  interval (ADR-031). presence.* (ADR-062) is ephemeral state on the task stream: never
  persisted, no `id:`, exempt from the durable-snapshot rule.
- task_events, plan_edits, usage_events append-only. Alembic only. Keyset pagination only.
- The edition seams (ADR-103): what a distribution adds goes through `operations/allowance.py` (a
  workspace's spend allowance: none here, every door opens), `ai/frames` `DRIVERS` /
  `EARLY_BOOT_DRIVERS` / `CLIENT_MODULES` and `ai/frames/screen.DOORS` (a driver, a screen door).
  Core code names no distribution's feature; a door of new spend calls `allowance.require`.
- Skills: draft → test → publish; publish requires green evals (409 otherwise); plans pin
  skill versions at plan write (a format maker's format skill included); a step runs a pin only
  when its agent holds every tool the skill declares (`allowed-tools`; a declaration grants
  nothing); a frameless agent reads its granted skills with `skill_read` off a menu frozen per run;
  drafts resolve only for their author.
- New dependency = ADR + a row in HANDOFF §3. Performance budgets (§15) are requirements.
- Process (ADR-008): STATE.md is the session anchor — read it first, update it when a
  workstream lands. Conventional commits, any sane size, no co-author/assistant trailer;
  trunk main; tags are releases, not stages. `just check` green before every commit;
  `just test` before a workstream
  merges; drills (smoke/perf/load/chaos/endurance/backup-restore) ride their CI cadence
  or run on demand. Red = stop and fix before new work. Nothing thrown away
  (replacement = removal + replacement + reason, same commit).
- The CLI (§5.6) stays the instrument: --json everywhere; meaningful exit codes;
  tasks events --follow is the terminal flight recorder. Prove a user-visible change
  with one real run (`just smoke` or the relevant CLI flow) — no log ceremony, no
  validation accumulation. Skills/agents → just evals.
- The Lead decides each turn (ADR-092, amending ADR-058): code reads a turn's facts once
  (`operations/turn/intake.py`: dials, origin, attachments, budget; never the words) and a
  code-made recipe answers only a fleet or a direct Bot's first turn, a long-form size dial and
  a deliverable dial its one step reaches (`route_source=dial`); every other turn, follow-ups,
  forks and routine fires included, is ONE Lead step (`route_source=lead`) that ends in a reply,
  a plan (`plan.edited{lead}` + a second `task.planned`; a scatter or long-form recipe step is
  code-expanded at commit into its map or outline), one ask, a card (`task.proposed`; Start
  = a follow-up with the deliverable dial) or, unattended, a delivery or a typed failure; no code
  guesses talk or work from text, and a Lead turn skips the intake round (the Lead asks for
  itself). Only a USER-sourced deliverable stamp binds a later turn (accidents are not thread
  DNA); the thread's own earlier deliverables are revised in place, never re-researched;
  `route_source ∈ dial | lead` for a new turn (`detector | anchor | hatch | planner` are older
  rows only: the doors, the answerer and its hatch are deleted, and a stored step naming a
  retired agent fails as "no such agent"); rollback is git. The routing dataset (`evals/datasets/outcomes.jsonl`) grows from
  every found-live misroute (ADR-052).
- One talk engine (ADR-059): a bot talks in exactly ONE place, its Conversation - the ONLY
  talk compile is `compile_bot(conversation=True)`, reached iff `TaskDeps.conversation_id` is
  set (there is no `TaskDeps.talk`); a task thread's talk turn is the Lead's reply in every
  thread, direct ones included (a direct task's first turn is the bot's work; from turn 2 the
  Lead decides, and its plan's work step is the bot's). The conversation a
  chat-born task came from is its THREAD's turn 0 (`conversation_prelude`), never an attached
  source. In a conversation a memo, a summary, a list, a table, a snippet IS the reply; the
  door reads facts, never words (a gesture, the card's Reply, the sender, the dials, length), and
  the bot proposes (`propose_task`) only what a message cannot hold (a file in a named format, a
  run, an action in a service). The reply
  STREAMS: `GET /v1/conversations/{id}/events` (SSE: `message` rows with id=seq + chat.start/
  delta/activity/end, the tap in chat mode, the hub keyed by conversation_id); the reply lane's
  claim is a LEASE (`replying_since`, `chat_lease_s` > `chat_timeout_s`), memory_extract runs after
  the claim is released (the owner's own quoted words only).
- One thread, the card, the guardrails (ADR-060): `bots_registry.threads ∈ one | many` (one: the
  bot IS its conversation, `open_conversation` is idempotent; many: a chat per topic, the list
  appears once a second exists); the Chat tab is ONE composer and ONE CARD PER TASK
  (`chat-stream.cardsOf`; a live card mounts the task's own stream; gates draw in the card; no
  Start-as-task button, no verdict/consent pills, no "talk is not work" footer - verdicts live on
  the record and on Details); a reply under a card continues the card when the bot hands it there
  (`continue_card`, offered only while a task's card is the newest thing above the line; code
  steers or follows up by the task's state; `anchored_by ∈ reply | card`; the continuation's
  answer comes home as the BOT's line, never a second Delivered); a yes or no on the bot's waiting
  card is the bot's to hear (`accept_pending` / `decline_pending`; no word list); `work_mode` defaults to `auto` (the owner's cap is the
  consent; strangers, over-cap work and service writes still ask); a reply becomes a file in one
  tap (`POST /v1/conversations/{id}/messages/{seq}/save`: the reply is the task's ONE source,
  the document dial and format set); the conversation set (`evals/datasets/conversation.jsonl`,
  `--suite conversation`) grows from every chat misroute found live.
- Library drift → docs/adr/006-api-drift.md.
