# ADR-058: Talk until it is work

**Status:** accepted (2026-09-02) · built the same day (W1-W4), gate green, proven live (the proof
block at the end).
**Amended by:** ADR-059 (2026-09-02) - §4's "a direct thread's talk turn runs on its own bot
compiled to TALK" is REVERSED: a bot talks only in its Conversation; a task thread's talk turn
is the answerer's in every thread, and `TaskDeps.talk` is gone.
**Amends:** ADR-049 §1 (the anchor: planner → the answerer with the hatch; the classifier seat
`triager` REPLACED by the answerer itself), ADR-049 §3 (the hatch W3, built, in two forms),
ADR-049 §4 (the hysteresis floor RETIRED), ADR-032 (a plan-sourced stamp never binds a later
turn), ADR-052 §1.4 (the monotonicity oracle: "no wider grants", not "never lighter").
**Grounding:** an unpublished landscape note of 2026-09-02 (how shipping assistants and
agent products decide talk vs work per turn, adopt/decline) and the flight recorder of task
`95d57386-7d4e-4eab-9ebe-81d7bfed2ace`.

**Amended 2026-11-15 (ADR-092):** talk or work becomes the Lead's ending: a reply is talk, `submit_plan` is work in the same run (`plan.edited{lead}` and `task.planned{route_source: lead}`, the hatch's shape), and `propose_task` answers and offers the card. Takes effect at the Lead's cutover.

## Context - the receipt

The owner typed "close" on turn 5 of a task thread, meaning *you're close*. The thread's first
four turns had gone through the express lane for a fifth of a cent. Turn 5 fell to the planner,
which returned one writer step named **"Close the conversation"** and published a 53-byte
document: "Understood, closing the thread on Karl Schwarzschild." Turn 6, "?", became a
researcher summarising the thread and a typesetter formatting it as single-page HTML. Turns 7
to 11 each ran researcher → writer → typesetter. Eleven turns: $0.41, 17 artifacts, 142 model
calls; the eight talk turns among them paid for all of it. Turn 10, a message about someone in
danger, was answered with a typeset memo two minutes later.

Three structural facts, not one bug:

1. **The door was a closed vocabulary.** `express_tier` knew thanks/ok/yes/no and no
   reaction - not close, wrong, not quite, hmm, "?", nor "no penrose huh".
2. **The rung for the ambiguous middle was never built, and could not have helped.** ADR-049's
   `triager` was to be "fed the goal head + a dial summary, never the whole thread"; "close" is
   only legible with turn 4 in view.
3. **The planner cannot reply.** `ProposedStep` publishes or feeds a step that does; `validate()`
   rejects a plan where nothing publishes. Once a turn reaches One, the minimum outcome is a file.

And two amplifiers: ADR-032's plan-stamp (`deliverable=document, source=plan`, written by the
accidental turn) was passed to `validate()` as binding on every later turn, and every turn's own
artifacts were named to One as "attached sources (inherited from the parent task)" - material to
gather and reduce, so a researcher led every plan.

ADR-049 chose the planner as the anchor on one sentence of its failure matrix: *"Standard misroute,
the question was trivial: the cost is politeness, not correctness."* The record says the two
mistakes are asymmetric by two orders of magnitude. A talk turn misrouted into a plan cost a
nonsense artifact, a binding stamp, a poisoned source, and six more plans. A work turn misrouted
into a reply costs one fast call and two seconds, and the reply says "this needs a real pass" and
escalates itself.

## §1 The constitution's fourth twin

Capture runs nothing. A Routine executes nothing. A Conversation executes nothing. Now:

> **A task turn is talk until it is work.** When the door cannot tell, it says talk; the answerer
> with the thread replies, and escalates ONCE - typed, journaled - if the turn is work. Only a
> user's own dial makes a later turn work by default.

The resolution law of ADR-049 keeps its order and gains an honest anchor:

> **dial > deterministic work > deterministic talk > the answerer with the hatch (the anchor) >
> the planner** - and the source is stamped (`route_source ∈ dial | detector | anchor | hatch |
> planner`).

Three principles, each with its mechanism below: **when unsure, talk** (§2); **the reader routes** -
code decides only what it can prove in five milliseconds, the model with the thread decides the
rest, once (§3); **accidents are not DNA** - nothing an unsure turn writes may bind the next one (§6).

