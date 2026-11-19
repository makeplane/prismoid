# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Plan validation.

Every one of these proposals is something a competent model will eventually produce, and every one
of them, unchecked, produces a task that **cannot finish** - or, worse, one that finishes and hands
the user nothing. The point of the plan rules is that none of this is a model behaviour to be argued
with at 3 a.m.: it is a test, and it runs in microseconds with nothing stood up.
"""

from __future__ import annotations

import pytest

from prismoid.ai.context.brief import handover
from prismoid.ai.rules.plan import DELIVERS_OUTCOME, MAX_STEPS, InvalidPlan, validate
from prismoid.operations.plan_shapes import one_step_plan
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import PlanProposal, ProposedStep, StepKind
from prismoid.schemas.sizing import SizeBucket

# How the maker hands its file over: the agent's brief, never a line a reviewer checks the file against.
HANDOVER = ("/work", "/out/change", "publish tool", "type='")

# name → the kinds it publishes (`AgentDef.kinds`), exactly as operations/planning flattens the
# registry for the policy layer. A mapping, not a list, since the contract's type follows the agent.
ROSTER = {"writer": (ArtifactType.DOCUMENT,), "researcher": (ArtifactType.DOCUMENT,)}
# The maker makes several kinds: a step on it names its kind (`ProposedStep.produces`).
MAKES = (ArtifactType.DOCUMENT, ArtifactType.SPREADSHEET, ArtifactType.DECK, ArtifactType.COLLECTION)


def step(
    label: str,
    agent: str = "writer",
    *,
    deps: list[str] | None = None,
    pub: bool = True,
    produces: ArtifactType | None = None,
):
    return ProposedStep(
        produces=produces,
        label=label,
        agent=agent,
        title=f"step {label}",
        goal=f"do {label}",
        acceptance=["it is done"],
        depends_on=deps or [],
        publishes=pub,
    )


def plan(*steps: ProposedStep) -> PlanProposal:
    return PlanProposal(must=["the goal is met"], steps=list(steps))


# ── a sound plan becomes a plan ───────────────────────────────────────────────────────────────


def test_a_two_step_chain_is_ordered_and_wired() -> None:
    steps, edges, criteria = validate(
        plan(step("s1", "researcher", pub=True), step("s2", "writer", deps=["s1"])),
        roster=ROSTER,
        inputs=[],
    )

    assert [s.title for _, s in steps] == ["step s1", "step s2"], "not topologically ordered"
    assert len(edges) == 1
    assert edges[0].from_step == steps[0][0] and edges[0].to_step == steps[1][0]
    assert criteria.must == ["the goal is met"]


def test_a_publishing_step_gets_the_publish_contract_it_did_not_ask_for() -> None:
    """The grounding rule and the typed output are the system's promise, not the planner's to restate -
    and a model asked to restate them will eventually restate them slightly wrong."""
    steps, _, _ = validate(plan(step("s1")), roster=ROSTER, inputs=[])
    contract = steps[0][1].contract
    assert contract is not None
    assert any("traceable to a source" in a for a in contract.acceptance)
    assert contract.output is not None and "type='document'" in handover(contract.output)


def test_a_research_step_owes_a_finding_not_a_file() -> None:
    steps, _, _ = validate(
        plan(step("s1", "researcher", pub=False), step("s2", "writer", deps=["s1"])),
        roster=ROSTER,
        inputs=[],
    )
    research = steps[0][1]
    assert research.contract is not None
    assert research.contract.output is None, "a non-publishing step must not be required to publish"
    assert steps[0][1].kind is StepKind.STEP


# ── and the six ways a plausible proposal is not a plan ───────────────────────────────────────


def test_an_empty_plan_is_rejected() -> None:
    with pytest.raises(InvalidPlan, match="no steps"):
        validate(plan(), roster=ROSTER, inputs=[])


def test_a_runaway_is_rejected() -> None:
    many = [step(f"s{i}") for i in range(MAX_STEPS + 1)]
    with pytest.raises(InvalidPlan, match="more than"):
        validate(plan(*many), roster=ROSTER, inputs=[])


def test_duplicate_labels_are_rejected() -> None:
    """Two steps called s1 make `depends_on: [s1]` meaningless - and it resolves to whichever one
    the dict happened to keep."""
    with pytest.raises(InvalidPlan, match="share a label"):
        validate(plan(step("s1"), step("s1")), roster=ROSTER, inputs=[])


def test_a_hallucinated_agent_is_rejected_and_the_roster_is_named() -> None:
    """The single most common planner hallucination: inventing `analyst` because the goal said
    "analyse". Unchecked, it surfaces minutes later at dispatch as a KeyError nobody can act on."""
    with pytest.raises(InvalidPlan, match="no such agent: analyst") as bad:
        validate(plan(step("s1", "analyst")), roster=ROSTER, inputs=[])
    assert "researcher" in str(bad.value), "the rejection must tell the planner what it MAY use"


def test_a_dangling_dependency_is_rejected() -> None:
    """A step waiting on a label that was never declared waits forever."""
    with pytest.raises(InvalidPlan, match="does not exist: s9"):
        validate(plan(step("s1", deps=["s9"])), roster=ROSTER, inputs=[])


def test_a_cycle_is_rejected() -> None:
    """`research → write → fact-check → research`. A human would nod at it. It never terminates,
    and no single step looks wrong."""
    with pytest.raises(InvalidPlan, match="cycle"):
        validate(
            plan(
                step("s1", "researcher", deps=["s3"], pub=False),
                step("s2", "writer", deps=["s1"]),
                step("s3", "researcher", deps=["s2"], pub=False),
            ),
            roster=ROSTER,
            inputs=[],
        )


def test_a_self_dependency_is_rejected() -> None:
    with pytest.raises(InvalidPlan, match="depends on itself"):
        validate(plan(step("s1", deps=["s1"])), roster=ROSTER, inputs=[])


def test_a_plan_that_delivers_nothing_is_rejected() -> None:
    """**The worst of the six, because it is the only one that looks like success.** Every step is
    research; the task runs, spends money, goes `delivered` - and the user has nothing to download.
    """
    with pytest.raises(InvalidPlan, match="deliver nothing"):
        validate(
            plan(step("s1", "researcher", pub=False), step("s2", "researcher", deps=["s1"], pub=False)),
            roster=ROSTER,
            inputs=[],
        )


# ── the floor ─────────────────────────────────────────────────────────────────────────────────


def test_the_one_step_plan_is_still_a_valid_plan() -> None:
    """It is the fallback when a proposal is rejected - so if it were not sound, a bad planner day
    would take the whole system down with it."""
    steps, edges, criteria = one_step_plan("summarise the attached brief")
    assert len(steps) == 1 and not edges
    assert steps[0][1].contract is not None
    assert steps[0][1].contract.output is not None
    assert criteria.must == ["summarise the attached brief"]


# ── the contract's type follows the agent ─────────────────────────────────────────────────────


def test_a_publishing_step_is_typed_by_its_agent_not_by_a_blanket_document() -> None:
    """The old constant typed EVERY publishing step `document`, so a workbook's contract lied and
    the verify judge graded an `.xlsx` against a demand for prose. The type now rides the roster
    mapping - one of the agent's kinds, never a kind it does not make."""
    roster = {"writer": (ArtifactType.DOCUMENT,), "maker": MAKES}
    steps, _, _ = validate(
        plan(step("s1", "maker", produces=ArtifactType.SPREADSHEET), step("s2", "writer", deps=["s1"])),
        roster=roster,
        inputs=[],
    )

    sheet_contract = steps[0][1].contract
    assert sheet_contract is not None and sheet_contract.output is not None
    assert sheet_contract.output.type is ArtifactType.SPREADSHEET

    writer_contract = steps[1][1].contract
    assert writer_contract is not None and writer_contract.output is not None
    assert writer_contract.output.type is ArtifactType.DOCUMENT


