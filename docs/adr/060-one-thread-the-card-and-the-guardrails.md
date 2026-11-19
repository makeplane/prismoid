# ADR-060: One thread, the card, and the guardrails

**Status:** accepted (2026-09-02) · built in five steps the same day (§1 first, on the owner's
word; §2-§5 after), gate green, proven live (the foot) · the design canvas "Bot Chat, One
Thread" is the visual record.
**Amends:** ADR-057 §2 (the owner's standing grant becomes the default), ADR-057 §3 (a third
per-bot dial, `threads`), ADR-059 §4 (the door gains the card anchor and the reversible-mistake
rule). **Builds on:** ADR-059 (one talk engine, the stream, the lease).
**Amended:** 2026-11-15 - the card anchor and the "yes" are the bot's to judge through typed tools,
never a word list (§6).
**Amended:** 2026-11-15 (the chat book becomes a dataset) - `evals/chat_book.py` and its walker are
retired with `ai/policy`; the door is `operations/chat_verdict.py`, its every rung pinned by
`tests/unit/test_conversation_door.py`. The bot's choice on a line is scored by the conversation set
(`evals/datasets/conversation.jsonl`, `--suite conversation`), each row's `source_id` the book row it
came from; a book row without a scenario is a door fact or the same behaviour as a kept row. A misroute
found live grows the set (§5, point 3).
**Grounding:** the owner's review of ADR-059 on 2026-09-02 ("do you think the multi-child approach
within the bot is a useful thing?"), the assessment that followed (the data model is right, the
presentation confuses: two composers, the work leaving the room, machinery vocabulary on a consumer
surface), two directions drawn on the canvas (One line: a thread is one operation; One thread: a
conversation with work as cards), and the owner's choice: **the conversation, with a setting for
threads, and guardrails so the bot never gets confused about when to start a task.**

## §1 The threads dial (built)

`bots_registry.threads ∈ one | many` (migration `d2e3f4a5b6c7`, default `many`), per name like
the face and the work dials, set through the face PATCH (`BotFaceIn.threads`, `BotOut.threads`)
and the Configure tab's "Threads" row (one chat · many chats). **One chat: the bot IS its one
conversation** - no rail, no New chat; `open_conversation` returns the row that exists (idempotent,
never a 409: a client that asks for "the chat" gets the chat); the Chat tab opens straight into it.
**Many chats: a chat per topic** - the list of chats appears once a second exists. A bad value is a
422 at the door. Proven live: threads=one → two opens, one id; threads=many → a new chat; "open" →
422. Tests: `test_conversation_door` (the dial), `test_chat_lane` (idempotent opens, the dial rides
every version of the name).

## §2 The thread as designed (built)

One composer. The Start-as-task button, the verdict and consent pills, and the "talk is not work"
footer left the chat (the record keeps every verdict; Details shows it). `web/app/routes/bots/
bot-chat.tsx` was rewritten around ONE CARD PER TASK: `lib/chat-stream.cardsOf` folds a task's
system rows (started · continued · delivered · failed · gate) into one card placed where the task
started; a live card fetches the task and mounts `useTaskStream` for its steps and phase, a
settled card reads `initialView` of the task detail; gates draw as the existing docks inside the
card; the file chips, a Reply (the explicit anchor) and a quiet Details link sit on its foot; a
bot line that answered a later turn renders as a message with its chips. The chat list replaces
the chip rail, shown only when `threads = many` and a second chat exists (a lone many-chat shows
one quiet New chat). The task page keeps its "From a chat with {bot}" link back.

## §3 A reply under a card continues the card (built)

The card is the anchor by proximity, deterministically and on the record. `continues_card`
(`ai/policy/conversation`, pure) says whether a line is a reaction (ADR-058's class, now public as
`triage.is_reaction`) or a STEER opener ("shorter", "drop the open questions", "use last week's
numbers", "make it a table", "that's too long"); an ack that turns into a new ask ("thanks, now
recap Friday's review") is neither. `chat._card_anchor` applies it only when the newest thing in
the thread that is not the person's own line is a task's card (a plain bot reply in between means
the thread moved on); the verdict then reads STEER (running) or FOLLOW_UP (settled), the row
wears `anchored_by = card`, and `continue_task` mints the next turn. The continuation's answer
comes home as the BOT's own line (`on_task_settled`, turn ≥ 2: role bot, the summary, the turn's
artifacts, the task on the row for Details), never a second "Delivered" receipt; a failure stays a
system row. A question is answered in chat; a fresh imperative goes through the door. The card's
Reply affordance stays as the explicit override (`anchored_by = reply`).

## §4 The owner's work starts by itself (built)

`work_mode` defaults to `auto` for new bots (migration `e3f4a5b6c7d8` moves the column default
only; existing bots keep their value; the Configure tab lists Auto first and says so). The owner's
own messages start the work the bot proposes inside the bot's per-task cap without a card; the
proposal card remains for strangers (channels without a work grant), over-cap work, and anything
the cap does not cover. Service writes keep the approval gate (ADR-054), drawn in the card. Money
never moves on a verdict alone - the cap is the consent the owner already gave.

## §5 The guardrails: answer or start a task (built)

The structural guardrail stands and is restated: a bot in a conversation holds no publish tool, so
an artifact can only come from a task, and every task is verified. The decision itself is made
robust by four rules:

1. **One typed decision, code first, model second.** The deterministic door decides everything it
   can prove (a named file format, a spreadsheet or deck or tracker, a run, a service action,
   attachments, size, the card anchor); the model only ever emits a typed verb through
   `propose_task`, never prose that "does" work.
2. **A wrong answer costs one tap, both ways.** A reply that turns out document-shaped (a title
   plus sections, or past a length line) carries a "Save as file" action: `POST /v1/conversations/
   {id}/messages/{seq}/save` (`chat.save_reply_as_file`) mints an ordinary task with the reply as
   its ONE source (`reply.md`), the document dial and the format set, the planner's seats (the
   writer, or the typesetter for a real file), the bot's cap, provenance on the row, and a card in
   the thread - idempotent per reply and format; a card that should have been a reply is simply
   read. The door does not have to be perfect; it has to be reversible.
3. **The problem book for chat** (`evals/chat_book.py`, the ADR-052 pattern): 33 rows - message,
   context facts, the verb the door must return, and for lines under a card whether they continue
   it - each with provenance, walked by `tests/unit/test_chat_book.py` on every commit through the
   pure door (`facts_of` · `verdict_for` · `continues_card`), $0. A model-graded lane for the
   residual (does the bot propose what a message cannot hold?) is armed and lands with its rows.
4. **Every verdict on the record, never in the chat.** Details shows why a card started, or did
   not; the thread stays clean. The cap, the budget gate, the approval gate, and the lease do not
   depend on the model being right.

## Rejected

- **One line as the only mode** (a thread is one operation): the canvas showed it is the right
  shape for do-this bots but throws away the back-and-forth the owner wants; it may return as a
  third value of the threads dial if a bot's job asks for it.
- **A mode toggle in the composer**: a switch makes the user do the routing (declined in ADR-058
  and again here).

## Consequences

Two migrations (`threads`; the `work_mode` default), one new route (save), one public policy
function (`triage.is_reaction`), one new pure predicate (`continues_card`), one new problem book,
and a rewritten Chat tab. Zero new dependencies. **CLAUDE.md gains** this ADR's line. Tests:
`test_conversation_door` (the dials, the anchor's source, the continue/plain tables),
`test_chat_book` (33 rows), `test_chat_lane` (idempotent opens under one chat; a reply under a card
continues the task and comes home as the bot's line; save-as-file; the auto default),
`web/app/lib/chat-stream.test.ts` (the card fold).

## The proof (2026-09-02, live on the dev stack, API + worker restarted on this code)

Against `release-notes-scribe` v3, a fresh chat, over the API (`scratchpad/proof060.sh`):

| Step | What was sent | What came back |
|---|---|---|
| 1 | "write the 3.2 release notes: …" + Start | `start · the explicit gesture`; the task ran and delivered; the thread holds user · started · delivered (the card) |
| 2 | "shorter, and drop anything about permissions" (no anchor chip) | **`follow_up · a reply under a delivered task's card`**, the same task, turn 2, delivered; the answer came home as **the bot's own line** with the task on the row and the revised file (1 artifact), never a second Delivered |
| 3 | "what changed in the approval gate, in one sentence?" (same card above) | `answer · talk`, no task: a plain bot line |
| 4 | "write me a short memo on the catalog rollout with three sections…" | `answer · talk`, **no proposal card**; a 1,301-char memo inline |
| 5 | Save as file (pdf) on that reply | `start · saved as a pdf file`: a task titled from the reply, `deliverable document · pdf`, **`inputs = [reply.md]`** (the reply is its one source), delivered with the file |

The threads dial was proven live at §1 (two opens, one id; a bad value 422). `just check` green
(backend unit 1490 → 1523 with the book, web 537); `just test` 2004 passed, 1 skipped, and one
routines throttle test that fails only inside the full parallel run and passes alone (untouched
by this ADR; the same load-sensitive family ADR-059 recorded).

## §6 Amendment (2026-11-15): the door keeps facts only

§3's `continues_card` and the chat door's "yes" pattern read the person's words for meaning, and each
grew a counter-case per phrasing (an ack that turns into an ask, an opener that turns into a
question, "yes please, but shorter"). Leading harnesses let the model's first output be the triage
and keep code for policy and state; so does this lane now.

- **The door reads facts, never words**: the sender, the dials, a gesture (`/task`, the button), the
  card's explicit Reply, attachments, and a line's length (the steer cap). Every other line is an
  ANSWER for the bot to judge (`operations/chat_verdict.py`).
- **A line under a card goes to the card when the bot hands it there**: the reply holds
  `continue_card` only while a task's card is the newest thing above the line that is not the
  person's own (the §3 proximity rule, now a state check in `chat_reply._card_above`: a plain
  reply in between, a moved task or an open gate offers no card). Code carries it out after the
  reply under the task row lock, by the task's state then: STEER a running task, FOLLOW_UP a settled
  one, with the person's line verbatim; a steer past its cap is refused by the tool. The line wears
  the verdict and `anchored_by = card`.
- **A yes or no on the waiting proposal is the bot's to hear**: while a proposal waits, the reply
  holds `accept_pending` / `decline_pending` in place of `propose_task`; an accept starts the card
  with `consent = proposal`, a decline sets it aside. The card's buttons stay the primary path.
- Only a sender who may start work holds a card verb; a pure talker holds none.
- **The problem book** (§5.3) now proves the door hands every row it does not own to the bot;
  what the bot then does is the conversation set's to score (`evals/datasets/conversation.jsonl`,
  played offline through the real door and lane by `tests/integration/test_conversation_set_offline.py`).