## §2 The door (`ai/policy/triage.py`)

- **The backward-looking acts.** SWBD-DAMSL gives every utterance a forward-looking function
  (statement, question, request) and a backward-looking one (accept, reject, answer, backchannel).
  The door recognised requests plus two backward acts (accept, thanks). It now recognises the
  REACTION class - close, closer, almost, not quite, wrong, hmm, huh, "?", emoji-only, a reaction
  or ack word opening or closing a line of at most six words with no object phrase and no work
  named inside it. Zero work in any of these, so the class rides the acknowledgement fast path:
  it expresses in ANY thread except standing work, and a "close" in a fleet thread is one sentence,
  never N seats.
- **The hard WORK shapes abstain to the planner, as before and slightly wider:** an imperative
  opener (the doing verbs, now including the transform verbs: turn, rewrite, reshape…), a
  deliverable named in ask position ("a proper document on…", "into slides"), a bare kind noun
  in a request-shaped turn-1 line ("memo on Q3"), a modal imperative ("can you write me a memo",
  which a plain question with the same noun - "what is a memo of understanding?" - is not), the
  scatter and action shapes (carried on `TurnFacts` by operations), and every blocker ADR-049
  listed (dials, mode, attachments, pinned bots/connectors, size words, code fences, length).
- **The anchor.** The residual - unshaped, under 480 characters, no blocker, no work named - is
  `ExpressDecision(tier="chat", anchor=True)`: the answerer with the hatch, the record wearing
  `route_source=anchor` and the reason "unsure whether this is talk or work - answer first,
  escalate if it is work". `COMPUTER_WORKER_TURN_ANCHOR=plan` restores ADR-049's law for a drill
  or a rollback; nothing else changes.
- **Thread facts** ride `TurnFacts`: `previous_escalated`, `scatter_shaped`, `action_shaped`,
  `anchor`. ADR-049 §4's hysteresis floor is NOT applied - under an answer-anchor it would cost a
  talk turn a document right after the thread produced one; the fact stays on the record and in
  the answerer's brief, and the hatch is what keeps a work turn from being answered twice.
- **`direct_bot` is no longer a blocker.** A DM thread's talk turn is answered by its own bot
  compiled to TALK (§4) - the door only says "a breath"; operations picks the agent.

## §3 The hatch (ADR-049 §3's W3, built) - two forms

The express step wires two closures onto `TaskDeps` (the proposer's shape - the engine stays
persistence-free) and grants the answerer two tools:

- **`escalate_to_plan(reason, shape)`** - hard. The answerer says the turn is WORK. It answers
  what it can in a line, says a plan is starting, and the step completes; the completion path
  (the outline's and the map's expansion, one lane over) re-plans the SAME turn through the ladder
  with talk closed - `plan.edited{hatch}` + a `task.planned` wearing `route_source=hatch` and the
  reason - and the frontier picks the new READY steps up. One hop; a second call is refused. The
  archivist runs on an escalated turn (a document is a memory; a "thanks!" is not).
- **`propose_task(goal, deliverable, why)`** - soft. The answer fits in a message but a
  deliverable would serve: a durable `task.proposed` frame lands under the reply; the web renders
  the ADR-057 card (Start / Not now); Start is a follow-up with the deliverable dial set (the
  dial-is-the-plan path, instant); Not now is nothing. Consent is a tap, never the model's. A card
  never outlives its turn. The transcript carries it as a marker so the next reply knows.

The answerer never interviews (`can_ask_user=False` stays): "close to what?" is a sentence.

## §4 Talk resolves before the lane, in every mode

`draft_plan` became `_route_turn` + `_commit_plan`. The talk door sits ABOVE the direct, fleet,
sectioned, dial, scatter, action and planner branches (the facts carry the scatter and action
shapes, so those lanes keep their place). A direct thread's talk turn runs on its own bot
compiled exactly as a conversation reply (`compile_bot(conversation=True)`, decided by
`TaskDeps.talk` - no caller can hand a talk turn a sandbox); a fleet thread's "close" is answered
once. Mode stays thread-DNA; weight stays per turn.

## §5 One may say "no work"

