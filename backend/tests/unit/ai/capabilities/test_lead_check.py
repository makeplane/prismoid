# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Lead's plan, checked inside its run: each rule's broken row goes back to the model and the fixed one
passes - the commit's own graph rules included, so a plan the commit would refuse never reaches it - a third
broken plan becomes the floor that says so, and the estimate is the catalog's prices over the steps' runs, as
the meter bills them, and sizes each step's envelope. The rules' table (`rules.plan.validate`, which the
commit also holds every plan to) is pinned below, each message byte-exact."""

from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any, cast

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from prismoid.ai import config
from prismoid.ai.agents.base import build, get
from prismoid.ai.agents.bot_compile import seat_table, seatable
from prismoid.ai.capabilities import lead_check
from prismoid.ai.deps import TaskDeps
from prismoid.ai.frames.layout import input_paths
from prismoid.ai.gateway import run_cost
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.providers import ClassSettings
from prismoid.ai.rules.plan import (
    DELIVERS_OUTCOME,
    InvalidPlan,
    breach,
    depth_clause,
    input_clause,
    validate,
)
from prismoid.schemas.bots import BotManifest, ResolvedBot
from prismoid.schemas.contract import SkillPin
from prismoid.schemas.ids import ActorId, BotName, SkillName
from prismoid.schemas.inputs import TaskInput
from prismoid.schemas.lead import AskUser, LeadFacts, SubmitPlan, TurnOrigin
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import (
    DeliverableKind,
    PlanFacts,
    PlanProposal,
    ProposedQuestion,
    ProposedStep,
    QuestionType,
    RecipeKind,
    Seat,
    StepRecipe,
)
from prismoid.schemas.usage import Unit
from tests.support.first_request import fake_row

WEB = LeadFacts(turn=1, origin=TurnOrigin.WEB)
KNOWN = PlanFacts(
    request="Compare the three CRMs on example.com and write it up.",
    skills={"compare": (), "shell-report": ("shell",)},
)


def step(**overrides: Any) -> dict[str, Any]:
    """A valid writer step, field by field overridable."""
    return {
        "label": "s1",
        "agent": "writer",
        "title": "Write it",
        "goal": "Write the comparison.",
        "publishes": True,
    } | overrides


def plan(*steps: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """`submit_plan`'s arguments: a files plan over `steps` (one valid writer step by default)."""
    return {
        "say": "I'll write it up.",
        "plan": {"deliverable": "files", "steps": list(steps) or [step()]} | overrides,
    }


def a_bot(name: str = "reviewer", tools: tuple[str, ...] = ("frame.read_file",)) -> ResolvedBot:
    """A published bot in the author's roster."""
    manifest = BotManifest(
        name=BotName(name),
        description="Reviews contracts clause by clause. Load when: a contract needs a read. Not for: drafting.",
        capability=ModelClass.FAST_EXECUTOR,
        produces=ArtifactType.DOCUMENT,
        tools=tools,
    )
    return ResolvedBot(
        name=BotName(name),
        version=1,
        manifest=manifest,
        instructions="Read it.",
        owner_actor_id=ActorId(uuid.uuid4()),
    )


def seated(known: PlanFacts, bots: tuple[ResolvedBot, ...] = ()) -> PlanFacts:
    """`known` with the roster's seats and the author's `bots`, as operations hands them to a Lead run."""
    return known.model_copy(update={"seats": seat_table(seatable(bots))})


