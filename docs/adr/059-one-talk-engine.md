# ADR-059: One talk engine - a bot talks in one place, and the answer streams

**Status:** accepted (2026-09-02) · built the same day (W1-W6), gate green, proven live (the
proof block at the end).
**Amended 2026-11-14:** the archivist and the curator are removed and replaced by one agent, `memory_extract`: it reads only what the person typed (never an agent's words), quotes them, and code keeps an item only when its quote is verbatim in their words; a memory supersedes another only when the extraction names the line it corrects (no word-overlap match). The chat lane runs it on every owner line to a personal bot, with no keyword gate.
**Amended 2026-11-14 (the stranger posture):** a reply to anyone but the bot's owner reads no memory at all (neither her user lane nor the bot's lane); its prompt is `third_party` in the run's taint (`capabilities/taint.speaker`); and only the bot's instructions and the owner's lines anchor its `web_fetch`. The talk compile is an overlay on the answerer's hands (ADR-045 §1): it never holds a tool or a connector reach the answerer lacks.
**Amends:** ADR-058 §4 (the direct thread's talk turn no longer runs on its bot - a task turn's
talk is the answerer's, always), ADR-057 §2 (the nudge fires only for what a message cannot
hold), ADR-057 §5 (streaming deltas, promised as v1.1, built), ADR-057 §1 (the conversation
mounted into a chat-born task is CONTEXT, never a grounding source).
**Grounding:** the owner's verdict of 2026-09-02 on the bots work ("no answer comes up straight
and/or easy"), an end-to-end audit of ADR-045/056/057/058 as built, and
the landscape note behind ADR-056 (the target: a Grok bot answers in its
thread, streamed, with its tools; work is one gesture away and never forced).