`PlanProposal.reply` + `reply_why`: a follow-up that is a reaction, a question about the thread,
or an ask a sentence settles gets a REPLY - zero steps, the express shape, the record wearing
`route_source=planner` and the reason. The brief gained a follow-up section (turn N: read the
request as a reaction first; the thread's own earlier deliverables are named APART from inherited
sources and are revised in place, never re-researched) and the planner prompt gained the rule.
Steps present still win.

## §6 The binding law - accidents are not DNA

`_resolve_kind` returns `(kind, fresh)`: `fresh` means THIS turn chose the kind (a dial, or this
message's own language). `_plan` binds `validate(deliverable=…)` only when the kind is fresh or
the stamp is USER-sourced; a carried auto/plan stamp is the chip's read-back and nothing more.
`replan` binds only a user stamp. The tail re-stamps a carried non-user stamp the new plan
disagrees with, so the row says what THIS turn became; talk and direct turns leave it alone. A
remembered preference "shapes HOW a deliverable is made, never WHETHER one exists" - in the
brief's memory section and the planner prompt.

## §7 The surface (ADR-049 W5, landed)

`task.planned` carries `route`/`route_source`/`route_why` to the web; the epilogue wears the turn
in the reader's words ("answered", "answered, unsure first", "answered, then planned", "planned",
"planned in sections", "your bot answered") with the door's reason on hover. The proposal card
renders under the reply with Start / Not now. Per DESIGN.md: monochrome, the bot-chat card's shape.

## §8 The book and the oracle (ADR-052 amendments)

Twelve rows joined `evals/range_book.py` from the thread and the law (provenance found-live /
adr), five ADR-058 failure-matrix slugs map onto them, and six rows were RE-LABELLED with the
reason on the row (`ambiguity-the-thing`, `boundary-question-cap-over`, `garble-question-mark`,
`garble-arabic-time`, `dial-direct-blocks`, `rewrite-plain-email` kept). The monotonicity oracle
(`evals/transforms.route_is_monotone`) now reads "no wider GRANTS under garbling": a garbled
standard prompt may fall to the anchor (no tools, no frame, one fast call) but may never become
express-lookup (the web grant). "Never lighter" was the steer when the anchor was the planner;
the hatch made the light route safe.

## §9 What did not change, and known limits

- The blockers: an attached thread (`tasks.inputs` is thread-level, so a thread that opened with a
  file never anchors on talk - only acks and reactions pass). Named for a later `inputs_added`
  fact on `task.continued`.
