# ADR-092 · The Lead: one model run decides a turn, and the record says how it ended

**Status:** accepted · 2026-11-15 (cut over: the Lead decides every new turn no recipe answers)
**Amends** ADR-049 (§10: `route_source` gains `lead`), ADR-052 (a book row grades the ending), ADR-058 (talk or
work becomes the Lead's ending), ADR-059 (a task thread's talk turn is the Lead's reply) and ADR-064 (the Lead
is a step).

## Context

A turn is decided today by a regex door, an answerer with an escape hatch and a planner: two model runs on
the worst path, a classifier in code, and a record that names the route but not how the turn ended. A client
learns "was this answered or planned" by parsing `route` and `route_source`, which are v1 implementation
words. The leading loops (Codex, Claude Code, OpenHands, the OpenAI Agents SDK, pydantic-ai) end a turn when
the model writes text, and add typed endings only for what a chat loop lacks.

## Decision

1. **One Lead run per turn, one typed ending.** `prismoid/schemas/lead.py` declares `LeadEnding`, a union
   discriminated by `ending`: `Reply` (the text, which streams), `SubmitPlan` (`say` plus a `PlanProposal`),
   `AskUser` (`kind` input or connect: one batch of questions, or one service), `ProposeTask` (`answer`,
   `goal`, `deliverable`), and, unattended runs only, `Fail` (a `FailKind`: `needs_owner`, `unavailable`,
   `refused`, and a reason) and `Deliver` (the `deliver_to_owner` sink: the result and `findings`). The run is a pydantic-ai agent whose output
   type is plain text plus tool outputs, so every ending lands in one model call and text stays a legal
   ending (tool choice stays `auto`). The Lead never parks and never waits: an ask ends the run and the
   answer starts a fresh one.
2. **Facts narrow the endings; code never guesses intent.** `LeadFacts` holds what intake knows without
   reading intent (turn, origin, unattended, clarify dial, whether this turn already asked, the deliverable
   dial, unread inputs, a direct thread's Bot, budget left). `LeadFacts.allowed_endings()` is the one rule
   that narrows the output type: unattended runs never reply in free text, ask or propose; they plan,
   deliver to the owner or fail; a deliverable dial or an
   input the Lead cannot read inline removes the reply (and the delivery); `clarify=never` or an earlier ask in the turn
   removes the ask; `clarify=always` on turn 1 allows only the ask.
3. **The discriminator is code's.** `ending` is skipped from each ending's JSON schema, so a model writing a
   tool output never sees or sets it.
4. **Additive wire values only.** `step.state` gains `ending` (`reply | plan | ask | propose | fail | deliver`)
   on the Lead step's terminal frame, the one frame every ending emits, and `findings` on a delivery's DONE
   frame; both are null on every other step and on older rows. `task.planned.route_source` gains the value `lead` (the field stays a string: stored rows hold the
   retired `classifier`). `PlanEditKind` gains `lead`: a plan ending is `plan.edited{lead}` plus a second
   `task.planned{route_source: lead}` after the turn's first one. A reply keeps today's express shape
   (`route: express`). No event type, table or column is added; `plan_edits.kind` has no check constraint.
5. **The Lead runs on `ModelClass.FAST_EXECUTOR` at `low` effort** (`AgentDef.effort`, over the class's routing
   settings): it writes every reply the person reads, so it is asked like the seat that wrote them before it.
   Deciding and planning are one call, so there is no per-message seam for `medium` when it plans.
6. **Where it runs and commits.** The Lead is the turn's express step (the express rung, frameless; it says
   "Thinking" before any read). Its ending commits in the step's one completion transaction
   (`operations/turn/commit.py`), so a crash before that commit leaves nothing: a reply is the express
   shape; a plan joins the turn as its plan edit, its steps ordered after every existing one, and its
   `task.planned`; a proposal is the card under the answer; a
   delivery is the line the turn delivers, its DONE frame wearing `findings`. An unattended failure is a typed
   failure, never a delivered line: the step escalates with a diagnosis from its kind (the escalation knocks
   the owner's inbox, so "needs the owner" is always seen), the task fails and the routine's failure ladder
   counts it; nothing parks (nobody is there to answer). An ask parks the step on the existing question or
   connect gate, its batch built by the intake's own builder (each question's default, the TTL,
   proceed-with-defaults when nobody answers; an ask whose batch cannot open as typed is refused inside the
   run), and the answer, or the defaults, start a fresh Lead run that may not ask again. Past the express wall the
   Lead lands on the one-step floor plan, never on an offline answer. Code prices the plan (`estimate`): the
   cost a person is shown, never a ceiling on a running step (amended 2026-11-15, below).

