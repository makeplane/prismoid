# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Lead's check, inside its run: an ending the turn's facts forbid goes back (`LeadEndings`), and a plan
is held to the rules and priced (`PlanCheck`): its seats, grants and recipes here, and every rule the commit
holds it to (`rules/plan.py`), so a plan the commit would refuse goes back in-run.

The rules are pure functions over values: the turn's `LeadFacts` and the `PlanFacts` operations computed,
whose `seats` are the roster and the author's bots. A broken plan goes back to the model with the rule it
broke, twice; the next broken one is replaced by the floor, one step that does the whole request and keeps the
ending the plan was for (an action stays an action, an answer an answer, files files), saying plainly why.
A plan's `say` that names a built-in agent as a role ("the maker") goes back once, then a neutral line stands.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import KW_ONLY, dataclass, field, replace
from itertools import pairwise
from typing import Any, Final

import logfire
from pydantic import ValidationError
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.output import OutputContext

from prismoid.ai import config
from prismoid.ai.capabilities.output_checks import may_send_back
from prismoid.ai.context.turn import named, said
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.run_cost import SEAT_SIZE, class_line, step_size
from prismoid.ai.rules.plan import breach
from prismoid.ai.skills import deliverables
from prismoid.ai.skills.deliverables import PROSE_AGENT, kind_agent
from prismoid.ai.tools import catalog
from prismoid.schemas.lead import (
    AskUser,
    KeepPlan,
    LeadEndingKind,
    LeadFacts,
    PlanRevision,
    ProposeTask,
    SubmitPlan,
)
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import (
    DeliverableKind,
    PlanEstimate,
    PlanFacts,
    PlanProposal,
    ProposedStep,
    RecipeKind,
    Seat,
    StepCost,
)
from prismoid.schemas.sizing import DOCUMENT_SIZES, SizeBucket


@dataclass
class LeadEndings(AbstractCapability[TaskDeps]):
    """Sends back, inside the run, an ending the turn's facts forbid and an ask whose batch cannot open as
    typed, while it may (`may_send_back`); past that, the floor where a plan is allowed, else the ending
    stands. A reply that is an output tool's JSON written as text goes back too, then stands as that ending;
    a batch asking what it could assume goes back, then stands. Inert without `TaskDeps.lead_facts`."""

    id: str | None = "lead_endings"
    _sent: int = field(default=0, init=False, repr=False)  # the endings this run sent back

    async def for_run(self, ctx: RunContext[TaskDeps]) -> LeadEndings:
        return replace(self)

    async def after_output_process(
        self, ctx: RunContext[TaskDeps], *, output_context: OutputContext, output: Any
    ) -> Any:
        facts = ctx.deps.lead_facts
        if facts is None or ctx.partial_output:
            return output
        allowed = facts.allowed_endings()
        if (
            isinstance(output, str)
            and (typed := written_ending(output)) is not None
            and typed.ending in allowed
        ):
            if may_send_back(ctx, self._sent):
                self._sent += 1
                raise ModelRetry(WRITTEN_AS_TEXT.format(tool=said(typed.ending)))
            logfire.warn("the Lead wrote its {kind} as text; it stands as that ending", kind=typed.ending)
            if isinstance(typed, SubmitPlan):
                return _held(typed, facts, ctx.deps.plan_facts or PlanFacts())
            return typed
        kind = LeadEndingKind.REPLY if isinstance(output, str) else output.ending
        if kind in allowed:
            why = misshapen(output) if isinstance(output, AskUser) else None
        else:
            why = f"This turn cannot end with {said(kind)}. End it with {named(allowed)}."
        if why is None:
            # Asking what it could assume goes back while it may; past that the ask stands as asked.
            soft = assumable(output) if isinstance(output, AskUser) and kind in allowed else None
            if soft is not None and may_send_back(ctx, self._sent):
                self._sent += 1
                raise ModelRetry(soft)
            return output
        if may_send_back(ctx, self._sent):
            self._sent += 1
            raise ModelRetry(why)
        if LeadEndingKind.PLAN in allowed and kind is not LeadEndingKind.PLAN:
            logfire.warn("the Lead's ending fell back: {why}", why=why)
            return floor(facts, ctx.deps.plan_facts or PlanFacts(), ENDING_WHY, say=ENDING_SAY)
        return output