- The DM lane's WORK turns still plan one step on the bot (ADR-045); the routine door never
  expresses (ADR-048/049, builder's call, unchanged).
- The bot's talk turn on a task thread mounts the thread and the ambient facts, not the bot's
  memory lane (ADR-056 §1) - v1.1.
- Nine of nine memories surfaced on every planning pass were deliverable-format preferences
  learned on other tasks; scoping them to the resolved kind is the named follow-up.
- The seat: on the dev box one non-reasoning model serves every text class including One's; the
  anchor would have produced a document with any model, but the seat deserves a reasoner.

## Rejected

- **Building the `triager` as designed** (ADR-049 W2): a classifier that never sees the thread
  cannot classify a backward-looking act; the answerer with the hatch has strictly more context,
  one call, the same cost class. Replaced, not deferred (ADR-008: removal + replacement + reason).
- **A Chat / Agent mode toggle** (the Manus shape): Plane's levers stay outcome-shaped - the
  deliverable and scale chips say "work", "quick:" says "talk", the epilogue discloses what
  happened. A mode switch makes the user do the routing.
- **An embedding router at the door now**: a dependency the anchor flip makes unnecessary; the
  named v2 for vocabulary drift, beside ADR-049 §10's trained encoder.
- **An LLM judge at the door** (already declined in ADR-049): the answerer is the reply, and its
  escalation is a side effect of trying to answer.
- **A persisted per-thread "conversation mode"**: express decays per turn (ADR-049 §4); talk is a
  verdict on a message, never a state on a row.
- **A prompt-only fix**: a better prompt would still have produced a seven-word document, because
  the proposal schema and rule 6 left One no other output.

## Consequences

- **Zero migrations. Zero new dependencies.** One new durable frame (`task.proposed`), one new
  plan-edit kind (`hatch`), two new route sources (`anchor`, `hatch`), one conf dial
  (`COMPUTER_WORKER_TURN_ANCHOR`), two new tool grants (`task.escalate`, and `task.propose` on the
  answerer), `TaskDeps.escalate` / `TaskDeps.talk`, `PlanProposal.reply`, the contract re-exported
  (`docs/api`) and the web types regenerated.
- **CLAUDE.md gains** (this ADR): *a task turn is talk until it is work (ADR-058): the door's
  verdict comes before every lane; a reaction or an unsure turn goes to the answerer with the
  hatch, never the planner; the answerer escalates once (`plan.edited{hatch}`, `route_source=hatch`)
  or proposes a card; only a USER stamp binds a later turn; `route_source ∈ dial | detector |
  anchor | hatch | planner`.*
- **Tests:** `tests/unit/test_triage.py` (+6), `tests/unit/test_range_book.py` (12 rows, the
  matrix, the oracle), `tests/unit/test_events_roundtrip.py` (the frame), `tests/integration/
  test_talk_door.py` (9: the thread replayed, the hatch, the card, the binding law, One's reply),
  `web/app/lib/task-view.test.ts` (+3).

## The proof (2026-09-02, live on the dev stack, task `82b7fb30`)

The thread's own messages replayed through the CLI (`computer do` + `tasks follow-up --wait`)
against the API and worker running this ADR's code:

| Turn | Message | Route on `task.planned` | What came back |
|---|---|---|---|
| 1 | who is karl schwarzschild | express · detector · short question | one sentence |
| 2 | what did he do | express · detector · short question | one sentence |
| 3 | **close** | **express · detector · a reaction to the last answer - nothing to plan** | one sentence, zero artifacts |
| 4 | ? | express · detector · reaction | one sentence |
| 5 | Wait, can you actually give me a proper document on who improved on Karl's solutions… | standard · planner | ONE document, published |
| 6 | no penrose huh | express · detector · reaction | a Penrose answer |

Six turns: 16 model calls, $0.029, 1 artifact - the document the user actually asked for. The
original thread's first six turns: 22 calls, $0.029, 3 artifacts, none of them asked for; its
eleven turns: 142 calls, $0.41, 17 artifacts. `just check` green; `just test` 1935 passed, 1
skipped.

## Amendment 2026-10-15 - a typed hatch and a smaller door (agent loop, round 1)

The hatch (`escalate_to_plan`) names a lane, `computer | plan`, and the hosts the work touches.
A computer hatch mints the one-Step computer plan without the planner (`route_source=hatch`);
a plan hatch re-plans as before. Hosts are pre-approved only when the user named them (a
host-boundary match in the message, the goal or a prior host); a host the model merely relays
asks on first open. Connector precedence holds on the computer hatch and on the failure floor:
a goal a live connector covers is the operator's, not the browser's.

The door shrank to its high-precision core: the answerer is the catch-all. `navigate_book`
went from 69 to 48 rows; 21 rows plus 6 flips moved to `evals/computer_book.py` as hatch rows.
The unanchored click/tap reply-object veto stays. `route_source` is unchanged
(`dial | detector | anchor | hatch | planner`).

## Amendment 2026-11-15 - the Lead decides each turn (ADR-092)

No code decides talk or work for a new task turn: intake's recipes (fleet and direct Bot turn 1, a long-form
size dial, a reachable deliverable dial) or the Lead's one step decide it, and the Lead's reply is the talk.
The door, the anchor and the hatch remain only for an answerer step running when the cutover deploys.

**Amended 2026-11-15 (ADR-092 §8, the v1 doors deleted):** The hatch (`escalate_to_plan`, `EscalationResult`, `expand_after_escalation`) and the anchor setting (`PRISMOID_WORKER_TURN_ANCHOR`) are deleted with the answerer; the Lead's `submit_plan` is the one way a turn becomes work. `route_source=hatch` and `PlanEditKind.HATCH` stay readable on stored rows.

**Amended 2026-11-15 (ADR-092, the planner deleted):** `PlanProposal.reply`/`reply_why` are deleted: no plan says "no work"; the Lead replies in text. `route_source=planner` stays readable on stored rows only.
