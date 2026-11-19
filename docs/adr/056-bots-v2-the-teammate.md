# ADR-056: Bots v2, the teammate

**Status:** accepted (2026-08-31)
**Amended by:** ADR-057 (2026-09-02) - the DM lane of §2 (a DM as one task, every message a
turn) and the `channel_messages` ledger of §5 are REMOVED and REPLACED by Conversations: a
channel is now a transport for a conversation, and talk never runs as work.
**Amended 2026-11-14:** the archivist and the curator are removed and replaced by one agent, `memory_extract`: it reads only what the person typed (never an agent's words), quotes them, and code keeps an item only when its quote is verbatim in their words; a memory supersedes another only when the extraction names the line it corrects (no word-overlap match). The chat lane runs it on every owner line to a personal bot, with no keyword gate.
**Research:** an unpublished landscape note of 2026-08-31 (a Grok bot as a teammate)
**Amends:** ADR-045 (extends; constitution unchanged) · closes deferrals named by ADR-045 §8
(lessons-from-corrections as standing state), ADR-048 §9 (the /bots routines join), ADR-054
(bots mounting `manifest.connectors`).

## Context

ADR-045 made a Bot a row that compiles to an `AgentDef` per task. That answered safety and
routing; it did not answer the product. A bot today is a description with a chat lane: no
memory, no visible data, no way to travel between people, no reachability from outside, and a
creation door that 422s until the author types routing grammar by hand ("load when", "not
for"). Meanwhile the market settled the shape: xAI's Grok Bot (2026-08) proved that a composed
teammate with durable state, one page, and a one-click shareable template is what users want,
and its documented lapses (opaque memory, one shared computer, secret-leaking templates,
ungated installs) are precisely the failures our constitution already prevents.

The thesis extends ADR-045's: everything a user composes becomes data; everything that must be
safe stays code; **and everything the machine knows or touches on a user's behalf must be
visible on one page.**

Six pillars. Nothing in ADR-045 §1..§7 changes: bots compile per task, never enter `REGISTRY`,
built-ins win name collisions, light agents can never be served by a bot, publishing needs
green selection evals as a 409, recursion stays declined (ADR-050).

## §1 Memory: a fourth scope on the ledger, never a new system

`MemoryScope.BOT` joins user, space, and skill on the existing `memories` ledger. The key is
the bot's NAME (the skill-scope precedent: registry rows are versioned, the name is the stable
identity) plus the existing carrier pair: a personal bot's rows ride `user_id` = owner, a
space bot's ride `space_id`, mirroring `bots_registry`'s two partial unique indexes. One new
nullable `bot_name` column, a fourth arm in the `scope_target` CHECK, two partial lane
indexes. Erasure and space deletion cascade with the carrier for free. `ACTIVE_CAP` and
`TEXT_MAX` unchanged.

**Writers.** People always: personal bots owner-only (strangers 404), space bots on the space
matrix (viewer reads, editor writes). Automation only for personal bots: `remember_for_bot`
runs beside the archivist after a delivered turn when `task.direct_bot` is set and the bot is
personal, reusing the archivist seat and `MemoryProposals`; the scope and carrier are decided
by code, never by the model; idempotent per turn via a durable `bot_memory.written` event. A
space bot gets no automation writes: "automation is private; sharing is deliberate" means
every shared row was typed by a person. Declined for v1: a mid-step `memory.save` tool (a
model write verb inverts propose-then-consolidate), and a proposed-then-accepted quarantine
for space bots (a status, an inbox verb, and a flow that waits for demand).

**Reader.** A sixth context lane, `bot`, budget 300 tokens, assembled only when
`task.direct_bot` is set, headed "What {name} remembers". It rides the frozen context pack
into the planner brief and `/task/context.md` exactly as the user lane does. It is NEVER baked
into `compile_bot`: memories churn per turn and the compiled prompt is the stable prefix
ADR-012's caching rides. Per-step mounting for routed multi-bot tasks is the named v1.1.

**Memory vs instructions, the line.** Instructions are craft: versioned, evaled, published,
shared by every user of the bot. Memory is standing state: facts, references, owner
preferences; fast, reversible, visible, and never part of the routing surface or the compiled
prompt, which is why it needs no gate. `bots teach` v2 has `bot_lesson` sort each lesson into
craft (a full-instruction rewrite, a new draft, unchanged) or state (`BotLesson.memories`, at
most 3, consolidated into the bot lane immediately; for a space bot they are returned for the
owner to add by hand). This is ADR-045 §8's named sequel, built.

**The DM thread stays singular.** ADR-018's turn boundaries already bound the thread; what
rots in an unbounded thread is state, and state now lives in the ledger, re-mounted every turn
regardless of elision. Named threads are the recorded v2 valve (the `dm:` client-token grammar
extends without migration).

## §2 The workplace: the bot page is the teammate's office

Tabs: chat · work (runs + artifacts, with a stats strip) · memory · routines · channels ·
access · configure.

The routines tab is ADR-048 §9's promised join, built: `Routine.for_keeper` filters
`template->>'bot'`. The stats strip is a join, never a table (ADR-045 §8's law kept):
`PlanStep.spend_of_agent` sums the ledger by step; version-blind by name, and it says so.

The weave becomes visible on the wire: `TaskSummaryOut.direct_bot`,
`TaskDetailOut.bot_pins` and `origin_routine_id`, `PlanStepView.bot_version`. The task page
wears a quiet "Direct · {name}" chip and the progress tree names the pinned version. There is
NO bot dial on the follow-up composer: `direct_bot` is thread DNA exactly as mode is.

Profile: `bots_registry.title`, a per-name mutable column beside icon and pinned (a face,
never the evaled routing surface). Greeting declined: chat copy, not identity.

## §3 Connectors: mounted at last, and legible on one page

`manifest.connectors` stops being decorative. At execution, per step, the step's bots'
declared slugs resolve against the TASK CREATOR's live connector rows (credentials never
travel with a bot; each runner mounts their own), dedupe against task-level attaches, and
extend `deps.connectors` for that step only, so the populated-only-by-`_attempt` fence holds.
An absent, disabled, or broken connector is a journaled drop that names the fix, never a task
failure. ADR-054's per-tool policies and approve gates ride untouched: they key on the
connector row, not on how it attached.

Connector entries may carry an author-written reason: `connectors:` accepts plain slugs or
`{slug, why}` through the one format door.

**The access tab** answers, for the viewing user, what this bot can touch and at what level:
tools with their one-line meanings, skill pins, `can_ask_user`, and per declared connector the
catalog identity, the author's why, MY status (connected · needs connect · broken · custom),
and MY effective per-tool policy rollup from ADR-054's `effective_policies`. Statuses are per
viewer because credentials are per owner: a space bot shows each member THEIR readiness.
Policy editing stays on /connectors (the one-home law); the access tab shows and deep-links.
One endpoint: `GET /v1/bots/{name}/access`.

## §4 Templates: a bot travels as a link, and the gate travels with it

**The unit** is `bot_templates`: immutable published snapshots (the rendered BOT.md and
cases.yaml bytes, a parsed manifest for reads, the face, a byline snapshot, visibility, an
install counter), append-only, one row per template version. No space FK and a SET NULL
author FK: a template survives the space and the author. A snapshot beats a visibility column
on the live row because the shared thing must not track the author's later edits silently
(Grok's G9), and only a PUBLISHED head can become a template, so every shared bot earned
green selection evals once before it could travel.

**The link**: `share_slug` = `bt_` + 128 bits, stored plaintext (it gates only content the
author pressed Share on, and must stay re-displayable; unlike `cr_`/`cn_`/`rt_` it
authenticates no mutation). `GET /v1/templates/bots/{slug}` is auth exception #5: read-only,
single-purpose, consent is the publish act, capability in the URL, per-IP throttled,
404-on-miss, noindex. The preview shows everything that installs, including full instructions
and cases (transparency is the anti-smuggling courtesy) and the honest strip: "Community
template. Plane has not reviewed it. It installs as a draft: nothing runs until you test and
publish it. Templates can never carry logins: you connect your own services." It never shows
`eval_report` (it embeds the author's roster), author email, or space names.

**Publish-time checks**, pure functions: a credential-pattern scan that refuses house
prefixes, Fernet, and the common vendor key shapes (a net, not a proof, and the ADR says so);
external URLs surfaced as warn-and-acknowledge. Custom connector server URLs never travel:
slugs only, stricter than plugin export, because a link is a broadcast.

**Install** is one click: the existing push door, a DRAFT in the installer's personal scope,
`origin=imported`, `source="template:{slug}@vN"` (provenance, never a privilege; no FK).
Face and cases copy; skill names resolve or become receipt notes; connector slugs resolve to
the installer's own rows with needs-connect chips. The installer re-earns green: a worker job
(`boteval-{row_id}`) runs the selection eval off `POST /v1/bots/{name}/test`, which also ends
the web-cannot-test gap for every bot author (Rule 5 holds: model calls in the worker, never
the API).

**Updates**: publishing again appends a new template version under the same slug; installers
see a quiet "v{n} available" chip (receipt version against head, computed at read time);
reinstall is a new draft to diff, test, publish. No inbox knock in v1.

**Redistribution allowed, lineage visible**: an installed bot is the installer's row; if they
share it onward the preview renders "adapted from" when `source` resolves. Prohibition is
unenforceable; lineage is. Moderation is exactly one audited admin verb, `template.unlist`.

Deferred with shapes pinned: starter memories (at most 10 plaintext strings, opt-in, shown in
full on the preview, minted as visible bot-lane rows marked with template provenance; ships
with §1's lane) · routine blueprints (RoutineTemplate-minus-bot dicts rendered as When
sentences, minted born PAUSED, installer arms each: the routine.create posture) · a listed
gallery (the `visibility` column ships; the page waits) · selling (ADR-014 stands).

## §5 Reach: doors, channels, and presence

A channel is a row: an external address bound to one bot, carrying its own hashed token or
provider secret, its own caps and allowlist, minting ordinary Tasks whose creator is the bot's
owner, whose inbound text is DATA behind a fence, and whose replies flow back through one
message ledger and one outbound job. It is the routine fire door (ADR-048 §5) generalized
from "start my routine" to "talk to my bot".

**The door.** `bot_channels` (kind: webhook · email · telegram · whatsapp) with
`channel_messages` beside it (dense per-channel seq, `provider_message_id` dedupe, both
directions). The webhook kind is the barest channel: a `bc_` token with the exact `rt_`
grammar; `POST /v1/channels/{id}/messages` starts or continues the channel thread
(`client_token "ch:{channel_id}[:{thread_key}]"`, never the owner's private DM thread);
`GET ...?since=` polls; an optional egress-vetted, HMAC-signed `callback_url` pushes. The
ledger absorbs the live-turn 409: inbound lands queued; the settle hook and a minute drain
mint the next turn. Auth exceptions #6 (bearer `bc_` on the messages pair) and #7 (provider
inbound webhooks authenticated by header secrets), burst-capped AFTER the token check (the
recorded fire-door lesson).

**The fence.** `CHANNEL_LAW` wraps inbound text as data (`PAYLOAD_LAW`'s sibling); the body
mounts at `/task/inputs/message.json`; `input_mode` defaults to data for every kind including
the API, because a leaked token must start turns, never steer them; instruction is an explicit
owner opt-in with the risk named in the UI. Per-turn budget (default 1 USD, rides the Bursar),
daily message cap (default 100), per-channel disable, and `COMPUTER_CHANNELS_ENABLED=false`
stops everything this tick. Channel-origin turns never auto-write bot memory (a poisoning
vector; an owner dial). Approve, budget, and connect gates resolve ONLY in-product at the
owner's inbox: an external party can never approve spend or credentials.

**Email.** Every bot can mint `{name}-{token6}@{email_domain}` (a random suffix, because a
guessable address is a spend-my-money invitation; not plus-tags, because the bot must appear
as itself and CRMs normalize tags away). Inbound is ONE generic JSON webhook any
inbound-parse provider can target; threading rides In-Reply-To; outbound rides
computer/mailer with the bot's from and reply-to (the console backend prints in dev; the
staging and prod refusal already holds). The sender allowlist is default-closed, seeded with
the owner's login email; anyone-can-email is an explicit dial with the caps restated beside
it; an unknown sender is a refused ledger row, no bounce, no knock.

**Telegram now, WhatsApp pinned.** Telegram is bring-your-own BotFather token
(Fernet-stored, getMe-verified), webhook mode with a per-channel secret, poll mode for dev.
WhatsApp ships as the enum value with the decline named: Meta business verification, BSP
mediation, per-message billing, operator-owned numbers.

**Presence: the bot as an assignee elsewhere.** A first-party integration surface, not a
connector: connectors are a user's egress for our tasks; presence is inbound agent sessions
under an operator-configured app identity. `presence_installs` (platform, workspace, token,
webhook secret, routing rules: "issues delegated in team X go to reviewer") and
`tasks.origin_presence_install_id`. The contract: an inbound agent-session event (what,
where, who) routes to a bot and mints or continues a `ps:{install}:{session}` task; outbound,
a seq-cursor tail over task_events maps narration to thought, tool.called to action, an INPUT
gate's numbered questions to an elicitation, delivery to the response, failure to error. One
"Plane Computer" OAuth app per platform, scopes assignable and mentionable, the bot's name
and face riding the activities, platform-native agent disclosure. Linear is the first
adapter; Slack is presence adapter #2 (a shared workspace surface is exactly this contract),
NOT a channel kind. Work questions may round-trip to the delegator in-platform; the gate
split above still holds.

Declined: a public anonymous chat widget (abuse surface; owner-distributed tokens cover it) ·
bot-to-bot channels (launders the depth budget) · inbound voice · per-message billing
passthrough (UsageEvents already meter) · per-provider email signature verification in v1
(behind the generic secret, pinned).

## §6 Authoring: the grammar becomes UI, never homework

**Amended 2026-09-01, the owner's call: nothing but the NAME is mandatory at birth.** The
first cut of this section still carried the door's publish-grade requirements into creation
(three questions, an eval case). That conflated two moments the draft-test-publish ladder
exists to separate: a valid row at birth, and a publishable bot at the gate. The contract
now: the door writes an empty-but-valid draft from a name alone; a NEWBORN chats immediately
(the charter plus one honest untaught line - corrections in that conversation are teaching);
the create dialog is name + optional instructions on top, with routing, the first eval case,
and every dial under Advanced; and PUBLISH (and test) name each missing piece with its fix -
routing description, instructions, at least one case - before the eval verdict is even
consulted. The grammar rules still bind whatever IS written, and no incomplete bot can ever
reach a roster, so the relaxation costs zero safety. Completeness is the gate's requirement,
never the door's.

The door's "Load when" and "Not for" requirements are correct semantics (One routes on
triggers and boundaries; the selection eval scores exactly that surface) enforced on the
wrong party. `BotSource` grows `job`, `use_when`, and `not_for`; the door composes the
canonical description through the one renderer; a verbatim `description` stays accepted and
`_check_description` keeps enforcing the grammar on the RESULT, so routing and the eval are
untouched, and BOT.md gains the optional structured keys. The create dialog becomes three
small questions (what it does, when it steps in, what it refuses) with everything else under
an Advanced disclosure carrying today's defaults. "Draft it for me" expands the job sentence
into instructions, a sharpened description, and cases via a worker job running `bot_lesson`
from the idea, the same `BotLesson` shape, landing a reviewable draft. Empty-state starters are
client-side prefills, never seeded rows (ADR-045 §8's decline stands).

## §7 Standards: what we adopt, what we decline

Adopted: Grok's teammate composition, visible-state answer to its own G2, task-skill-routine
legibility, and link-install flow · the GPT Store's copy-on-install privacy · Agent Plugins
v1 as the template byte format (ADR-045 §7; one format, two transports) · Linear's delegation
model, opt-in scopes, session-and-activities contract, and always-disclose · the agent-email
identity pattern · Telegram as the first personal channel.

Declined, with reasons on the record: a shared always-on computer (ADR-013 frames are the
safety story) · an unbounded bot thread (ADR-018) · model-based review as the boundary
(ADR-054's structural gates) · bot-to-bot messaging (ADR-045/050) · a marketplace, selling,
central review (ADR-014) · agents as accountable assignees (the human-creator invariant) ·
WhatsApp v1 · autonomous account creation by email identity.

## §8 Money, and what is NOT built

Metering is untouched; per-bot and per-channel spend stay ledger joins
(`PlanStep.spend_of_agent`, `tasks.origin_channel_id`), never tables. Channel turns carry
their own budget; the Bursar and the space ceiling apply unchanged.

Not built, named: bot recursion · named DM threads · a space-bot memory quarantine flow ·
per-step bot-memory mounting on routed tasks · starter memories and routine blueprints in
templates (shapes pinned in §4) · a listed gallery · WhatsApp · Slack presence · provider
email signatures · an inbox knock for template updates · pgvector memory retrieval (ADR-011's
plan unchanged).

## §9 Build order, and where the first campaign stopped

W1 authoring relief · W2 memory spine · W3 the bot lane · W4 auto-memory · W5 teach v2 ·
W6 the weave read path · W7 connectors mounted + the access tab · W8 profile and parity ·
W9 templates spine · W10 templates web · W11 channel spine (webhook) · W12 email ·
W13 telegram · W14 presence contract + Linear · W15 sweep. Migrations landed in order:
memories_bot_scope (d5bcbbe27e23) · bots_title (ee385cf975bc) · bot_templates (5a8e7aa8f3b2) ·
bot_channels (aa9e93465547). Zero new dependencies.

The first campaign (2026-08-31 to 09-01) landed W1 through W13, each proven live. **W14
presence is designed above and deliberately NOT built**: a Linear adapter without a Linear
workspace, OAuth app, and webhook target to prove it against would ship dead code, and this
repo does not land what it cannot run (the prove-it-live law). The contract in §5 - the
session event, the routing rows, the activity mapper, the gate split - is the next session's
starting point, with the callback_url arm of the webhook kind and the per-provider email
signatures deferred beside it. Deferred inside landed pillars, for the record: the web
follow-up bot dial (declined, thread DNA) · named DM threads · space-bot auto-memory
quarantine · per-step bot-memory on routed tasks · starter memories and routine blueprints in
templates · the listed gallery · WhatsApp.