WRITTEN_AS_TEXT: Final = (
    "You wrote {tool}'s arguments as text, which the person would read as your answer. Call {tool} with "
    "them instead; text is only ever the reply itself."
)
# The endings a model may write as JSON text in place of calling their tool: each tried in turn.
_TYPED: Final = (SubmitPlan, AskUser, ProposeTask)


def _fenced(text: str) -> list[str]:
    """The bodies of the text's ``` blocks (an optional `json` tag dropped) that read as one JSON object."""
    bodies = (part.lstrip().removeprefix("json").strip() for part in text.split("```")[1::2])
    return [body for body in bodies if body.startswith("{") and body.endswith("}")]


def written_ending(text: str) -> SubmitPlan | AskUser | ProposeTask | None:
    """The output tool call a reply holds as JSON (a fenced block, or the whole text), when its keys are
    only its tool's and it validates in full; None for any other text, a sample with keys of its own included."""
    stripped = text.strip()
    found = _fenced(text)
    candidates = found or ([stripped] if stripped.startswith("{") else [])
    for raw in candidates:
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(value, dict):
            continue
        for model in _TYPED:
            if not value.keys() <= model.model_fields.keys():
                continue
            try:
                return model.model_validate(value)
            except ValidationError:
                continue
    return None


def _held(written: SubmitPlan, facts: LeadFacts, known: PlanFacts) -> SubmitPlan:
    """A plan the model wrote as text, held to the plan check as a called one is; the floor when it breaks."""
    try:
        return written.model_copy(update={"plan": checked(written.plan, facts, known)})
    except PlanRefused as broke:
        logfire.warn("the Lead's written plan fell back: {why}", why=str(broke))
        return floor(facts, known, REFUSED_WHY, intended=written.plan)


def misshapen(ask: AskUser) -> str | None:
    """Why an ask's batch cannot open as typed (the intake's own shape rule), or None."""
    for n, question in enumerate(ask.questions, start=1):
        if why := question.misshapen():
            return f"Question {n} {why}: give a choice two or more options, and free text none."
    return None


def assumable(ask: AskUser) -> str | None:
    """Why a batch of two or more asks what it could assume, or None: a question carrying a default is one a
    wrong guess would not ruin. A lone question stands, its default what a timeout proceeds with."""
    assumed = [n for n, q in enumerate(ask.questions, start=1) if q.default.strip()]
    if len(ask.questions) < 2 or not assumed:
        return None
    return DEFAULTED.format(listed=", ".join(str(n) for n in assumed))


DEFAULTED: Final = (
    "Question(s) {listed} carry a safe default, so a guess would not ruin the work: drop them from the batch "
    "and state each default as an assumption in `context`. Ask only what has no default, or, if nothing is "
    "left, go on without asking."
)


# A broken plan goes back to the model this many times; the next broken one is replaced by the floor.
PLAN_REFUSALS: Final = 2

# Why a plan fell back, in the person's words: the `task.planned` fallback field, and the thread's line.
REFUSED_WHY: Final = "the plan for this did not pass its checks"
FLOOR_SAY: Final = (
    "My plan for this did not pass its checks, so one step will take on the whole request directly."
)
ENDING_WHY: Final = "the first answer did not fit what this turn was set to"
ENDING_SAY: Final = "My first answer did not fit what this turn was set to, so one step will take on the whole request directly."
REVISION_KEPT: Final = "the new plan did not pass its checks; the steps not started follow your steer"
FLOOR_GOAL: Final = "Do what the person asked in their request, in full, and deliver the result."
# The floor's seat when no step of the plan can deliver and no kind is dialed: an answer, never a file.
_ANSWER_AGENT: Final = "researcher"
_OUTCOMES: Final = (DeliverableKind.ACTION, DeliverableKind.CHANGE)

# Each recipe's agent, and the page range a long-form document spans: past what one run writes, at most
# the document size table's ceiling.
_RECIPE_AGENT: Final = {RecipeKind.SCATTER: "researcher", RecipeKind.LONGFORM: PROSE_AGENT}
LONGFORM_PAGES: Final = (DOCUMENT_SIZES[SizeBucket.STANDARD].cap + 1, DOCUMENT_SIZES[SizeBucket.BOOK].cap)

# The tools that open a site: the page reader and the browser.
_OPENS_SITES: Final = frozenset(
    (catalog.model_name("web.fetch"), *(tool.name for tool in catalog.family("browser")))
)