def test_the_one_step_plan_can_be_typed_too() -> None:
    steps, _, _ = one_step_plan("model the budget", agent="maker", produces=ArtifactType.SPREADSHEET)
    contract = steps[0][1].contract
    assert contract is not None and contract.output is not None
    assert contract.output.type is ArtifactType.SPREADSHEET


# ── the deliverable honors the user's choice ──────────────────────────────────────────────────


def test_a_chosen_deliverable_constrains_the_final_publishing_step() -> None:
    """A dropdown implicitly promises determinism: when the user picked collection, a plan whose
    last published thing is prose is REJECTED - with the agents that could deliver it named, so
    the retry gotcha teaches the planner instead of just refusing it."""
    roster = {"writer": (ArtifactType.DOCUMENT,), "maker": MAKES}

    with pytest.raises(InvalidPlan, match="maker"):
        validate(plan(step("s1")), roster=roster, inputs=[], deliverable=ArtifactType.COLLECTION)

    steps, _, _ = validate(
        plan(step("s1", "maker")), roster=roster, inputs=[], deliverable=ArtifactType.COLLECTION
    )
    contract = steps[0][1].contract
    assert contract is not None and contract.output is not None
    assert contract.output.type is ArtifactType.COLLECTION


def test_interim_publishes_stay_legal_under_a_deliverable() -> None:
    """Only the FINAL publishing step is constrained - a writer's memo feeding the board is a
    working note, not a broken promise."""
    roster = {"writer": (ArtifactType.DOCUMENT,), "maker": MAKES}
    steps, _, _ = validate(
        plan(step("s1", "writer"), step("s2", "maker", deps=["s1"])),
        roster=roster,
        inputs=[],
        deliverable=ArtifactType.COLLECTION,
    )
    final = steps[-1][1].contract
    assert final is not None and final.output is not None
    assert final.output.type is ArtifactType.COLLECTION