**Amended 2026-11-15 (ADR-092):** a task thread's talk turn is the Lead's reply (the answerer's role); the Conversation lane is unchanged. Takes effect at the Lead's cutover.

## Context - the audit

Four ADRs in three days built the bots product on top of each other. Each was proven live on
its own; together they left the one thing the owner asked for - *ask a bot something, get the
answer* - with two homes, two doors, and no stream. The findings, each verified in code:

1. **Bot talk exists twice.** A bot answers a message through `operations/chat._reply_once`
   (the conversation reply lane, ADR-057) AND through `operations/execution._attempt_express`
   with `TaskDeps.talk=True` (a direct task's talk turn, ADR-058 §4). Two composers, two briefs,
   two transcript renderers, two memory mounts (the task side mounts none - ADR-058 §9's own
   known limit), two proposer closures, two card homes (`messages.proposal` and the
   `task.proposed` frame), two Start paths, two results-come-home paths. `compile_bot(
   conversation=True)` was reached through two flags. On the task side the compiled prompt
   carried CHAT_LAW ("you are in a CONVERSATION, not a task - propose, never attempt") under
   an express acceptance that said "call escalate_to_plan ONCE" - two laws in one prompt.
   ADR-057 retired the DM-as-task lane; ADR-058 quietly rebuilt half of it a day later.
2. **Two doors, two vocabularies of work.** `triage.express_tier` decides talk vs work for a
   task turn; `conversation.verdict_for` decides answer/propose/start for a chat line. The
   same message routed differently on the two surfaces: "summarize this" was an inline answer
   in the Chat tab and a sandbox run publishing a document on the task page. The conversation
   door's own detectors were loose: `run` matched "in the long run", `compile` matched
   "compile a list", and a connector write needed a capitalized service name.
3. **The nudge forced a card for prose.** Any document noun in ask position ("a memo", "a
   report", "a doc") told the bot to PROPOSE a task instead of answering. The Grok bot the
   owner named writes the memo. Here: answer + card, a tap, a task, a sandbox, a document, a
   verification receipt - a minute for a memo that fits in a message.
4. **The chat transcript was a grounding SOURCE.** `_mint_task` mounted `conversation.md`
   into `/task/inputs/`. Three consequences: `has_inputs=True` on every chat-born task, so the
   talk door (ADR-058) was structurally blocked on every follow-up but acks and reactions -
   the "close" incident's exact shape, reinstated for precisely the tasks bots mint; the
   transcript became the artifact's default `sources`, so the grounding law demanded that
   every fact in the bot's document come from the chat; and the namer still ran on a task the
   conversation had already named.
5. **Replies did not stream.** ADR-057 §1 says "streamed as text"; v1 polled every 1.5 s and
   the reply landed whole. `_reply_once` wired no `emit_event`. The task lane has had the
   two-lane socket since M2; conversations had nothing. This is the literal "no answer comes
   up straight".
6. **The reply claim could wedge.** `conversations.replying` was a bool with no lease. A
   worker killed mid-reply left it true forever; every later reply spun 80 × 1.5 s, wrote
   "did not reply in time", and only then released it. The curator (a second model call after
   any line matching "our", "I'm", "don't", "stop", "instead"…) ran INSIDE the claim, so the
   next message waited behind it.
7. **The latency stack.** send → 202 → DBOSClient enqueue → worker queue poll → claim → four
   memory queries + `mark_used` + a 30-row history read → compile → the whole model call →
   commit → the web's next poll. No first-token signal; "is replying…" was inferred from the
   last row's verdict.

## §1 The law

Capture runs nothing. A Routine executes nothing. A Conversation executes nothing. A task turn
is talk until it is work. Now, the piece those four left open:

> **A bot talks in exactly one place: its Conversation. A Task is work.** Talk on a task thread
> is Plane's courtesy (the answerer with the thread, ADR-058), never the bot's second voice.
> The reply streams, and the conversation the task came from is context, never a source.

## §2 One engine (W1)

`TaskDeps.talk` is REMOVED. `definition_for` compiles a bot to TALK iff `deps.conversation_id`
is set - one flag, one lane. The task talk door (`_route_turn`) seats the `answerer` on every
express step, direct threads included; `_attempt_express` hands it no bots. A direct task's
WORK turns still run on its one bot in a frame (the DM lane, ADR-045, unchanged); its talk
turns ("thanks", "?", "what did you mean") are the answerer's one sentence with the thread
mounted, exactly as on every other task. The pin's blocker (`TurnFacts.has_bots`) holds on a
direct task's FIRST turn only - turn 1 is the bot's work by construction (a gesture, a routine,
`--direct` said so) - and from turn 2 the door decides: acks, reactions and questions are the
answerer's; an imperative is the bot's. The conversation compile grants `task.propose` and
the bot's read-only reach (`web.fetch`, `web.search`) and nothing else - `task.escalate` (a
grant no conversation could ever wire) is gone. The web's "your bot answered" on the direct
lane became "your bot did the work", which is what that lane means.

## §3 Context, never a source (W2)

The conversation a chat-born task came from is not an input at all: it IS the thread. `_mint_task`
no longer attaches `conversation.md`; `operations/thread.render_thread` opens the root task's
transcript with the conversation's newest lines up to the minting message as **turn 0**
(`conversation_prelude`, bounded by `PRELUDE_ROWS` / `PRELUDE_CHARS`, deterministic per task
because message rows are append-only and the bound is the row's own `origin_message_seq`). So
the planner's brief, the executor's `/task/thread.md`, and the answerer's inline thread all read
the talk that led here under the thread's own wall - "instruction and context, not a source" -
and the transcript's anchor stays the task's own opening ask (turn 0 is context the budget may
drop first; the renderer gives it its own heading, outside the turn numbering). Consequences:
`has_inputs` is false on a chat-born task, so the talk door works on its follow-ups; the
verifier never holds a bot's fact to the chat; `deps.inputs` stays the real sources. The task
is named from its goal at the mint, so the namer never runs on a task the conversation already
named. A first cut of this section carried a `TaskInput.role` primitive; it was removed the same
hour - declared-never-read once the thread carried the talk (ADR-008: removal + replacement +
reason, here).

## §4 Documents are inline (W3)

The conversation door's `deliverable` fact counts only what a message cannot hold: a
spreadsheet, a deck, a collection, a document with a NAMED FILE FORMAT (docx, pdf, html), or
an ask that runs (run/execute/deploy/benchmark/scrape/crawl, "build me an app"), acts in a
connected service (a write verb + a catalog service word, the ONE intent grammar of ADR-054),
carries attachments, or is sized past a message. A memo, a summary, a list, a table, a draft
email, a code snippet is the reply. CHAT_LAW now says so: answer in the message, in markdown,
whole; propose a task only for a file, a run, a verified deliverable, or an action; never a
card for something the reply can be.

## §5 The reply streams (W4)

Conversations get the task lane's two-lane socket. `GET /v1/conversations/{id}/events?since=
<seq>&token=` (SSE, the fifth stream route beside the task pair; never compressed, never
buffered; `Last-Event-ID` == `since`): Lane 1 replays and then follows the durable `message`
rows (`id: <seq>`, the same `MessageOut` the list route returns - `_append` NOTIFYs a pointer in
its own commit and the hub reads the row back); Lane 2 carries `chat.start` / `chat.delta` /
`chat.activity` / `chat.end` frames (id-less, anchored on `after_seq`, the message tap's own
slices - the tap emits chat frames whenever the deps carry a conversation and no task). The
credential re-proves every `stream_reauth_seconds` against `Conversation.get_for`, the ADR-031
leash. The web's Chat tab folds the stream (a streaming bubble under the bot's name, tool
activity as a quiet line, the durable row replacing the bubble on arrival) and keeps the old
poll only as the disconnected fallback.

## §6 The lane cannot wedge (W5)

`conversations.replying_since` (migration `c1d2e3f4a5b6`): the claim is a LEASE. `claim_reply`
takes the lane when it is free OR the holder's claim is older than `chat_lease_s` (the reply's
wall clock + a margin); `release_reply` clears both. Every reply runs under
`worker.chat_timeout_s` (default 150 s) - a timeout is a SYSTEM row wearing its reason, never
silence. The `reply_chat` job waits past the lease before it gives up, so a dead holder's
message is answered by the very job that waited on it. The curator runs AFTER the claim is
released: teaching never delays the next answer.

## Rejected

- **Keeping the bot's talk turn on task threads, unified behind one `talk.reply()` function**
  (Option C). It keeps two records for one act (a step narration on a task; a message row in a
  conversation) and two surfaces the user must choose between. The constitution already says
  where talk lives.
- **Collapsing conversations back into tasks** (revert ADR-057). A task turn is a DBOS
  workflow with dequeue, plan, frontier, deliver, title - ten round trips before the model for
  "hi". The owner's own verdict on that shape ("it's not like a conversation") stands.
- **A "Chat / Agent" toggle** - declined in ADR-058 for the same reason: a switch makes the
  user do the routing.
- **Polling the DB inside the SSE loop for new rows** instead of NOTIFY. The hub exists; a
  second delivery mechanism for one socket is the thing "boring beats clever" forbids.

## Consequences

- **One migration** (`replying_since`). **Zero new dependencies.** One new stream route, four
  new live frame types (their schema rides the route's OpenAPI response so the web types
  regenerate from the one contract), one removed field (`TaskDeps.talk`), one removed grant
  (`task.escalate` on the conversation compile), one removed mount (`conversation.md`), a turn 0
  on the thread transcript, two conf dials (`COMPUTER_WORKER_CHAT_TIMEOUT_S`,
  `COMPUTER_WORKER_CHAT_LEASE_S`).
- **CLAUDE.md**: the ADR-058 rule loses "a DM thread's talk turn runs on its bot compiled to
  TALK" and gains this ADR's line.
- **Tests:** `tests/unit/test_conversation_door.py` (the file-shaped rule, the tightened
  detectors, the compile's grants), `tests/unit/test_chat_stream.py` (the tap in chat mode),
  `tests/unit/test_conversation_prelude.py` (turn 0: the anchor law, the heading, the numbering),
  `tests/integration/test_bot_roster.py` (the conversation rides the thread, never an input),
  `tests/integration/test_chat_lane.py` (a memo is the reply; the reply streams and lands; the
  lease; the curator after the claim; a chat-born task's talk turns are the answerer's and its
  work turns the bot's), `web/app/lib/chat-stream.test.ts` (the fold).

## The proof (2026-09-02, live on the dev stack, worker + API restarted on this code)

Against `release-notes-scribe` v3 (published, work_mode ask), a fresh conversation, the SSE
stream opened BEFORE the first message (`curl -N …/v1/conversations/{id}/events?since=0&token=`):

| Step | What was sent | What came back |
|---|---|---|
| 1 | "who was karl schwarzschild, in two sentences?" | verdict `answer · talk`; **first `chat.delta` 0.61 s after the POST, the durable bot row (`id: 2`) 1.66 s after** - 9 deltas, 1 start, 1 end, a two-sentence answer |
| 2 | "write me a short memo on his 1916 solution, three paragraphs" | verdict `answer · talk`, **no nudge, no proposal card**; the memo streamed inline (1,642 chars, opens `**Karl Schwarzschild's 1916 Solution…**`) |
| 3 | "write the memo as a pdf" + Start as task | verdict `start · the explicit gesture`; the task `title` = the goal, `inputs` = **[]** (no `conversation.md`), `direct_bot` = the bot; `task.planned` seq 5: `route direct · dial`, one step on the bot |
| 4 | task page: "thanks! what did you mean by the second paragraph?" | `task.planned` seq 23: **`route express · anchor`, agent `answerer`**; the answer streamed as `message.delta` frames and delivered as one sentence (the Schwarzschild radius, the two singularities); no frame, no artifact |

The chat-born task, both turns: 4 model calls, $0.0023. The whole stream over the session: 41
`chat.delta` frames, message ids 1 to 6 in order. Found live and fixed on the way: the
conversation stream route had to join `_STREAM_ROUTES` (the `?token=` concession is per route),
and the replay converted rows AFTER its session closed (a `DetachedInstanceError` the new wire
test `tests/integration/test_chat_wire.py` caught before the browser would have). Found by the
owner in the browser minutes later: a reply's last delta frames can land AFTER the durable bot
row (the batch notifier's flush tick racing the row's commit NOTIFY), and the fold re-opened a
bubble holding only the tail. The fix is the anchor doing its job: a live frame whose
`after_seq` is below the newest bot row's seq belongs to a reply already on the record and is
dropped (`web/app/lib/chat-stream.ts`; the shape is pinned in its test). `just check`
green; `just test` 1949 passed, 1 skipped, and one routines auto-pause test that fails only inside
the full parallel run - it passes serially, 4-wide on its own file, and 4-wide beside every new
chat test (untouched by this ADR; load-sensitive; recorded, not hidden).
