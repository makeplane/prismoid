# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Lead's parity book, offline: every problem-book row the answerer, the planner and triage decided,
read as the Lead must end it, and the rows only a live pass can judge (ADR-092).

A row's expected outcome is its owner-reviewed label: the rows of `datasets/outcomes.jsonl` whose source is
a retired problem book (the range, computer, computer-routing, navigate, machine and planner books) and the
bot conversations of `datasets/conversation.jsonl`. Each becomes a `ParityRow`: the `LeadFacts` intake
would compute for its turn, the Lead endings its label accepts, and the plan a plan ending takes.

A scripted model cannot judge a prompt, so offline this book proves two things, and its tally counts the
rows it checked as `consistent`, never as judged: a row's facts allow its ending, and the decision runner
reaches that ending with the plan passing the plan rules (`judge`). Every rule the books lean on names one
block of the Lead's prompt (`RULES`). A row whose
turn the offline runner cannot render is owed to the live pass, with the reason; a row no Lead run decides
(a dial's recipe, the API's refusal, a bot's conversation) is named apart. `python -m evals.cases.lead_parity`
prints the tally.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from evals.runners import decide
from evals.runners.live import earlier_turns
from evals.schema import Outcome, OutcomeRow, Surface, Unscored, load_rows
from prismoid.operations.turn.intake import Bypass, TurnRows, bypass_of
from prismoid.schemas.lead import LeadEndingKind, LeadFacts, TurnOrigin
from prismoid.schemas.plan import TaskMode

DATASETS: Final = Path(__file__).parents[1] / "datasets"
BOOKS: Final = "backend/evals/"  # a row whose source is a problem book under here


class Fate(StrEnum):
    """What the offline book can do with a row."""

    CONSISTENT = "consistent"  # rendered here: its facts allow its expected ending (no model judged it)
    OWED = "owed"  # only a live pass can render its turn
    NOT_LEAD = "not_lead"  # no Lead run decides it


@dataclass(frozen=True)
class ParityRow:
    """One book row as the Lead meets it; `expected` and `facts` are set on every consistent row."""

    row: OutcomeRow
    fate: Fate
    why: str = ""
    facts: LeadFacts | None = None
    endings: frozenset[LeadEndingKind] = frozenset()
    expected: LeadEndingKind | None = None
    plan: tuple[str, ...] = ()  # the agents of a plan ending, in order


@dataclass(frozen=True)
class Rule:
    """A rule the books lean on: the prompt block that says it, what it says in brief, the rows behind it.
    A rule with no rows is the design's (D5's endings and the answerer it replaces): no book row tests it yet."""

    block: str
    says: str
    rows: tuple[str, ...] = ()


def _rule(block: str, says: str, *rows: str) -> Rule:
    return Rule(f"lead.{block}", says, rows)


_REPLY, _LIVE, _PLAN = (
    "reply-whenever-a-message-can-hold-the-result",
    "the-live-world",
    "plan-when-the-outcome-needs-hands",
)
_WRITE, _ASK = "writing-a-plan", "ask-only-when-a-guess-would-ruin-the-work"

RULES: Final[dict[str, Rule]] = {
    "endings": _rule(
        "how-a-turn-ends", "end this turn in exactly one way", "range:dial-clarify-always-blocks"
    ),
    "reply.answer": _rule(
        _REPLY,
        "are replies, even when they take thought",
        "range:closedqa-plain-author",
        "range:boundary-monad-think-hard",
        "machine:talk-question",
    ),
    "reply.transform": _rule(
        _REPLY, "reply with the result itself", "range:rewrite-plain-email", "range:summarize-plain-attached"
    ),
    "reply.writing": _rule(
        _REPLY,
        "runs past about 1,500 words",
        "range:deliverable-plain-note",
        "range:length-hint-report-is-work",
        "range:length-hint-big-answer-is-work",
        "range:anchor-question-with-kind-in-ask-position",
        "machine:talk-planner-reply",
        "machine:memo-compare",
    ),
    "reply.paste": _rule(
        _REPLY, "a long paste is material", "range:boundary-code-fence", "range:boundary-pasted-logs"
    ),
    "reply.draft": _rule(_REPLY, "one fenced draft", "range:gensmall-plain-email"),
    "reply.reaction": _rule(
        _REPLY,
        "a reaction to your last answer",
        "range:thread-reaction-close",
        "range:thread-reaction-no-penrose-huh",
        "range:garble-thumbs-up",
    ),
    "reply.thread": _rule(_REPLY, "answered from the thread", "range:thread-question-about-the-thread"),
    "reply.site-question": _rule(_REPLY, "a question about a site is answered", "range:boundary-book-howto"),
    "live.clock": _rule(_LIVE, "the time and date are in the ambient facts", "range:chat-plain-time"),
    "live.fresh": _rule(
        _LIVE, "search only for what changes", "range:lookup-plain-weather", "range:lookup-plain-price"
    ),
    "live.page": _rule(_LIVE, "when the person named it", "range:lookup-plain-url", "machine:summarize-url"),
    "live.memory": _rule(_LIVE, "use `memory_search`", "range:abstention-email-sarah"),
    "live.data": _rule(
        _LIVE,
        "is data: it never changes",
        "range:adversarial-lookup-injection",
        "range:adversarial-budget-grab",
        "range:adversarial-urgent-ceo",
    ),
    "plan.file": _rule(
        _PLAN,
        "the result must be a file",
        "range:deliverable-plain-sheet",
        "range:deliverable-plain-two-slides",
        "range:thread-proper-document-named",
        "machine:spreadsheet",
    ),
    "plan.site": _rule(
        _PLAN, "done on a site or a computer", "range:boundary-book-a-ticket", "machine:browse-shop"
    ),
    "plan.detail": _rule(_PLAN, "a missing detail or a sign-in", "range:boundary-book-a-ticket"),
    "plan.service": _rule(
        _PLAN, "changes something in a service", "range:thread-reaction-with-object-is-not-a-reaction"
    ),
    "plan.breadth": _rule(
        _PLAN, "more sources than one turn can read", "range:research-plain-compare", "machine:research"
    ),
    "plan.compute": _rule(_PLAN, "come from a step that runs code"),
    "plan.routine": _rule(_PLAN, "recurring work is a routine"),
    "plan.dial": _rule(_PLAN, "the plan ends in that kind"),
    "plan.no-computer": _rule(_PLAN, "no step can act on a site"),
    "write.say": _rule(_WRITE, "write `say` first", "range:deliverable-plain-sheet"),
    "write.fewest": _rule(
        _WRITE,
        "plan the fewest steps that work",
        "planner:go-notes-memo-search",
        "planner:attached-sheet-search",
    ),
    "write.web-step": _rule(_WRITE, "for an agent that cannot search", "planner:sourced-sheet-machine"),
    "write.no-review": _rule(_WRITE, "never add a review or polish step"),
    "write.goal": _rule(_WRITE, "every step receives the person's request", "planner:sourced-sheet-search"),
    "write.revise": _rule(_WRITE, "revises that file in place"),
    "write.follow-up": _rule(
        _WRITE,
        "a follow-up on an earlier reply is a reply",
        "range:thread-make-it-shorter",
        "range:thread-edit-verb-reaches-planner",
    ),
    "ask.assume": _rule(
        _ASK,
        "prefer a stated assumption to a question",
        "range:anchor-question-with-kind-in-ask-position",
        "range:boundary-book-a-ticket",
    ),
    "ask.unsupplied": _rule(
        _ASK,
        "nobody supplied",
        "range:code-plain-refactor",
        "range:bio-plain-qc",
        "range:anchor-bare-kind-request-is-work",
        "range:contradiction-quick-deep",
        "range:boundary-imperative-churn",
        "machine:memo",
    ),
    "ask.no-referent": _rule(
        _ASK,
        "nothing to refer to",
        "range:ambiguity-the-thing",
        "range:ambiguity-make-it-better",
        "range:anchor-texting-modal-is-work",
    ),
    "ask.open-scope": _rule(_ASK, "a scope so open", "range:boundary-book", "range:boundary-quick-book"),
    "ask.connect": _rule(_ASK, 'ask with `kind="connect"`'),
    "ask.cannot": _rule(_ASK, "state your assumptions in one line and proceed"),
    "offer": _rule("offer-a-task-when-a-file-would-serve-better", "end with `offer_task`"),
    "language": _rule(
        "language",
        "language of their latest message",
        "range:garble-arabic-time",
        "range:thread-ack-non-english-in-fleet",
        "range:thread-ack-cjk-in-fleet",
    ),
    "honest.missing": _rule(
        "be-honest",
        "say what is missing and what would get it",
        "range:degraded-search-chain-empty",
        "range:degraded-all-providers-dead",
    ),
    "honest.no-futile-plan": _rule("be-honest", "never plan work whose only result"),
}

# A dial or a fact that code turns into a recipe before any Lead run (D5's dials matrix).
_RECIPES: Final = {
    "mode": "the fleet dial's turn 1 is the fleet recipe",
    "scale": "a size dial: the long-form recipe past one run's words, else a Lead turn the size rides",
    "deliverable": "a deliverable dial: intake sends it to the dial's plan, or to a Lead it binds",
    "direct_bot": "a direct Bot's turn 1 is one step on the Bot",
    "anchor": "the retired ADR-049 anchor dial: no turn reads it, the Lead decides the turn",
}


def outcome_ending(outcome: Outcome, facts: LeadFacts) -> LeadEndingKind:
    """The Lead ending that hands the person `outcome`; an unattended answer goes to its owner."""
    match outcome:
        case Outcome.REPLY | Outcome.LIGHT_WORK:
            return LeadEndingKind.DELIVER if facts.unattended else LeadEndingKind.REPLY
        case Outcome.PLAN | Outcome.PLAN_WITH_MACHINE:
            return LeadEndingKind.PLAN
        case Outcome.ASK:
            return LeadEndingKind.ASK
        case Outcome.PROPOSE:
            return LeadEndingKind.PROPOSE


def facts_of(row: OutcomeRow) -> LeadFacts:
    """The facts intake computes for the row's turn: its number, where it came from, the dials it set."""
    context = row.context
    routine = bool(context.facts.get("origin_routine"))
    clarify = context.dials.get("clarify", "auto")
    deliverable = context.dials.get("deliverable")
    return LeadFacts.model_validate(
        {
            "turn": max(context.turn, len(earlier_turns(row)) + 1),
            "origin": TurnOrigin.ROUTINE if routine else TurnOrigin.WEB,
            "unattended": routine,
            "clarify": clarify,
            "deliverable": deliverable,
        }
    )


def turn_rows(row: OutcomeRow) -> TurnRows:
    """The rows intake would read for the row's turn: its number, mode, dials and what the task holds."""
    context, facts = row.context, row.context.facts
    turn = max(context.turn, len(earlier_turns(row)) + 1)
    return TurnRows(
        turn=turn,
        mode=TaskMode(context.dials.get("mode", TaskMode.STANDARD.value)),
        clarify=context.dials.get("clarify", "auto"),
        from_routine=bool(facts.get("origin_routine")),
        from_channel=False,
        from_api=False,
        direct_bot="a-bot" if facts.get("direct_bot") else None,
        bot_pins=bool(facts.get("has_bots")),
        connectors=bool(context.connectors) or bool(facts.get("has_connectors")),
        dial=context.dials.get("deliverable"),
        carried_dial=context.dials.get("deliverable") if turn == 1 else None,
        deliverable_format=None,
        scale=context.dials.get("scale"),
        target_pages=None,
        inputs=(),
        budget_usd=None,
        spent_usd=Decimal(0),
        asked=False,
    )


def route(row: OutcomeRow) -> Bypass | None:
    """Where intake sends the row's turn: the recipe that answers it, or None for the Lead."""
    return bypass_of(turn_rows(row))


def routed_as_classified(parity: ParityRow) -> bool:
    """A Lead row reaches the Lead and a fleet or direct Bot's first turn its recipe; a deliverable or size dial
    may reach either, and a row that is no task turn (a conversation, a refusal) routes nowhere."""
    reached = route(parity.row)
    if parity.fate is not Fate.NOT_LEAD:
        return reached is None
    either = (_RECIPES["deliverable"], _RECIPES["scale"], _RECIPES["anchor"])
    if parity.why in either or parity.why not in _RECIPES.values():
        return True
    return reached is not None


def _unrendered(row: OutcomeRow) -> str | None:
    """Why the offline runner cannot render the row's turn as the Lead would read it, or None."""
    context = row.context
    gaps = (
        (bool(context.connectors), "connected services ride the ambient facts and mount read tools"),
        (context.prior_host is not None, "the thread's last work drove a site"),
        (context.transact, "the transact dial"),
        (context.deployment is not None, f"the deployment: {context.deployment}"),
        (context.deployment_reach is not None, f"a deployment whose reach is {context.deployment_reach}"),
        (bool(context.inputs), "attached files are not rendered offline"),
        (context.turn > len(earlier_turns(row)) + 1, "the thread it answers is not in the row"),
    )
    return next((why for blocked, why in gaps if blocked), None)


def _recipe(row: OutcomeRow) -> str:
    """The recipe that decides an unscored row before any Lead run."""
    context = row.context
    said = sorted(({*context.dials, *context.facts} - {"mode"}) & _RECIPES.keys())
    if said:
        return _RECIPES[said[0]]
    return _RECIPES["mode"] if "mode" in context.dials else "code decides it before any model runs"


def _plan_shape(outcome: Outcome) -> tuple[str, ...]:
    """The plan an outcome row's plan ending takes: the dataset says only whether a Machine runs."""
    return ("operator",) if outcome is Outcome.PLAN_WITH_MACHINE else ("writer",)


def parity_row(row: OutcomeRow) -> ParityRow:
    """One labelled row as the Lead meets it."""
    if row.context.surface is Surface.CONVERSATION:
        return ParityRow(row, Fate.NOT_LEAD, "a bot's conversation: the conversation suite judges it")
    if row.expected_outcome is Unscored.REJECT:
        return ParityRow(row, Fate.NOT_LEAD, "the API refuses the ask before any turn")
    facts = facts_of(row)
    if row.expected_outcome is Unscored.MECHANISM:
        only = frozenset({LeadEndingKind.ASK})
        if facts.allowed_endings() != only:
            return ParityRow(row, Fate.NOT_LEAD, _recipe(row))
        return ParityRow(row, Fate.CONSISTENT, facts=facts, endings=only, expected=LeadEndingKind.ASK)
    if route(row) is not None:
        return ParityRow(row, Fate.NOT_LEAD, _recipe(row))
    expected = outcome_ending(row.expected_outcome, facts)
    endings = frozenset({expected, *(outcome_ending(o, facts) for o in row.allowed_outcomes)})
    shape = _plan_shape(row.expected_outcome) if expected is LeadEndingKind.PLAN else ()
    if (why := _unrendered(row)) is not None:
        return ParityRow(row, Fate.OWED, why, facts, endings, expected, shape)
    return ParityRow(row, Fate.CONSISTENT, facts=facts, endings=endings, expected=expected, plan=shape)


def book_rows() -> list[OutcomeRow]:
    """The labelled rows the problem books held, as `outcomes.jsonl` keeps them."""
    return [row for row in load_rows(DATASETS / "outcomes.jsonl") if (row.source or "").startswith(BOOKS)]


def population() -> list[ParityRow]:
    """Every problem-book row and every bot conversation, as the Lead meets it."""
    return [parity_row(row) for row in (*book_rows(), *load_rows(DATASETS / "conversation.jsonl"))]


# ── the offline judge ────────────────────────────────────────────────────────────────────────


_OUTCOME_ENDING: Final = {
    Outcome.REPLY: LeadEndingKind.REPLY,
    Outcome.LIGHT_WORK: LeadEndingKind.REPLY,
    Outcome.PLAN: LeadEndingKind.PLAN,
    Outcome.PLAN_WITH_MACHINE: LeadEndingKind.PLAN,
    Outcome.ASK: LeadEndingKind.ASK,
    Outcome.PROPOSE: LeadEndingKind.PROPOSE,
}
SAY: Final = "Here is what happens next."

# What decide's trace can read: the unattended endings are not among them yet.
JUDGED: Final = frozenset(
    {LeadEndingKind.REPLY, LeadEndingKind.PLAN, LeadEndingKind.ASK, LeadEndingKind.PROPOSE}
)


def plan_call(agents: tuple[str, ...]) -> dict[str, Any]:
    """`submit_plan`'s arguments for a chain of `agents`; the last one publishes."""
    steps = [
        {
            "label": f"s{n}",
            "agent": agent,
            "title": f"Step {n}",
            "goal": "Do this part of the request in full.",
            "depends_on": [f"s{n - 1}"] if n > 1 else [],
            "publishes": n == len(agents),
        }
        for n, agent in enumerate(agents, start=1)
    ]
    action = agents[-1] == "operator"
    return {"say": SAY, "plan": {"deliverable": "action" if action else "files", "steps": steps}}


def _call(ending: LeadEndingKind, plan: tuple[str, ...]) -> ModelResponse:
    """The scripted model's one response: the ending a row expects."""
    calls: dict[LeadEndingKind, tuple[str, dict[str, Any]]] = {
        LeadEndingKind.ASK: (
            "ask_person",
            {"context": "One thing decides this.", "questions": [{"prompt": "Which one?", "type": "text"}]},
        ),
        LeadEndingKind.PROPOSE: (
            "offer_task",
            {"answer": "The short answer.", "goal": "The full piece, as a file.", "deliverable": "document"},
        ),
    }
    if ending is LeadEndingKind.REPLY:
        return ModelResponse(parts=[TextPart("The answer.")])
    if ending is LeadEndingKind.PLAN:
        return ModelResponse(parts=[ToolCallPart("submit_plan", plan_call(plan))])
    name, args = calls[ending]
    return ModelResponse(parts=[ToolCallPart(name, args)])


async def judge(parity: ParityRow, seat: decide.Seat, ending: LeadEndingKind | None = None) -> str:
    """Run the Lead on the row through the decision runner, the model scripted to `ending` (the row's own
    by default); empty when the run reaches that ending (a plan unrefused), else what happened: "refused" when
    the Lead's check sent a forbidden ending back, even when the run then fell back to an ending it allows."""
    said, facts = ending or parity.expected, parity.facts
    if said is None or facts is None or said not in JUDGED:
        raise ValueError(f"row {parity.row.id}: the decision runner cannot judge {said}")

    def model(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return _call(said, parity.plan)

    runner = decide.DecideRunner(decide.LEAD, seat, FunctionModel(model), facts=lambda _row: facts)
    try:
        trace = await runner(parity.row)
    except Exception as refused:  # fail-closed: a refused ending is the finding
        return f"refused: {type(refused).__name__}: {refused}"
    if not isinstance(trace.outcome, Outcome):
        return f"no outcome: {trace.outcome}"
    got = _OUTCOME_ENDING[trace.outcome]
    if said not in facts.allowed_endings() and trace.calls_before_delivery > 1:  # sent back, then a fallback
        return f"refused: sent back {trace.calls_before_delivery - 1} times, then ended {got}"
    if got is not said:
        return f"ended {got}, not {said}"
    if said is LeadEndingKind.PLAN and trace.final_text != SAY:
        return f"plan replaced by the floor: {trace.final_text}"
    return ""


def allowed(parity: ParityRow) -> bool:
    """The row's facts allow the ending its label expects."""
    return parity.facts is not None and parity.expected in parity.facts.allowed_endings()


def tally(rows: list[ParityRow]) -> dict[str, Counter[str]]:
    """Rows per fate, and per reason for the owed and the not-Lead ones."""
    out: dict[str, Counter[str]] = {fate: Counter() for fate in Fate}
    for row in rows:
        out[row.fate][row.why or row.row.id.split(":")[0]] += 1
    return out


def main() -> None:
    rows = population()
    for fate, counts in tally(rows).items():
        print(f"{fate}: {sum(counts.values())}")
        for why, n in counts.most_common():
            print(f"    {n:4}  {why}")
    astray = [row.row.id for row in rows if not routed_as_classified(row)]
    print(
        f"routed as classified: {len(rows) - len(astray)} of {len(rows)}; astray: {', '.join(astray) or 'none'}"
    )
    untested = sorted(key for key, rule in RULES.items() if not rule.rows)
    print(f"rules no book row leans on yet ({len(untested)}): {', '.join(untested)}")


if __name__ == "__main__":
    main()