def test_a_step_on_an_agent_of_several_kinds_publishes_the_kind_it_names() -> None:
    """The maker makes several kinds: a step names its own, and a name outside the agent's kinds, or no
    name with no deliverable to fall back on, is refused with the kinds it could have named."""
    roster = {"writer": (ArtifactType.DOCUMENT,), "maker": MAKES}
    steps, _, _ = validate(plan(step("s1", "maker", produces=ArtifactType.DECK)), roster=roster, inputs=[])
    contract = steps[0][1].contract
    assert contract is not None and contract.output is not None
    assert contract.output.type is ArtifactType.DECK

    for named in (None, ArtifactType.CODE):
        with pytest.raises(InvalidPlan, match="maker makes document, spreadsheet, deck, collection"):
            validate(plan(step("s1", "maker", produces=named)), roster=roster, inputs=[])


def test_a_single_kind_agent_keeps_its_kind_whatever_the_step_names() -> None:
    """Typed by the agent, never writable by the proposal: a writer step naming a deck is still a document."""
    steps, _, _ = validate(plan(step("s1", produces=ArtifactType.DECK)), roster=ROSTER, inputs=[])
    contract = steps[0][1].contract
    assert contract is not None and contract.output is not None
    assert contract.output.type is ArtifactType.DOCUMENT


# ── an agent whose deliverable is a change in a service publishes nothing ──────────────────


def test_a_step_whose_agent_publishes_nothing_may_not_be_asked_to_publish() -> None:
    """The operator has no publish tool: its deliverable is the change on
    the site or in the service, and its narration is the receipt. A planned step that set
    publishes=true got a document contract it could never meet, failed every attempt, and drove
    the live site again on each one."""
    roster = {**ROSTER, "operator": None}
    with pytest.raises(InvalidPlan, match="operator publishes nothing"):
        validate(plan(step("read", "operator", pub=True)), roster=roster, inputs=[])

    steps, edges, _ = validate(
        plan(step("read", "operator", pub=False), step("report", "writer", deps=["read"])),
        roster=roster,
        inputs=[],
    )
    (_, reading), (_, writing) = steps
    assert reading.contract is not None and reading.contract.output is None
    assert writing.contract is not None and writing.contract.output is not None
    assert len(edges) == 1, "the narration flows down the edge the reading step feeds"


# ── an agent whose step delivers its outcome (the operator) ────────────

OUTCOME_ROSTER = {**ROSTER, "operator": DELIVERS_OUTCOME}


def test_a_step_of_a_browsing_agent_drives_the_browser_only_when_the_caller_says_it_browses() -> None:
    """A step drives the browser when the Lead says it must work on a website AND the caller names its agent as
    one that browses (computer use on); a connector act of the same agent takes no browser."""
    site = step("do", "operator", deps=["read"], pub=False).model_copy(update={"browser": True})
    proposal = plan(step("read", "researcher", pub=False), site)
    on = {
        str(s.agent): s.browser
        for _, s in validate(proposal, roster=OUTCOME_ROSTER, inputs=[], browses=["operator"])[0]
    }
    off = {str(s.agent): s.browser for _, s in validate(proposal, roster=OUTCOME_ROSTER, inputs=[])[0]}
    assert on == {"operator": True, "researcher": False}
    assert off == {"operator": False, "researcher": False}
    act = plan(step("do", "operator", pub=False))
    assert not validate(act, roster=OUTCOME_ROSTER, inputs=[], browses=["operator"])[0][0][1].browser