# A `say` names an agent the way the Lead does when it says who works: an article before a built-in's name
# ("the operator", "a researcher"); so "writer" or "operator" in their ordinary sense pass.
_ROLE_ARTICLES: Final = frozenset({"the", "a", "an", "our"})
SAY_SENT_BACK: Final = (
    "Your `say` names {who}, an internal agent the person never sees. Say what will happen in their "
    "terms, never which agent does it."
)
PLAN_SAY: Final = "Here is the plan; the steps below will carry out your request."
REVISION_SAY: Final = "The steps not started now follow your steer."


def agent_named(say: str, seats: Sequence[Seat]) -> str | None:
    """The built-in seat `say` names as a role (an article, then its name as a whole word, any case), or None;
    the author's bots are theirs to name."""
    names = {seat.name.lower() for seat in seats if not seat.bot}
    words = "".join(c if c.isalnum() or c in "_-" else " " for c in say.lower()).split()
    return next((w for a, w in pairwise(words) if a in _ROLE_ARTICLES and w in names), None)


class PlanRefused(Exception):
    """A rule the plan broke, said to the model as the reason to plan again."""


@dataclass
class PlanCheck(AbstractCapability[TaskDeps]):
    """Holds a Lead's plan or revision to the rules inside its run and sets code's fields on it: a broken one goes
    back with the rule it broke, twice, then the floor runs (a revision keeps the plan); inert without `TaskDeps.lead_facts`."""

    id: str | None = "plan_check"
    _: KW_ONLY
    _refused: int = field(default=0, init=False)
    _said: int = field(default=0, init=False)  # the says this run sent back for naming an agent

    async def for_run(self, ctx: RunContext[TaskDeps]) -> PlanCheck:
        return replace(self)

    async def after_output_validate(
        self, ctx: RunContext[TaskDeps], *, output_context: OutputContext, output: Any
    ) -> Any:
        facts = ctx.deps.lead_facts
        if facts is None or ctx.partial_output or not isinstance(output, SubmitPlan | PlanRevision):
            return output
        known = ctx.deps.plan_facts or PlanFacts()
        if not output.plan.steps:  # a revision that drops the rest: only its say to check
            return self._plainly(ctx, output, known)
        try:
            plan = checked(output.plan, facts, known)
        except PlanRefused as broke:
            if self._refused >= PLAN_REFUSALS or ctx.retry >= ctx.max_retries:
                logfire.warn("the Lead's plan fell back: {why}", why=str(broke))
                if isinstance(output, PlanRevision):
                    return KeepPlan(reason=REVISION_KEPT)
                return floor(facts, known, REFUSED_WHY, intended=output.plan)
            self._refused += 1
            raise ModelRetry(f"{broke} Fix the plan and submit it again.") from broke
        return self._plainly(ctx, output.model_copy(update={"plan": plan}), known)

    def _plainly(
        self, ctx: RunContext[TaskDeps], output: SubmitPlan | PlanRevision, known: PlanFacts
    ) -> SubmitPlan | PlanRevision:
        """`output` when its say names no agent; else sent back once, then said neutrally."""
        who = agent_named(output.say, known.seats)
        if who is None:
            return output
        if not self._said and may_send_back(ctx, self._said):
            self._said += 1
            raise ModelRetry(SAY_SENT_BACK.format(who=f"the {who}"))
        logfire.warn("the Lead's say named an agent: {who}", who=who)
        say = REVISION_SAY if isinstance(output, PlanRevision) else PLAN_SAY
        return output.model_copy(update={"say": say})


def checked(plan: PlanProposal, facts: LeadFacts, known: PlanFacts) -> PlanProposal:
    """The plan held to the seats, the grants, the recipes and every rule the commit holds it to (the dialed
    kind included), its request and its estimate set; raises `PlanRefused` naming the first rule it breaks."""
    if plan.deliverable is None:
        raise PlanRefused("Set `deliverable`: answer, change, action or files.")
    seats = {seat.name: seat for seat in known.seats}
    if unknown := sorted({s.agent for s in plan.steps} - seats.keys()):
        raise PlanRefused(f"No such agent: {', '.join(unknown)}. The roster is: {', '.join(sorted(seats))}.")
    if facts.direct_bot is not None and all(s.agent != facts.direct_bot for s in plan.steps):
        raise PlanRefused(f"This is {facts.direct_bot}'s thread: its work step runs as {facts.direct_bot}.")
    for step in plan.steps:
        _granted(step, seats[step.agent], known)
        _recipe_fits(step, plan, facts)
    dialed = ArtifactType(facts.deliverable) if facts.deliverable is not None else None
    if why := breach(plan, known.seats, dialed):
        raise PlanRefused(f"{why[0].upper()}{why[1:]}.")
    return sized(plan.model_copy(update={"request": known.request, "fallback": ""}), facts, known)