class Scripted:
    """A model that submits each plan in turn and keeps what it was told back."""

    def __init__(self, *plans: dict[str, Any]) -> None:
        self.plans = list(plans)
        self.told: list[str] = []

    def __call__(self, messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        if isinstance(last, ModelRequest):
            self.told += [
                str(p.content) for p in last.parts if isinstance(p, ToolReturnPart) and p.outcome == "retried"
            ]
        return ModelResponse(parts=[ToolCallPart("submit_plan", self.plans.pop(0))])


async def run(scripted: Scripted, *, facts: LeadFacts | None = WEB, **deps: Any) -> SubmitPlan:
    """One Lead run through `build()`, ending with the plan the check let through."""
    known = seated(deps.pop("known", KNOWN), deps.get("bots", ()))
    task = TaskDeps(lead_facts=facts, plan_facts=known, **deps)
    runner = build("lead", task, [fake_row(ModelClass.FRONTIER_EXECUTOR)])
    with runner.agent.override(model=FunctionModel(scripted)):
        result = await runner.run("go")
    return cast(SubmitPlan, result.output)


# ── each rule's row fails, then the fixed plan passes ───────────────────────────────────────────

BROKEN: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {
    "roster": (plan(step(agent="analyst")), {}, "No such agent: analyst"),
    "retired name": (plan(step(agent="coder")), {}, "No such agent: coder"),
    "deliverable": (plan(deliverable=None), {}, "Set `deliverable`"),
    "unknown skill": (plan(step(skills=["poetry"])), {}, "no skill named 'poetry'"),
    "skill beyond grants": (plan(step(skills=["shell-report"])), {}, "cannot use 'shell-report'"),
    "bot not in the roster": (plan(step(agent="reviewer")), {}, "No such agent: reviewer"),
    "direct thread": (
        plan(),
        {"facts": WEB.model_copy(update={"direct_bot": "reviewer"}), "bots": (a_bot(),)},
        "its work step runs as reviewer",
    ),
    "hosts off the web": (
        plan(step(agent="reviewer", hosts=["example.com"])),
        {"bots": (a_bot(),)},
        "cannot open a site",
    ),
    "scatter off a researcher": (
        plan(step(recipe={"kind": "scatter", "target_count": 60})),
        {},
        "runs on a researcher step",
    ),
    "scatter too small": (
        plan(
            step(agent="researcher", publishes=False, recipe={"kind": "scatter", "target_count": 3}),
            step(label="s2", depends_on=["s1"]),
        ),
        {},
        "one researcher covers 3 items",
    ),
    "scatter without a count": (
        plan(step(agent="researcher", publishes=False, recipe={"kind": "scatter", "target_pages": 20})),
        {},
        "scatter sets `target_count`",
    ),
    "longform too short": (
        plan(step(recipe={"kind": "longform", "target_pages": 5})),
        {},
        "longform is for 13 to 100",
    ),
    "longform as an answer": (
        plan(step(recipe={"kind": "longform", "target_pages": 40}), deliverable="answer"),
        {},
        "longform writes a document",
    ),
    # The commit's graph rules, asked in-run: the Lead fixes the plan before it reaches the commit.
    "labels repeat": (plan(step(), step()), {}, "Two steps share a label: s1"),
    "dangling dependency": (plan(step(depends_on=["s9"])), {}, "depends on a step that does not exist: s9"),
    "cycle": (
        plan(step(depends_on=["s2"]), step(label="s2", depends_on=["s1"])),
        {},
        "The plan has a cycle among: s1, s2",
    ),
    "nothing delivered": (
        plan(step(agent="researcher", publishes=False)),
        {},
        "No step publishes anything",
    ),
    "the dialed kind": (
        plan(),
        {"facts": WEB.model_copy(update={"deliverable": ArtifactType.SPREADSHEET})},
        "user chose a spreadsheet deliverable",
    ),
}


@pytest.mark.parametrize("rule", sorted(BROKEN))
async def test_each_rule_sends_a_broken_plan_back_and_the_fixed_plan_passes(rule: str) -> None:
    broken, deps, said = BROKEN[rule]
    facts = str(deps.get("facts"))
    fixed = (
        plan(step(agent="reviewer"))
        if "direct_bot='reviewer'" in facts
        else plan(step(agent="maker", produces="spreadsheet"))
        if "spreadsheet" in facts
        else plan()
    )
    scripted = Scripted(broken, fixed)
    ending = await run(scripted, **deps)
    assert len(scripted.told) == 1 and said in scripted.told[0]
    assert not ending.plan.fallback and ending.plan.steps[0].agent == fixed["plan"]["steps"][0]["agent"]


async def test_a_plan_that_keeps_the_rules_passes_first_time_with_code_fields_set() -> None:
    scripted = Scripted(
        plan(
            step(
                agent="researcher",
                publishes=False,
                skills=["compare"],
                recipe={"kind": "scatter", "target_count": 60},
            ),
            step(label="s2", agent="reviewer", depends_on=["s1"]),
        )
    )
    ending = await run(scripted, bots=(a_bot(),))
    assert scripted.told == []
    assert ending.plan.request == KNOWN.request and ending.plan.estimate is not None
    assert ending.plan.steps[0].recipe is not None and ending.plan.steps[0].recipe.kind is RecipeKind.SCATTER


async def test_without_facts_the_check_is_inert() -> None:
    scripted = Scripted(plan(step(agent="analyst")))
    ending = await run(scripted, facts=None)
    assert scripted.told == [] and ending.plan.steps[0].agent == "analyst"


# ── the third broken plan is the floor, and it says so ───────────────────────────────────────────


async def test_the_third_broken_plan_runs_the_floor_and_says_so() -> None:
    broken = plan(step(agent="analyst"))
    scripted = Scripted(broken, broken, broken)
    ending = await run(scripted)
    assert len(scripted.told) == 2 and scripted.plans == []
    assert ending.say == lead_check.FLOOR_SAY and ending.plan.fallback == lead_check.REFUSED_WHY
    (only,) = ending.plan.steps
    assert only.agent == "researcher" and ending.plan.deliverable is DeliverableKind.ANSWER, (
        "no step of the plan can deliver and no kind is dialed: an answer, never a document"
    )
    assert ending.plan.request == KNOWN.request and ending.plan.estimate is not None


# Each broken plan's intended ending, and the floor's one step that keeps it: (agent, deliverable, produces).
KEEPS: dict[str, tuple[dict[str, Any], tuple[str, DeliverableKind, ArtifactType | None]]] = {
    "an action stays an action": (
        plan(
            step(agent="researcher", publishes=False, skills=["poetry"]),
            step(label="s2", agent="operator", hosts=["uber.com"], depends_on=["s1"]),
            step(label="s3", depends_on=["s2"]),
            deliverable="action",
        ),
        ("operator", DeliverableKind.ACTION, None),
    ),
    "research stays research": (
        plan(step(agent="researcher", skills=["poetry"]), deliverable="answer"),
        ("researcher", DeliverableKind.ANSWER, None),
    ),
    "a sheet stays a sheet": (
        plan(step(agent="maker", produces="spreadsheet", skills=["poetry"])),
        ("maker", DeliverableKind.FILES, ArtifactType.SPREADSHEET),
    ),
}


@pytest.mark.parametrize("case", sorted(KEEPS))
async def test_the_floor_keeps_the_ending_the_plan_was_for(case: str) -> None:
    broken, (agent, deliverable, produces) = KEEPS[case]
    ending = await run(Scripted(broken, broken, broken))
    (only,) = ending.plan.steps
    assert (only.agent, ending.plan.deliverable, only.produces) == (agent, deliverable, produces)
    assert only.goal == lead_check.FLOOR_GOAL and not only.skills and only.recipe is None
    assert only.hosts == (["uber.com"] if agent == "operator" else [])


def test_the_floor_passes_the_rules_the_commit_holds_it_to() -> None:
    known = seated(KNOWN)
    roster = {
        seat.name: DELIVERS_OUTCOME if seat.delivers_outcome else (seat.kinds if seat.publishes else None)
        for seat in known.seats
    }
    for broken, _ in KEEPS.values():
        intended = SubmitPlan.model_validate(broken).plan
        floor = lead_check.fallback(
            intended, known.seats, dialed=None, doc_format=None, request=KNOWN.request, why="x"
        )
        assert len(validate(floor, roster=roster, inputs=[])[0]) == 1


def test_the_floor_keeps_the_browser_a_web_step_asked_for() -> None:
    intended = SubmitPlan.model_validate(plan(step(agent="operator", browser=True))).plan
    floor = lead_check.fallback(
        intended, seated(KNOWN).seats, dialed=None, doc_format=None, request=KNOWN.request, why="x"
    )
    (only,) = floor.steps
    assert only.agent == "operator" and only.browser, "the floor's operator lost its browser"


async def test_the_floor_delivers_the_dialled_kind() -> None:
    broken = plan(step(agent="analyst"))
    dialled = WEB.model_copy(update={"deliverable": ArtifactType.SPREADSHEET})
    ending = await run(Scripted(broken, broken, broken), facts=dialled)
    (only,) = ending.plan.steps
    assert only.agent == "maker" and only.produces is ArtifactType.SPREADSHEET


def test_the_floor_never_seats_a_bot_even_one_listed_first_that_could_deliver() -> None:
    able_bot = Seat(
        name="sheets",
        model_class=ModelClass.FAST_EXECUTOR,
        seat_class=ModelClass.FAST_EXECUTOR,
        kinds=(ArtifactType.SPREADSHEET,),
        makes_formats=True,
        publishes=True,
        bot=True,
    )
    known = seated(KNOWN).model_copy(update={"seats": (able_bot, *seated(KNOWN).seats)})
    dialled = WEB.model_copy(update={"deliverable": ArtifactType.SPREADSHEET})
    (only,) = lead_check.floor(dialled, known, "broke").plan.steps
    assert only.agent == "maker"


# ── the estimate is the catalog's prices over the steps' runs ───────────────────────────────────

M = Decimal("0.000001")
HEAD = ModelSpec(
    provider="p",
    model_id="head",
    family=Family("f"),
    protocol="openai_compatible",
    classes=(ModelClass.FAST_EXECUTOR,),
    prices={
        Unit.INPUT_TOKEN: Decimal("0.000003"),
        Unit.CACHE_READ_TOKEN: Decimal("0.0000003"),
        Unit.CACHE_WRITE_TOKEN: Decimal("0.00000375"),
        Unit.OUTPUT_TOKEN: Decimal("0.000015"),
        Unit.FLEX_INPUT_TOKEN: Decimal("0.0000015"),
        Unit.FLEX_CACHE_READ_TOKEN: Decimal("0.00000015"),
        Unit.FLEX_OUTPUT_TOKEN: Decimal("0.0000075"),
    },
)
BEHIND = ModelSpec(
    provider="p",
    model_id="behind",
    family=Family("f"),
    protocol="openai_compatible",
    classes=(ModelClass.FAST_EXECUTOR,),
    prices={Unit.INPUT_TOKEN: Decimal(1)},
)


def test_the_estimate_matches_the_catalog_prices_for_a_known_plan() -> None:
    known = run_cost.with_prices(
        seated(KNOWN), [BEHIND, HEAD], search=Decimal(0), order={"fast-executor": ["p:head", "p:behind"]}
    )
    ending = SubmitPlan.model_validate(plan(step(), step(label="s2", agent="maker", produces="spreadsheet")))
    estimate = lead_check.plan_estimate(
        ending.plan, WEB.model_copy(update={"budget_left_usd": Decimal("0.5")}), known
    )
    # writer: 2,800 uncached in, 28,000 read, 4,200 written, 3,500 out; maker: 8,800 / 88,000 / 13,200 / 12,500.
    writer = Decimal("0.0084") + Decimal("0.0084") + Decimal("0.01575") + Decimal("0.0525")
    maker = Decimal("0.0264") + Decimal("0.0264") + Decimal("0.0495") + Decimal("0.1875")
    assert [s.usd for s in estimate.steps] == [writer.quantize(M), maker.quantize(M)]
    assert estimate.usd == writer + maker and all(s.priced for s in estimate.steps)
    assert [s.ceiling_usd for s in estimate.steps] == [2 * writer, 2 * maker]
    assert estimate.ceiling_usd == Decimal("0.5")  # twice the estimate is 0.6802; the budget left caps it


def test_a_class_asked_on_flex_is_priced_at_its_flex_rates() -> None:
    settings = {"fast-executor": ClassSettings(service_tier="flex")}
    known = run_cost.with_prices(seated(KNOWN), [HEAD], search=Decimal(0), settings=settings)
    assert known.flex == (ModelClass.FAST_EXECUTOR,)
    ending = SubmitPlan.model_validate(plan())
    (writer,) = lead_check.plan_estimate(ending.plan, WEB, known).steps
    flex = Decimal("0.0042") + Decimal("0.0042") + Decimal("0.01575") + Decimal("0.02625")
    assert writer.usd == flex.quantize(M)


def test_an_unpriced_class_is_said_unpriced_never_free() -> None:
    ending = SubmitPlan.model_validate(plan())
    (writer,) = lead_check.plan_estimate(ending.plan, WEB, seated(KNOWN)).steps
    assert writer.usd == 0 and not writer.priced


def test_a_scatter_step_prices_its_seats_and_a_long_document_its_parts() -> None:
    known = run_cost.with_prices(seated(KNOWN), [HEAD], search=Decimal(0))
    scatter = step(agent="researcher", publishes=False, recipe={"kind": "scatter", "target_count": 60})
    longform = step(label="s2", depends_on=["s1"], recipe={"kind": "longform", "target_pages": 40})
    ending = SubmitPlan.model_validate(plan(scatter, longform))
    lines = lead_check.plan_estimate(ending.plan, WEB, known).steps
    assert [(line.label, line.seats) for line in lines] == [("s1", False), ("s1", True), ("s2", False)]
    assert lines[1].priced, "a seat's searches are priced beside its tokens"
    width = config.current().worker.swarm_max_width
    assert lines[1].requests == run_cost.SEAT_SIZE.requests * width
    assert lines[2].requests == run_cost.RUN_SIZES["writer"].requests * (
        1 + 7
    )  # 12,000 words in 1,900-word parts


async def test_the_plan_is_priced_by_code_whatever_the_model_sends() -> None:
    known = run_cost.with_prices(seated(KNOWN), [HEAD], search=Decimal(0))
    scatter = step(agent="researcher", publishes=False, recipe={"kind": "scatter", "target_count": 60})
    sent = plan(scatter, step(label="s2", depends_on=["s1"]))
    sent["plan"]["estimate"] = {"steps": [], "usd": "0", "ceiling_usd": "1000"}
    tight = WEB.model_copy(update={"budget_left_usd": Decimal("0.05")})
    ending = await run(Scripted(sent), facts=tight, known=known)
    assert ending.plan.estimate is not None
    assert [line.label for line in ending.plan.estimate.steps] == ["s1", "s1", "s2"]
    assert ending.plan.estimate.usd > 0
    assert ending.plan.estimate.ceiling_usd == Decimal("0.05")  # the budget left caps the high end


async def test_the_floor_is_sized_too() -> None:
    broken = plan(step(skills=["poetry"]))  # its writer delivers, so the floor's step is the writer's
    ending = await run(
        Scripted(broken, broken, broken), known=run_cost.with_prices(seated(KNOWN), [HEAD], search=Decimal(0))
    )
    (only,) = ending.plan.steps
    assert only.label == "s1" and ending.plan.estimate is not None
    assert [line.label for line in ending.plan.estimate.steps] == ["s1"] and ending.plan.estimate.usd > 0


def test_the_lead_carries_the_plan_check() -> None:
    assert any(isinstance(c, lead_check.PlanCheck) for c in get("lead").capabilities)


# ── the plan rules, which the commit holds every plan to and the Lead's run asks first ───────────
#
# They walk in order and the first broken one names itself; the message is fed back to the Lead
# verbatim, so its wording is pinned here byte-exact.

V1_ROSTER = {
    "writer": (ArtifactType.DOCUMENT,),
    "researcher": (ArtifactType.DOCUMENT,),
    "designer": (ArtifactType.DECK,),
    "computer": DELIVERS_OUTCOME,
    "operator": None,
}


def _seat(name: str, makes: tuple[ArtifactType, ...] | object) -> Seat:
    """A roster entry as the Lead's plan facts carry it."""
    kinds = makes if isinstance(makes, tuple) else ()
    return Seat(
        name=name,
        model_class=ModelClass.FAST_EXECUTOR,
        seat_class=ModelClass.FAST_EXECUTOR,
        kinds=cast(tuple[ArtifactType, ...], kinds),
        publishes=bool(kinds),
        delivers_outcome=makes is DELIVERS_OUTCOME,
    )


V1_SEATS = tuple(_seat(name, makes) for name, makes in V1_ROSTER.items())


def v1_step(
    label: str, agent: str = "writer", *, deps: tuple[str, ...] = (), pub: bool = True, filename: str = ""
) -> ProposedStep:
    """A proposed step, publishing a document by default."""
    return ProposedStep(
        label=label,
        agent=agent,
        title=f"step {label}",
        goal=f"do {label}",
        acceptance=["it is done"],
        depends_on=list(deps),
        publishes=pub,
        filename=filename,
    )


def v1_plan(*steps: ProposedStep, must: tuple[str, ...] = ("the goal is met",)) -> PlanProposal:
    """A proposal over `steps`."""
    return PlanProposal(must=list(must), steps=list(steps))


_SILENT = (
    "step s1: operator publishes nothing - the change it makes is the deliverable and its "
    "narration flows to the steps that depend on it. Set publishes=false, and publish from a step "
    "that depends on it (the writer, or the maker for a file format)."
)
_NOTHING = (
    "no step publishes anything - the task would run, cost money, and deliver nothing to the user. "
    "At least one step must set publishes=true, or the plan must end on an agent that publishes an "
    "outcome."
)

V1_REJECTED: list[tuple[str, PlanProposal, ArtifactType | None, str]] = [
    ("no steps", v1_plan(), None, "the plan has no steps"),
    (
        "too many steps",
        v1_plan(*(v1_step(f"s{n}") for n in range(13))),
        None,
        "13 steps is more than the 12 a plan may have",
    ),
    (
        "labels repeat",
        v1_plan(v1_step("s2"), v1_step("s1"), v1_step("s2"), v1_step("s1")),
        None,
        "two steps share a label: s1, s2",
    ),
    (
        "unknown agents",
        v1_plan(v1_step("s1", "zeta"), v1_step("s2", "analyst")),
        None,
        "no such agent: analyst, zeta. The roster is: computer, designer, operator, researcher, writer",
    ),
    (
        "dangling dependency names the first sorted",
        v1_plan(v1_step("s1", deps=("z", "y"))),
        None,
        "step s1 depends on a step that does not exist: y",
    ),
    ("self dependency", v1_plan(v1_step("s1", deps=("s1",))), None, "step s1 depends on itself"),
    (
        "per-step order: the first step's self loop before the second's dangle",
        v1_plan(v1_step("s1", deps=("s1",)), v1_step("s2", deps=("nope",))),
        None,
        "step s1 depends on itself",
    ),
    (
        "cycle",
        v1_plan(v1_step("s1", deps=("s2",)), v1_step("s2", deps=("s1",))),
        None,
        "the plan has a cycle among: s1, s2 - it would never finish",
    ),
    ("a silent agent asked to publish", v1_plan(v1_step("s1", "operator")), None, _SILENT),
    (
        "nothing delivered",
        v1_plan(v1_step("s1", pub=False), v1_step("s2", "researcher", pub=False)),
        None,
        _NOTHING,
    ),
    (
        "an outcome step that feeds another is not an ending",
        v1_plan(v1_step("s1", "computer", pub=False), v1_step("s2", pub=False, deps=("s1",))),
        None,
        _NOTHING,
    ),
    (
        "the final publisher is the wrong kind",
        v1_plan(v1_step("s1", "designer"), v1_step("s2", deps=("s1",))),
        ArtifactType.DECK,
        "the user chose a deck deliverable, but the final publishing step ('s2', agent 'writer') "
        "publishes document. The delivering step must use: designer",
    ),
    (
        "an outcome never satisfies a dial",
        v1_plan(v1_step("s1", "computer", pub=False)),
        ArtifactType.DOCUMENT,
        "the user chose a document deliverable, but no step publishes it. "
        "The delivering step must use: researcher, writer",
    ),
    (
        "no agent publishes the dialed kind",
        v1_plan(v1_step("s1")),
        ArtifactType.SPREADSHEET,
        "the user chose a spreadsheet deliverable, but the final publishing step ('s1', agent "
        "'writer') publishes document. The delivering step must use: no agent publishes this kind",
    ),
]


@pytest.mark.parametrize(
    ("name", "proposal", "deliverable", "message"), V1_REJECTED, ids=[c[0] for c in V1_REJECTED]
)
def test_each_rule_names_itself_exactly(
    name: str, proposal: PlanProposal, deliverable: ArtifactType | None, message: str
) -> None:
    del name
    with pytest.raises(InvalidPlan) as broke:
        validate(proposal, roster=V1_ROSTER, inputs=[], deliverable=deliverable)
    assert str(broke.value) == message
    assert breach(proposal, V1_SEATS, deliverable) == message, "the run asks the commit's own rule set"


def test_an_outcome_ending_delivers_without_a_publish() -> None:
    built, edges, criteria = validate(
        v1_plan(v1_step("s1", "computer", pub=False)), roster=V1_ROSTER, inputs=[]
    )
    assert [(s.agent, s.contract.output if s.contract else "-") for _, s in built] == [("computer", None)]
    assert edges == []
    assert criteria.must == ["the goal is met"]


def test_the_dialed_kind_from_the_last_publisher_in_execution_order_passes() -> None:
    proposal = v1_plan(v1_step("s2", "designer", deps=("s1",)), v1_step("s1"))
    built, _, _ = validate(proposal, roster=V1_ROSTER, inputs=[], deliverable=ArtifactType.DECK)
    assert [s.agent for _, s in built] == ["writer", "designer"]


def test_a_sound_plan_builds_exactly() -> None:
    long_title, long_why = "t" * 130, "w" * 200
    proposal = v1_plan(
        v1_step("s2", deps=("s1",), filename="../escape.md"),
        ProposedStep(
            label="s1",
            agent="researcher",
            title=long_title,
            goal="find it",
            acceptance=["sources cited"],
            publishes=False,
            why=long_why,
        ),
        v1_step("s0", filename="brief.md"),
        must=(),
    )
    inputs = [TaskInput(filename="notes.md", sha256="a" * 64, bytes=1)]
    built, edges, criteria = validate(proposal, roster=V1_ROSTER, inputs=inputs)

    paths = list(input_paths(["notes.md"]))
    promise = [
        *input_clause(paths),
        *depth_clause(ArtifactType.DOCUMENT),
    ]
    rows = [
        (
            s.kind.value,
            s.agent,
            s.title,
            s.why,
            s.contract.goal if s.contract else "",
            s.contract.acceptance if s.contract else [],
            s.contract.inputs if s.contract else [],
            (s.contract.output.type.value, s.contract.output.title, s.contract.output.filename)
            if s.contract and s.contract.output
            else None,
        )
        for _, s in built
    ]
    assert rows == [
        (
            "step",
            "writer",
            "step s0",
            "",
            "do s0",
            ["it is done", *promise],
            paths,
            ("document", "step s0", "brief.md"),
        ),
        ("step", "researcher", "t" * 120, "w" * 200, "find it", ["sources cited"], paths, None),
        (
            "step",
            "writer",
            "step s2",
            "",
            "do s2",
            ["it is done", *promise],
            paths,
            ("document", "step s2", None),
        ),
    ]
    title_of = {sid: s.title for sid, s in built}
    assert [(title_of[e.from_step], title_of[e.to_step], e.kind) for e in edges] == [
        ("t" * 120, "step s2", "produces_for")
    ]
    assert (criteria.must, criteria.tone, criteria.audience) == (["do s0"], "", "")


def test_a_recipe_step_delivers_through_its_shape() -> None:
    """A lone scatter step publishes nothing itself: its shape's synthesis delivers, in-run as at the commit."""
    found = ProposedStep(label="s1", agent="researcher", title="Find", goal="find 60", publishes=False)
    recipe = StepRecipe(kind=RecipeKind.SCATTER, target_count=60)
    assert breach(PlanProposal(steps=[found.model_copy(update={"recipe": recipe})]), V1_SEATS, None) is None
    assert breach(PlanProposal(steps=[found]), V1_SEATS, None) == _NOTHING


def test_a_step_loads_the_skills_it_names_at_their_pinned_version() -> None:
    proposal = v1_plan(v1_step("s1").model_copy(update={"skills": ["compare", "gone"]}))
    pins = {"compare": SkillPin(name=SkillName("compare"), version=3)}
    [(_, spec)] = validate(proposal, roster=V1_ROSTER, inputs=[], pins=pins)[0]
    assert spec.skill_pins == [SkillPin(name=SkillName("compare"), version=3)], "an unpinned name is left out"


# ── the say names no agent ───────────────────────────────────────────────────────────────────────


async def test_a_say_naming_an_agent_goes_back_once_then_a_neutral_line_stands() -> None:
    named = plan() | {"say": "I'll have the Maker build the script."}
    scripted = Scripted(named, named)
    ending = await run(scripted)
    assert len(scripted.told) == 1 and "the maker" in scripted.told[0]
    assert ending.say == lead_check.PLAN_SAY and not ending.plan.fallback


async def test_an_ordinary_sentence_with_an_agent_word_passes() -> None:
    said = "I'll read each operator's terms and have a writer-friendly summary ready."
    scripted = Scripted(plan() | {"say": said})
    ending = await run(scripted)
    assert scripted.told == [] and ending.say == said


# ── an ending written as text, and a batch asking what it could assume ─────────────────────────


class Parts:
    """A model that answers with each response's parts in turn and keeps what it was told back."""

    def __init__(self, *responses: list[Any]) -> None:
        self.responses = list(responses)
        self.told: list[str] = []

    def __call__(self, messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        if isinstance(last, ModelRequest):
            self.told += [f"{p.part_kind}:{getattr(p, 'content', '')}" for p in last.parts]
        return ModelResponse(parts=self.responses.pop(0))


async def _ended(model: Parts) -> object:
    task = TaskDeps(lead_facts=WEB, plan_facts=seated(KNOWN))
    runner = build("lead", task, [fake_row(ModelClass.FRONTIER_EXECUTOR)])
    with runner.agent.override(model=FunctionModel(model)):
        return (await runner.run("go")).output


def _fenced(value: dict[str, Any]) -> TextPart:
    return TextPart(f"I'll build it now.\n\n```json\n{json.dumps(value)}\n```")


async def test_a_plan_written_as_text_goes_back_then_the_called_plan_stands() -> None:
    model = Parts([_fenced(plan())], [ToolCallPart("submit_plan", plan())])
    assert isinstance(await _ended(model), SubmitPlan)
    assert any("submit_plan" in told and "as text" in told for told in model.told), model.told


async def test_a_plan_written_as_text_twice_stands_as_the_plan_held_to_its_checks() -> None:
    written = _fenced(plan())
    ended = await _ended(Parts([written], [written], [written]))
    assert isinstance(ended, SubmitPlan) and ended.plan.estimate is not None, (
        "checked and priced as a call is"
    )


@pytest.mark.parametrize(
    "text",
    [
        'Here is a config:\n\n```json\n{"name": "prismoid", "steps": 3}\n```',
        # A sample quiz the person asked for: an ask's shape, plus a key of its own.
        'A sample:\n\n```json\n{"title": "Onboarding quiz", "context": "New hires.", '
        '"questions": [{"prompt": "Your team?", "type": "text"}]}\n```',
        "{not json at all",
        "A plain answer.",
    ],
)
async def test_text_that_is_no_ending_stays_the_reply(text: str) -> None:
    assert await _ended(Parts([TextPart(text)])) == text


def _ask(*defaults: str) -> ToolCallPart:
    questions = [{"prompt": f"Q{n}?", "type": "text", "default": d} for n, d in enumerate(defaults, start=1)]
    return ToolCallPart("ask_person", {"context": "Why I ask.", "questions": questions})


async def test_a_batch_asking_what_it_could_assume_goes_back_then_the_fewer_questions_stand() -> None:
    model = Parts([_ask("", "the team", "docx")], [_ask("")])
    ended = await _ended(model)
    assert isinstance(ended, AskUser) and len(ended.questions) == 1
    assert any("2, 3 carry a safe default" in told for told in model.told), model.told


async def test_a_lone_question_with_a_default_stands() -> None:
    ended = await _ended(Parts([_ask("yes")]))
    assert isinstance(ended, AskUser) and len(ended.questions) == 1


def test_a_batch_holds_three_questions_at_most() -> None:
    questions = [ProposedQuestion(prompt=f"Q{n}?", type=QuestionType.TEXT) for n in range(4)]
    with pytest.raises(ValueError, match="at most 3"):
        AskUser(context="Why.", questions=questions)
