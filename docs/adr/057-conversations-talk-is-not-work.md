# ADR-057: Conversations: talk is not work

**Status:** accepted (2026-09-01)
**Amended by:** ADR-059 (2026-09-02) - the reply STREAMS (§5's v1.1, built); the nudge fires only
for what a message cannot hold (a memo is the reply); the conversation rides a chat-born task's
THREAD as turn 0, never `/task/inputs/conversation.md`; the reply claim is a lease. ADR-060 §6
(2026-11-15) - the door reads facts only: rung 5 (work-shaped nudge) is gone, and a "yes" on a
proposal is the bot's `accept_pending`, never a word list.
**Amends:** ADR-045 §4 (the DM lane, retired and replaced) · ADR-056 §5 (channels become
transports for conversations; the channel ledger is replaced by `messages`).

## Context

Plane had one unit of anything: the Task. ADR-045 built bot chat on top of it - a DM was one
task, every message a turn - so a one-line question to a bot planned a step, booted a frame,
published the answer as a document, emitted a delivery, ran the archivist, and signed a
verification receipt, thirty to ninety seconds later, in the task list next to real work, and
a bot's question arrived as a numbered form. ADR-056 then built memory, channels, and templates
on that foundation. The owner's verdict on 2026-09-01: "it's not like a conversation at all".
The diagnosis: **talk was implemented as work.** The fix is a missing primitive, not a polish
pass.

## §1 The constitution's third twin

Capture runs nothing. A Routine executes nothing; every fire mints an ordinary Task. Now:
**a Conversation executes nothing; every piece of work it needs mints an ordinary Task.**

A Conversation (`conversations`) is one thread of talk between a person and one bot: cheap,
fast, unverified, many per bot ("New chat" per topic). Its rows are `messages`: a person's, the
bot's, or the machine's. A reply is ONE frameless model call - the bot's instructions under the
charter, its memory lane, the thread - streamed as text. No plan, no sandbox, no publish, no
verification, and therefore no way to produce an artifact by accident. Every artifact still
comes only from a Task, so every artifact is still verified.

Channels (ADR-056 §5) become transports FOR conversations: an inbound email, webhook post, or
Telegram message appends a user row to the channel's thread conversation; the bot replies
conversationally; the reply goes back out through the same transport. The channel ledger
(`channel_messages`) is REMOVED and REPLACED by `messages`: two message tables for one thread
was the DM-as-task mistake in a new coat. The DM lane (`dm:{name}` task threads, the
`/v1/bots/{name}/chat` routes) is REMOVED and REPLACED by conversations; `direct_bot` tasks
remain for routines and for chat-born direct work.

## §2 The kickoff rule: a deterministic door, the model as a proposer, consent as the gate

Every user message resolves to exactly one verb, journaled on its row with its reason
(`verdict`, `verdict_reason`, `facts`, `consent`):

| verb | what happens |
|---|---|
| answer | one streaming model call in the conversation, text only |
| propose | the bot answered AND attached a task proposal card (goal, deliverable, why) |
| start | an ordinary Task minted: creator the person, DIRECT to this bot (the person is talking to it; a proposal may hand the work to the planner's seats with `direct=false` when it is outside the bot's role), `origin_conversation_id` + `origin_message_seq`, budget capped; a task card lands in the chat |
| steer | a reply on a running chat-born task steers it (the frontier-boundary merge, ADR-018) |
| follow_up | a reply on a delivered chat-born task continues it (ADR-018: a follow-up continues the task) |

The model can produce answer and propose. It can never produce start. The door
(`operations/chat_verdict.py`, pure) walks a ladder, first match wins:

1. `work_mode = never` → answer.
2. An outsider (a channel sender who is not the owner) without a work grant → answer; the bot
   may still propose, and the proposal waits for the owner. An outsider never starts spend.
3. The explicit gesture (the Start-as-task button, a "yes" on a proposal) → start, or steer /
   follow_up when the message anchors on a task card.
4. A task anchor without a gesture → steer (running) or follow_up (settled).
5. Work-shaped facts → answer, with the bot NUDGED to propose unless the answer fits in a
   message. The facts: attachments; a deliverable named (ADR-032's own detector); a sandbox
   verb (run, execute, build a script); an action in a connected service; size words or a
   think-hard ask (the express door's own vocabularies).
6. Otherwise → answer.

After the model speaks, `settle_verdict` finishes the record: a proposal becomes **propose**,
or **start** under a standing grant - `work_mode = auto` AND the owner's own message AND the
goal inside the bot's per-task cap. Rock-solid because: same inputs, same verdict, testable
with no model; money moves only with a gesture, a tap, or a standing grant inside a cap; every
verdict is a row wearing its reason; nothing new executes.

## §3 The dials and the defaults

`bots_registry.work_mode` (ask | auto | never, default **ask**) and `task_budget_usd` (default
the routines' $2) are per-name settings beside the face. `bot_channels.work_grant` (default
false) lets a channel's non-owner senders reach the ladder's rungs 3 to 6. The brief's rule of
thumb, given to every bot in conversation: if the answer fits in a message, answer; if the
result needs a file, a run, or a verification, propose.

## §4 The result comes home

A chat-born task's delivery, failure, and any gate it opens land in the conversation as
SYSTEM rows (`kind` = started | delivered | failed | gate, with `task_id`, `artifact_ids`,
`gate_id`), gates answerable in place (the inbox's finish-things law). The task page still
exists for anyone who wants to watch the machinery; it wears "from a chat with {bot}".

## §5 Memory, spend, and the reply lane

The bot memory lane (ADR-056 §1) mounts into every reply - the place it always mattered most -
and the curator runs after replies on personal bots. Chat spend is attributed at time of spend
(`usage_events.conversation_id`, a column like every origin; spend per conversation is a
join). Replies run on the worker (Rule 5) under the `chat-{conversation}-{seq}` job with a
per-conversation claim so replies never race out of order; the web polls the thread while a
reply is pending. Streaming deltas over a conversation socket are the named v1.1.

## §6 What does not ship in v1

Connectors inside replies (a reply must not pay probe latency; the bot proposes a task when it
needs its hands); attachments on chat messages (a file is work: use Start-as-task); a bot
comment after a delivery (the system row reports it); streaming deltas.