7. **The cutover: every new turn is the Lead's.** `operations/turn/route.py` routes a turn once, from intake's
   facts read before any stamp: a fleet's or a direct Bot's first turn, a long-form size dial and a deliverable
   dial its one step reaches run their code-made recipes (`route_source=dial`); every other turn, follow-ups,
   forks and routine fires included, is one Lead step (`route: express`, `route_source=lead`). The talk door,
   the scatter, action and navigate doors and the planner are no longer asked for a new turn, and an
   answerer step stored before the cutover runs as a Lead step (item 8). A Lead turn
   skips the intake round (the clarifier) and is titled after delivery. A Lead step that ended in a plan is
   never a sink fence 2 re-runs. Rollback is git: no switch brings the old routing back.

8. **The v1 doors are deleted (amended 2026-11-15).** The talk door, the ladder's probes (scatter, action,
   navigate), the express rules (the offline retry, the wall stop), the answerer and its hatch
   (`escalate_to_plan`) are gone, with every text detector the routing read (`ai/policy/{triage,doors,kinds,
   action,navigate,sizing,swarm}`, the question text helpers): `operations/turn` reads rows only. A plan's
   recipe step is expanded by code at commit: a scatter step becomes the map step (`operations/swarm`), whose
   synthesis takes over what waited on the step; a long-form step becomes the outline (`longform_plan`), the
   row sized like the dial, its assembly taking over what waited on it; a revision's recipe steps expand the
   same way. A recipe step at the end of a plan delivers through its shape. Bot replies run on the
   `grants.TALK` row (the answerer's hands less the hatch). `PRISMOID_WORKER_TURN_ANCHOR` is removed;
   `PRISMOID_WORKER_EXPRESS_TIMEOUT_S` stays as the Lead's wall, a deviation from the plan that named it for
   removal. Stored values stay readable: `route_source` keeps every older word, `PlanEditKind.HATCH` and
   `FailureKind.WALL_STOP` stay, and `answerer` aliases to `lead`.

## Consequences

- Clients read `ending` instead of inferring an ending from v1 route words; the route log is `route`,
  `route_source`, `route_why` and `ending`.
- Every new turn emits `route_source=lead` or `dial`; the web shows `lead` as the route's source.
- Old readers are unaffected: every addition defaults to what an old row means.

## Alternatives considered

- **`ending` on `task.delivered`.** Rejected: an ask ending delivers nothing, and a plan delivers turns later.
- **`ending` on `task.planned`.** Rejected: a reply turn's `task.planned` is written before the Lead runs.
- **Typing `route_source` as an enum.** Rejected: stored rows hold `classifier`, so narrowing it would break
  replay; the values are listed in the field's description instead.
- **`ask_user` as a deferred tool.** Rejected: resuming a parked run replays serialised history into code
  that may have changed; an ask that ends the run needs none of that.

## Amendment 2026-11-15 - the planner, clarifier and router are deleted

The v1 `planner`, `clarifier` and `router` agents, their prompts and `ai/policy/planner.py` are gone; the
names alias to `lead`. The plan rules live in `ai/capabilities/plan_rules.py`, and `PlanCheck` runs the
whole set in-run (graph rules and the dialed kind), so the commit's refusal is a last resort; the plan
shapes code makes are `operations/plan_shapes.py`. Skills are pinned per step from the Lead's plan at the
commit; the Lead is told the skills a step may load and, with no search engine, that the browser is the
web's road. The intake effects keep their names and ask nothing; a recipe turn has no intake question.

## Amendment 2026-11-15 - no per-step dollar ceiling

A step no longer runs under a dollar ceiling of its own. The ceiling was twice a token-size guess, and on a
cheap model the guess reads three to four times low: a maker step spent $0.167 against ~$0.16 and was held
from writing, so it published nothing while the task's budget had room. Limits now follow the leading agent
harnesses (pydantic-ai `UsageLimits`, the Claude Agent SDK, the OpenAI Agents SDK): counts per run, the
request ceiling per segment and the wall, stop a runaway loop; money has ONE budget, the task's, which the
person sets and the run guard reads at every safe point (`reached`), the frontier and a further attempt read
(`over`), and the budget gate parks on. Parallel seats each meet their own request ceiling and all read the
one task budget. The plan's `estimate` stays, as the figure a person is shown and the consent and wave
projection read (its `ceiling_usd` is the estimate's high end). Deleted: the step's envelope on the plan and
the step, the guard's step-spend read and its hold, the room for fixed-price tools that only raised that
ceiling, and the `share_spent` failure kind. A stored step or plan that carries `envelope`, and a logged
spend read that carries `step_spent`, still read: the field is ignored. No durable name changed;
`LOG_VERSION` stays 1 (the `spend` operation keeps its name and its result only lost a field).