def _granted(step: ProposedStep, seat: Seat, known: PlanFacts) -> None:
    """A step's skills exist for the author and fit its seat's tools, and its hosts are on a seat that opens sites."""
    for skill in step.skills:
        needs = known.skills.get(skill)
        if needs is None:
            index = ", ".join(sorted(known.skills)) or "none"
            raise PlanRefused(f"Step {step.label}: no skill named {skill!r}. The skills are: {index}.")
        if not set(needs) <= set(seat.tools):
            raise PlanRefused(
                f"Step {step.label}: {seat.name} cannot use {skill!r}, which needs {', '.join(needs)}. "
                "Put it on a step whose agent holds those tools, or drop it."
            )
    if step.hosts and not _OPENS_SITES.intersection(seat.tools):
        raise PlanRefused(
            f"Step {step.label}: {seat.name} cannot open a site. Put the hosts on a step that reads the web."
        )


def _recipe_fits(step: ProposedStep, plan: PlanProposal, facts: LeadFacts) -> None:
    """A recipe is on the agent it expands, with its own field set to a size worth the recipe."""
    recipe = step.recipe
    if recipe is None:
        return
    if step.agent != (agent := _RECIPE_AGENT[recipe.kind]):
        raise PlanRefused(f"Step {step.label}: the {recipe.kind} recipe runs on a {agent} step.")
    if recipe.kind is RecipeKind.SCATTER:
        least = config.current().worker.swarm_min_count
        if recipe.target_count is None or recipe.target_pages is not None:
            raise PlanRefused(
                f"Step {step.label}: scatter sets `target_count`, how many items to find, and nothing else."
            )
        if recipe.target_count < least:
            raise PlanRefused(
                f"Step {step.label}: one researcher covers {recipe.target_count} items; "
                f"scatter is for {least} or more. Drop the recipe."
            )
        return
    low, high = LONGFORM_PAGES
    if recipe.target_pages is None or recipe.target_count is not None:
        raise PlanRefused(
            f"Step {step.label}: longform sets `target_pages`, how many pages, and nothing else."
        )
    if not low <= recipe.target_pages <= high:
        raise PlanRefused(
            f"Step {step.label}: longform is for {low} to {high} pages; one writer step writes fewer."
        )
    if plan.deliverable is not DeliverableKind.FILES or facts.deliverable not in (
        None,
        ArtifactType.DOCUMENT,
    ):
        raise PlanRefused(f"Step {step.label}: longform writes a document; the plan delivers files.")


def plan_estimate(plan: PlanProposal, facts: LeadFacts, known: PlanFacts) -> PlanEstimate:
    """What the plan is projected to cost: each step's run sized by its agent and recipe, priced at its class's
    head row as the meter bills it; a scatter step's seats are their own line, under the step's label."""
    seats = {seat.name: seat for seat in known.seats}
    costs: list[StepCost] = []
    for step in plan.steps:
        seat = seats[step.agent]
        cls = step_class(step, seat, facts)
        costs.append(class_line(step.label, cls, step_size(step), known))
        if step.recipe is not None and step.recipe.kind is RecipeKind.SCATTER:
            width = config.current().worker.swarm_max_width
            seat_line = SEAT_SIZE.times(width)
            costs.append(class_line(step.label, seat.seat_class, seat_line, known, seats=True))
    return PlanEstimate.of(tuple(costs), facts.budget_left_usd)


def step_class(step: ProposedStep, seat: Seat, facts: LeadFacts) -> ModelClass:
    """The model class a step runs on: a format maker's from the format skill its file takes, else its agent's."""
    if not seat.makes_formats:
        return seat.model_class
    named_format = next(
        (made for skill in step.skills if (made := deliverables.of_skill(skill)) is not None), None
    )
    kind = step.produces or facts.deliverable
    made = named_format or (
        deliverables.format_for(kind, facts.deliverable_format) if kind is not None else None
    )
    return made.model_class if made is not None else seat.model_class