def test_a_plan_that_ends_on_an_outcome_step_delivers() -> None:
    """Rule 6 used to force a writer behind every browser plan: an operator step's report IS the
    deliverable, so a plan that ends on one delivers - and no typed contract is forged for it,
    even when the planner set publishes=true."""
    for pub in (False, True):
        steps, edges, _ = validate(plan(step("do", "operator", pub=pub)), roster=OUTCOME_ROSTER, inputs=[])
        ((_, only),) = steps
        assert edges == [] and str(only.agent) == "operator"
        assert only.contract is not None and only.contract.output is None
        assert not any("publish tool" in a for a in only.contract.acceptance)


def test_an_outcome_step_upstream_of_nothing_delivered_is_still_rejected() -> None:
    with pytest.raises(InvalidPlan, match="no step publishes"):
        validate(
            plan(step("do", "operator", pub=False), step("read", "researcher", deps=["do"], pub=False)),
            roster=OUTCOME_ROSTER,
            inputs=[],
        )


def test_an_outcome_never_satisfies_a_chosen_deliverable() -> None:
    with pytest.raises(InvalidPlan, match="chose a document deliverable"):
        validate(
            plan(step("do", "operator", pub=True)),
            roster=OUTCOME_ROSTER,
            inputs=[],
            deliverable=ArtifactType.DOCUMENT,
        )
    steps, _, _ = validate(
        plan(step("do", "operator", pub=False), step("write", "writer", deps=["do"])),
        roster=OUTCOME_ROSTER,
        inputs=[],
        deliverable=ArtifactType.DOCUMENT,
    )
    assert steps[-1][1].contract is not None and steps[-1][1].contract.output is not None


def test_an_outcome_sink_delivers_whatever_the_label_order() -> None:
    """The verdict never rides a topological tie-break: an outcome step nothing depends on
    delivers, in either label order."""
    for first, second in (("operator", "researcher"), ("researcher", "operator")):
        steps, _, _ = validate(
            plan(step("a", first, pub=False), step("b", second, pub=False)),
            roster=OUTCOME_ROSTER,
            inputs=[],
        )
        assert len(steps) == 2


def test_a_file_the_user_named_rides_the_steps_contract() -> None:
    """`sky.md` must reach the system's publish as data: a name only in the goal's prose was lost."""
    named = step("s1").model_copy(update={"filename": "sky.md"})
    steps, _, _ = validate(plan(named), roster=ROSTER, inputs=[])
    contract = steps[0][1].contract
    assert contract is not None and contract.output is not None
    assert contract.output.filename == "sky.md"


@pytest.mark.parametrize("hostile", ["../sky.md", "a/b.md", ".env", "a\\b.md", "   "])
def test_a_named_file_is_a_plain_name_or_no_name(hostile: str) -> None:
    named = step("s1").model_copy(update={"filename": hostile})
    steps, _, _ = validate(plan(named), roster=ROSTER, inputs=[])
    contract = steps[0][1].contract
    assert contract is not None and contract.output is not None
    assert contract.output.filename is None


# ── acceptance is what the person checks ──────────────────────────────────────────────────────


@pytest.mark.parametrize("kind", MAKES)
def test_acceptance_never_carries_how_the_file_is_handed_over(kind: ArtifactType) -> None:
    """QA and the receipt judge read acceptance verbatim: a publish order there failed a published file."""
    steps, _, _ = validate(plan(step("s1", "maker", produces=kind)), roster={"maker": MAKES}, inputs=[])
    contract = steps[0][1].contract
    assert contract is not None and contract.output is not None and contract.output.type is kind
    assert not [line for line in contract.acceptance if any(word in line for word in HANDOVER)]


def test_a_collection_the_person_gave_no_number_gets_no_row_band() -> None:
    """ "10 cities" was judged against a 10-30 band and a 100-row ask capped at 45: no number, no band."""
    steps, _, _ = validate(
        plan(step("s1", "maker", produces=ArtifactType.COLLECTION)), roster={"maker": MAKES}, inputs=[]
    )
    contract = steps[0][1].contract
    assert contract is not None
    acceptance = " ".join(contract.acceptance)
    assert "rows is the band" not in acceptance and "ceiling" not in acceptance
    assert "rows carry substance" in acceptance


def test_a_collection_the_person_sized_carries_their_number() -> None:
    steps, _, _ = validate(
        plan(step("s1", "maker", produces=ArtifactType.COLLECTION)),
        roster={"maker": MAKES},
        inputs=[],
        size=(SizeBucket.EXTENDED, 40),
    )
    contract = steps[0][1].contract
    assert contract is not None
    assert any("~40 rows" in line for line in contract.acceptance)