def sized(plan: PlanProposal, facts: LeadFacts, known: PlanFacts) -> PlanProposal:
    """`plan` with its estimate, whatever a model sent in its place: the cost a person is shown, never a stop."""
    return plan.model_copy(update={"estimate": plan_estimate(plan, facts, known)})


def floor(
    facts: LeadFacts,
    known: PlanFacts,
    why: str,
    *,
    intended: PlanProposal | None = None,
    say: str = FLOOR_SAY,
) -> SubmitPlan:
    """The plan that runs in place of the Lead's (`fallback`), priced, the person told `say`."""
    plan = fallback(
        intended,
        known.seats,
        dialed=facts.deliverable,
        doc_format=facts.deliverable_format,
        request=known.request,
        why=why,
    )
    return SubmitPlan(say=say, plan=sized(plan, facts, known))


def fallback(
    intended: PlanProposal | None,
    seats: Sequence[Seat],
    *,
    dialed: ArtifactType | None,
    doc_format: str | None,
    request: str,
    why: str,
) -> PlanProposal:
    """One step that does the whole request on a built-in, keeping the ending `intended` was for, the dialed
    kind first, else an answer; never a bot. `fallback` is `why`, in the person's words."""
    builtins = {seat.name: seat for seat in seats if not seat.bot}
    kept = _deliverer(intended, builtins)
    if kept is not None and dialed is not None and not _makes(builtins[kept.agent], dialed):
        kept = None
    if kept is not None and intended is not None:
        agent, kind = kept.agent, dialed or kept.produces
        deliverable = intended.deliverable or _delivers(builtins[agent])
    elif dialed is not None:
        agent, kind = kind_agent(dialed, list(builtins.values()), doc_format), dialed
        deliverable = DeliverableKind.FILES
    else:
        agent, kind, deliverable = _ANSWER_AGENT, None, DeliverableKind.ANSWER
    seat = builtins[agent]
    if seat.makes_formats and kind is None:
        kind = ArtifactType.DOCUMENT
    opens = kept is not None and bool(_OPENS_SITES.intersection(seat.tools))
    step = ProposedStep(
        label="s1",
        agent=agent,
        title=" ".join(request.split())[:100] or "The request",
        goal=FLOOR_GOAL,
        publishes=not seat.delivers_outcome,
        filename=kept.filename if kept is not None else "",
        produces=kind if seat.makes_formats else None,
        hosts=kept.hosts if kept is not None and opens else [],
        browser=kept.browser if kept is not None and opens else False,
    )
    shape = intended or PlanProposal()
    return PlanProposal(
        steps=[step],
        deliverable=deliverable,
        answer_shape=shape.answer_shape,
        tone=shape.tone,
        audience=shape.audience,
        request=request,
        fallback=why[:300],
    )


def _deliverer(intended: PlanProposal | None, builtins: Mapping[str, Seat]) -> ProposedStep | None:
    """The step that hands over what `intended` delivers (the act for an action, else the last step nothing
    reads, a publishing one first), when a built-in that can deliver holds it."""
    if intended is None or not intended.steps:
        return None
    read = {label for step in intended.steps for label in step.depends_on}
    acts = [s for s in intended.steps if s.agent in builtins and builtins[s.agent].delivers_outcome]
    ends = [s for s in intended.steps if s.label not in read] or intended.steps
    pool = acts if intended.deliverable in _OUTCOMES and acts else ends
    step = next((s for s in reversed(pool) if s.publishes), pool[-1])
    seat = builtins.get(step.agent)
    return step if seat is not None and (seat.publishes or seat.delivers_outcome) else None


def _makes(seat: Seat, kind: ArtifactType) -> bool:
    """Whether a step on `seat` publishes a file of `kind`."""
    return seat.publishes and not seat.delivers_outcome and kind in seat.kinds


def _delivers(seat: Seat) -> DeliverableKind:
    """What a lone step on `seat` hands the person: an act, an answer, or files."""
    if seat.delivers_outcome:
        return DeliverableKind.ACTION
    return DeliverableKind.ANSWER if seat.name == _ANSWER_AGENT else DeliverableKind.FILES
