# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The plan: nodes, edges, edits - and the gates that park a job on a human.

Questions live here because a gate *is* a plan node, and its spec is a plan concern.

The QuestionSet schema is deliberately **flat: primitives and enums only, no nesting**. That is
not simplicity for its own sake - it is MCP's elicitation restriction, and honouring it
means any MCP-capable client renders our questions natively, with no adapter. The canonical
rendering is numbered 1..N on every surface, which is what makes `1: b, 2: yes, 3: skip` a
grammar a human can type in one line.
"""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.json_schema import SkipJsonSchema

from prismoid.schemas.computer_use import named_hosts
from prismoid.schemas.contract import BotPin, Contract, Criteria, SkillPin
from prismoid.schemas.ids import AgentName, GateId, StepId
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.usage import Unit

# The part a step plays in a deterministic lane; the agent seated in it may take that part's mode
# (`ai/agents/base.Mode`). Set only by the lanes' own expansion, never by a model.
LaneRole = Literal[
    "outline",
    "outline_draft",
    "section",
    "harmonize",
    "assembly",
    "express",
    "scatter_map",
    "gather",
    "synthesis",
    "fleet_table",
]


class StepKind(StrEnum):
    STEP = "step"  # an agent does work
    GATE = "gate"  # a human is asked something
    ARTIFACT = "artifact"  # a deliverable is published and verified


class StepState(StrEnum):
    """The step machine. Transitions outside it raise InvalidTransition - the map is the spec."""

    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    VERIFYING = "verifying"
    DONE = "done"
    RETRYING = "retrying"
    ESCALATED = "escalated"
    SKIPPED = "skipped"


class TaskStatus(StrEnum):
    """The task machine."""

    CREATED = "created"
    CLARIFYING = "clarifying"  # at most once, before planning
    PLANNING = "planning"
    RUNNING = "running"
    WAITING = "waiting"  # parked on a gate at ~zero cost
    PAUSED = "paused"  # user-initiated; dispatch stops at the next frontier boundary
    DELIVERED = "delivered"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskEnding(StrEnum):
    """What a settled turn's answer says it did, beside its status: `delivered` reads `done`, `partial`, or
    `could_not` when the Lead's answer declined; `failed` reads `could_not` when an agent answered that it
    could not do the work."""

    DONE = "done"
    PARTIAL = "partial"
    COULD_NOT = "could_not"


class TaskMode(StrEnum):
    STANDARD = "standard"
    FLEET = "fleet"  # every enabled model answers in parallel; the writer lays the answers in one table
    COUNCIL = "council"  # legacy wire value - accepted for compat, stored as FLEET; do not use


# ── questions ─────────────────────────────────────────────────────────────────────────────────


class QuestionType(StrEnum):
    TEXT = "text"
    NUMBER = "number"
    BOOLEAN = "boolean"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"


class QuestionOption(BaseModel):
    model_config = ConfigDict(frozen=True)

    value: str
    label: str
    description: str | None = None


class Question(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str  # stable within the set; the number the user types maps to this
    prompt: str
    type: QuestionType
    options: list[QuestionOption] = Field(default_factory=list[QuestionOption])
    required: bool = True
    default: str | None = None  # what `--decline` proceeds with, when it exists
    allow_other: bool = False


class TimeoutPolicy(StrEnum):
    PROCEED_WITH_DEFAULTS = "proceed_with_defaults"
    WAIT = "wait"
    CANCEL = "cancel"


class QuestionSet(BaseModel):
    """ONE numbered batch per gate - never dripped."""

    model_config = ConfigDict(frozen=True)

    title: str
    context: str = ""  # why we are asking; a question without a reason is an interruption
    questions: list[Question] = Field(min_length=1)
    expires_at: dt.datetime
    on_timeout: TimeoutPolicy = TimeoutPolicy.PROCEED_WITH_DEFAULTS

    def numbered(self) -> list[tuple[int, Question]]:
        """The canonical rendering, shared by the CLI, the API, and any MCP client."""
        return list(enumerate(self.questions, start=1))


# ── what the clarifier *proposes* ─────────────────────────────────────────────────────────────
#
# **Flatter than the QuestionSet it becomes, on purpose.** A model asked for
# `options: list[QuestionOption]` produces `[{"value": "a", "label": "a", "description": null}]`
# about as often as it produces something useful; asked for `options: list[str]` it produces
# `["euros", "dollars"]` every time. The policy layer expands them, and it also supplies `expires_at` -
# which is a *policy* (the gate TTL), not something a model should be inventing a timestamp for.
#
# The rule is "flat schemas: primitives and enums only". This is what it is for.


class ProposedQuestion(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt: str = Field(min_length=1, max_length=300)
    type: QuestionType
    options: list[str] = Field(default_factory=list, max_length=8)
    default: str = ""  # what `--decline` proceeds with. Empty ⇒ there is no sensible default.
    allow_other: bool = False

    def misshapen(self) -> str | None:
        """Why this question cannot be asked as it is typed, or None: a choice needs two options, text none."""
        selecting = self.type in (QuestionType.SINGLE_SELECT, QuestionType.MULTI_SELECT)
        if selecting and len(self.options) < 2:
            return f"is a {self.type.value} with fewer than two options"
        if not selecting and self.options:
            return f"is a {self.type.value} but carries options"
        return None


class Clarification(BaseModel):
    """At most ONE of these per task, before planning. Empty `questions` ⇒ nothing to ask."""

    model_config = ConfigDict(frozen=True)

    context: str = ""  # why we are asking. A question without a reason is an interruption.
    questions: list[ProposedQuestion] = Field(default_factory=list[ProposedQuestion], max_length=6)


class GateKind(StrEnum):
    APPROVE = "approve"  # the degenerate QuestionSet: content is null
    INPUT = "input"  # carries a QuestionSet
    BUDGET = "budget"  # the Bursar stopped, and will not overrun silently
    # A step needs a service the task does not hold: the gate IS the connect block
    # in the conversation - the user authorizes right there, accept attaches the (now live)
    # connector to the task, and the run resumes with its tools mounted. Decline = continue
    # without; timeout declines (questions=None, the approve gate's structural deny).
    CONNECT = "connect"
    PAUSE = "pause"  # a mid-step pause: no questions, no clock
    # A person at the browser: a step's browser call met a live control lease
    # (`source="takeover"`), or the agent asked for one with `browser.handoff` (`source="handoff"`).
    # Only the lease's ending accepts it (as the system); an editor may decline it (Skip) while
    # nobody drives; the TTL (`control_wait_s`) declines it structurally (questions=None).
    CONTROL = "control"


class GateAction(StrEnum):
    """The three actions, verbatim from MCP elicitation."""

    ACCEPT = "accept"
    DECLINE = "decline"
    CANCEL = "cancel"


class GateState(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    EXPIRED = "expired"


class BudgetFigures(BaseModel):
    """The money behind a "Budget reached" gate, as the Bursar saw it when the gate opened: the
    ceiling it hit, what that ceiling had spent, and the coming wave's estimate when the wave
    projection opened it (0 when the spend alone reached the ceiling). A raise that resumes the
    wave clears `max(ceiling_usd, spent_usd + projected_usd)`.

    The run guard reads the same shape live (`TaskDeps.spend`, `ai/rules/budget.reached`).
    `exhausted`: the workspace has spent its allowance (ADR-103), which stops the task whatever its
    ceiling - 0 when it has none. An "allowance spent" gate wears it with zero dollars: the floor it
    hit is the workspace's allowance."""

    model_config = ConfigDict(frozen=True)

    ceiling_usd: Decimal
    spent_usd: Decimal
    projected_usd: Decimal = Decimal(0)
    exhausted: bool = False


# The Bursar's scope for a workspace that spent its allowance (ADR-103), beside "task" and "project":
# the gate it opens carries it, and the allowance's wake finds those gates by it.
WORKSPACE_SCOPE: Final = "workspace"


_CENT: Final = Decimal("0.01")


def said_usd(amount: Decimal) -> str:
    """Money as a person reads it in a sentence: cents, or four significant figures under a cent
    ($0.0002541) - never the ledger's twelve decimals. Figures the client computes with stay exact.
    The one home for money in a sentence: the budget gate's reason and the run guard's notice."""
    if amount == 0 or abs(amount) >= _CENT:
        return f"${amount.quantize(_CENT, rounding=ROUND_HALF_UP)}"
    figure = amount.quantize(Decimal(1).scaleb(amount.adjusted() - 3), rounding=ROUND_HALF_UP)
    return f"${figure.normalize():f}"


class GateSpec(BaseModel):
    """A gate node's spec. `questions` is present iff kind == input."""

    model_config = ConfigDict(frozen=True)

    kind: GateKind
    title: str
    questions: QuestionSet | None = None
    reason: str = ""  # budget gates say what would have been overrun; approve gates the evidence
    # CONNECT gates only: the catalog/connector slug the block should offer. Empty
    # when the agent asked for a service nothing matches - the block still renders, honestly,
    # pointing at the connectors page.
    connector_slug: str = ""
    # APPROVE gates only (a browser step): where the ask came from -
    # "connector" (unchanged) or "browser" (the web door). A browser gate also
    # carries the host being asked about and which scopes the human may choose from
    # (`["once", "task", "always"]` for an allowlist ask; `[]` for a one-off danger ask, which
    # keeps today's plain accept/deny). Connector gates keep the empty defaults untouched.
    # CONTROL gates: "handoff" (the agent asked) or "takeover" (a person grabbed it).
    source: str = "connector"
    host: str = ""
    scopes: list[str] = Field(default_factory=list)
    # APPROVE gates only: why the approval rules asked (`schemas/approvals.Rule.why`), each once -
    # the card says it first. Empty on a gate opened before it existed.
    asks_because: list[str] = Field(default_factory=list)
    # BUDGET gates only (B3): which ceiling a "Budget reached" gate hit - "task" (an editor raises
    # it on the gate) or "project" (only the Project's owner can). Empty for the long-form cost
    # confirmation, which is consent, not a ceiling.
    budget_scope: str = ""
    # BUDGET gates only: the Bursar's figures behind "Budget reached". None for the cost
    # confirmation and for a gate parked before the figures existed.
    budget_figures: BudgetFigures | None = None
    # CONTROL takeover gates only: when the lease that opened it was claimed (ISO, the
    # lease's `since`), so only THAT lease's ending answers it - never another lease's late one.
    lease_since: str = ""


class GateResolution(BaseModel):
    """What a human answered. `content` is validated against the QuestionSet (422 on mismatch)."""

    model_config = ConfigDict(frozen=True)

    action: GateAction
    content: dict[str, str | int | float | bool | list[str]] | None = None
    defaulted: list[str] = Field(default_factory=list[str])  # ids in `content` the person skipped: defaults
    resolved_by: str = ""  # the user's display name, for the audit envelope
    # Who, by id: "Bob answered first" compares people, and two people can share a
    # name. None for the clock (timeout, expiry) and for rows resolved before this field.
    resolved_by_id: UUID | None = None
    at: dt.datetime


# ── the plan ──────────────────────────────────────────────────────────────────────────────────


class StepSpec(BaseModel):
    """The JSONB `spec` on a plan_steps row - everything the executor needs and nothing it doesn't.

    ORM rows never reach an agent; this value does.
    """

    model_config = ConfigDict(frozen=True)

    kind: StepKind = StepKind.STEP
    agent: AgentName | None = None  # by registry name - a role ("maker"), never a model
    contract: Contract | None = None
    gate: GateSpec | None = None
    skill_pins: list[SkillPin] = Field(default_factory=list[SkillPin])  # resolved at plan time
    # The bot this step runs on, pinned to a version - empty for a built-in agent.
    # `agent` names the role; this says WHICH version of it, so publishing v2 of a bot never
    # changes what a running task is doing. Same guarantee `skill_pins` gives, one level up.
    bot_pins: list[BotPin] = Field(default_factory=list[BotPin])
    # Why THIS agent for this step, in the user's terms. One line, written by the
    # Lead and carried on the durable record so the routing is legible after the fact -
    # "the contract-reviewer reads clause by clause against our standard terms" beats a name
    # appearing in a tree with no explanation. Rides inside the existing `task.planned` frame,
    # so this adds no wire event variant.
    why: str = ""
    # A fleet seat: the ONE catalog ref this step must be served by, chosen from the
    # enabled catalog at plan time - data, like a skill pin, never a model string in code
    # (Rule 7's intent). None ⇒ route by the agent's model class, as ever.
    model_ref: str | None = None
    # The Lead's plan failed and this step is the one-step floor that runs in its place: why it failed
    # (the Lead past its wall, a proposal that broke a rule). Set only by the floor, never by a model.
    fallback: str = ""
    # A consumer that runs on whatever upstream survived (the fleet's table compares the answers
    # that exist). Promotion still requires at least ONE producer done - comparing nothing is not
    # tolerance, it is an empty table.
    tolerates_missing_inputs: bool = False
    title: str = ""
    # The turn's own message, verbatim (the task's goal on turn 1): every step reads it under its
    # goal, so text the person pasted reaches the step that works on it. Stamped by `write_steps`.
    request: str = ""
    # The step drives the task's browser: an agent that browses per step takes the browser seat for it. Set
    # by code when the step is minted (a site lane, a planned operator step while computer use is on).
    browser: bool = False

    # ── the long-form lane - all defaulted, so every pre-lane JSONB row validates ──
    # unchanged. Set ONLY by the deterministic expansion (`operations/longform`), never a model.
    #
    # What part this step plays in the lane: the writer's outline step (or a consensus
    # proposal feeding the merge), one part, an old row's harmonize pass, or the
    # assembly join. None = not a lane step. One field serving expansion detection, the ask
    # policy, the artifact role, and the web's tree grouping. "express": the
    # frameless one-breath step - no frame, no publish, the narration is the answer.
    # "scatter_map"/"gather"/"synthesis": the wide-and-deep research lane -
    # the map expands, seats harvest citation-backed records, the quorum merge writes.
    # "fleet_table": the fleet's table, laying every seat's answer side by side.
    lane_role: LaneRole | None = None
    # The gather wave this seat belongs to (the iteration law): round 1 is the
    # map's own expansion; re-scatter waves carry 2, 3, … - the tally counts rounds by
    # this stamp, and the round cap bounds the loop beside money and exhaustion.
    scatter_round: int | None = None
    # The seat's FENCE identity, verbatim from the map's SeatBrief - a re-scattered seat
    # carries the SAME id as its round-1 fence (steps stay unique by row id). A field, not
    # a title parse: run 5 found the `{id}r{n}` suffix both overflowing the id cap and
    # colliding with the letter r in hyphenated ids.
    scatter_seat: str | None = None
    # What this step's published artifacts ARE (stamped at harvest, model-unforgeable):
    # `working` = machinery the next step mounts (outline, sections, figures) - never on the
    # shelf, never verified; `deliverable` = the thing the user asked for.
    artifact_role: Literal["deliverable", "working"] = "deliverable"
    # The outline's section count, stamped onto ASSEMBLY so its pre-run guard can hold the
    # 80% floor without re-reading the outline.
    section_total: int | None = None
    # The post-delivery receipt switch: the long-form lane
    # trades the receipt for the pre-delivery QA gate - expansion sets False on assembly.
    # Standard and fleet lanes never touch it. Reversible per task later; the machinery stays.
    verify: bool = True
    # The pre-delivery editorial gate: execute_step's third arm runs the qa agent on
    # this step's deliverable before DONE. Expansion sets it on assembly; planning sets it on
    # a maker's document and deck steps - file deliverables whose rendered LOOK is judged.
    qa: bool = False

    # ── typed joins: what "my upstream is ready" MEANS, declared per node ────────────────────
    # "all"       - every upstream DONE (with `tolerates_missing_inputs` softening it to the
    #               fleet's settled-plus-one-done, exactly as before this field existed).
    # "quorum"    - every upstream SETTLED promotes; the `join_quorum` floor is NOT enforced
    #               here but by the EXECUTOR (`operations/longform.assembly_ready_guard`, over
    #               `section_total`) - the law lives in `operations/execution/joins.py`. Promotion never
    #               holds the floor: a settled wave below quorum has nothing left to run, so a
    #               promotion-time refusal would strand the graph in WAIT forever.
    # "consensus" - every upstream SETTLED promotes (like quorum): the merge judges whatever
    #               proposals survived; a lost proposal costs a voice, never the merge. Even an
    #               all-skipped wave promotes so the merge refuses legibly rather than stranding
    #               the graph in WAIT - the floor is the executor's, per `execution/joins.py`.
    join: Literal["all", "quorum", "consensus"] = "all"
    join_quorum: float | None = Field(default=None, gt=0.0, le=1.0)
    # The ONE upstream this step is FOR (an old row's harmonize pass: its own part). If the primary
    # settles without DONE, this step is SKIPPED at promotion instead of promoted; neighbours are
    # optional context only.
    primary_input: StepId | None = None
    # The done sink this step re-drafts (fence 2): the step whose superseded output
    # this one redoes, and reads read-only from `/task/inputs/`. Set only by
    # fence 2; None everywhere else.
    redrafts: StepId | None = None
    # A long-form revision's kept section: the mounted frame path of the last document's
    # text, which the system publishes as this section byte for byte - the writer's reply is not.
    kept_from: str | None = None


def is_seat(spec: StepSpec) -> bool:
    """A seat keeps a throwaway frame, decided by the plan's shape: a fleet
    seat, a lane section, a scatter seat. With fan-out off these run one after another, and a
    fleet's answers must stay independent either way; `dispatch_frontier` marks the rest."""
    return spec.model_ref is not None or spec.lane_role in ("section", "gather")


class PlanEdge(BaseModel):
    model_config = ConfigDict(frozen=True)

    from_step: StepId
    to_step: StepId
    kind: Literal["depends_on", "produces_for"] = "depends_on"


# ── what the Lead *proposes* ──────────────────────────────────────────────────────────────────
#
# **This is a proposal, not a plan.** A model wrote it, so it may name an agent that does not exist,
# depend on a step it never declared, or describe a cycle. `ai/rules/plan.validate` turns
# it into a plan or refuses - which is why "the Lead produced an invalid graph" is a *test failure*
# here rather than a model behaviour argued with at 3 a.m.
#
# The steps carry **labels**, not ids. A model asked to invent a UUID will invent a UUID, and it
# will be wrong in a way that is tedious to detect; asked for `s1`, it produces `s1`. The policy layer
# mints the real StepIds once the graph is known to be sound.
#
# The fields typed `SkipJsonSchema` are code's: a model never sees them, and the Lead's plan check
# overwrites whatever a model sent in their place (the estimate, the request).

# A step may name a host to open; more is a crawl, which is the scatter recipe's job.
MAX_STEP_HOSTS: Final = 8
# The high end of a step's estimate, the figure a person consents to: the token sizes read low against
# measured runs (research wall time modelled 192 s, measured 309 s; a browser task 35% low). Never a stop.
CEILING_FACTOR: Final = Decimal(2)
_MICRO: Final = Decimal("0.000001")


class DeliverableKind(StrEnum):
    """What a plan hands the person."""

    ANSWER = "answer"  # a reply: the last step's answer is the turn's answer
    CHANGE = "change"  # a code change, as a diff
    ACTION = "action"  # something done on a site or in a connected service
    FILES = "files"  # one or more published files


class AnswerShape(StrEnum):
    """How an answer reads: one exact value, or prose."""

    EXACT = "exact"
    PROSE = "prose"


class RecipeKind(StrEnum):
    """A shape code expands on one step."""

    SCATTER = "scatter"  # a researcher step fanned out by code into parallel seats
    LONGFORM = "longform"  # a writer step written in parts by code


class StepRecipe(BaseModel):
    """A code-expanded shape on one step, never a fan-out the model builds itself."""

    model_config = ConfigDict(frozen=True)

    kind: RecipeKind
    target_count: int | None = Field(
        default=None, ge=1, le=500, description="scatter only: how many items the step must find"
    )
    target_pages: int | None = Field(
        default=None, ge=1, le=400, description="longform only: how many pages the document runs to"
    )


class StepCost(BaseModel):
    """One step's projected spend: its model class, the requests its run is sized for, its dollars at
    its class's prices (0 and `priced=False` when no price is known), and the estimate's high end."""

    model_config = ConfigDict(frozen=True)

    label: str  # the step's label
    model_class: ModelClass
    requests: int = Field(ge=1)
    usd: Decimal
    ceiling_usd: Decimal
    priced: bool = True
    seats: bool = False  # the line prices a scatter step's seats, not the step's own run


class PlanEstimate(BaseModel):
    """What a plan is projected to cost, and the estimate's high end: the steps' high ends, at most the
    budget left on the task. A figure a person is shown, never a stop."""

    model_config = ConfigDict(frozen=True)

    steps: tuple[StepCost, ...]
    usd: Decimal
    ceiling_usd: Decimal

    @classmethod
    def of(cls, steps: tuple[StepCost, ...], budget_left_usd: Decimal | None) -> PlanEstimate:
        """The plan's figures from its steps' figures; a task's budget left caps the high end."""
        usd = sum((s.usd for s in steps), Decimal(0))
        ceiling = sum((s.ceiling_usd for s in steps), Decimal(0))
        if budget_left_usd is not None:
            ceiling = min(ceiling, max(budget_left_usd, Decimal(0)))
        return cls(steps=steps, usd=usd, ceiling_usd=ceiling)


def step_cost(
    label: str,
    model_class: ModelClass,
    units: dict[Unit, int],
    prices: dict[Unit, Decimal],
    *,
    seats: bool = False,
) -> StepCost:
    """One step's figures: `units` (a run's native units, the meter's arithmetic) at its class's `prices`."""
    priced = bool(prices) and all(unit in prices for unit in units if unit is not Unit.REQUEST)
    usd = sum((prices.get(unit, Decimal(0)) * count for unit, count in units.items()), Decimal(0))
    usd = usd.quantize(_MICRO, rounding=ROUND_HALF_UP)
    return StepCost(
        label=label,
        model_class=model_class,
        requests=max(1, units.get(Unit.REQUEST, 1)),
        usd=usd,
        ceiling_usd=usd * CEILING_FACTOR,
        priced=priced,
        seats=seats,
    )


class Seat(BaseModel):
    """An agent a plan may seat, as the plan rules read it: its classes, its tools by model name and what it delivers."""

    model_config = ConfigDict(frozen=True)

    name: str
    model_class: ModelClass
    seat_class: ModelClass  # a scatter step's gather seats run on this class
    tools: tuple[str, ...] = ()
    kinds: tuple[ArtifactType, ...] = ()
    makes_formats: bool = False
    publishes: bool = False
    delivers_outcome: bool = False
    bot: bool = False  # the author's bot, never the floor's seat


class PlanFacts(BaseModel):
    """What code knows that a Lead's plan is checked and priced against, computed by operations per
    turn: values only, never a guess at intent."""

    model_config = ConfigDict(frozen=True)

    request: str = ""  # the turn's own message, verbatim
    seats: tuple[Seat, ...] = ()  # the plannable built-ins, then the author's bots
    # The skills the turn's author may pin, each with the tools it declares it needs (`allowed-tools`).
    skills: dict[str, tuple[str, ...]] = Field(default_factory=dict[str, tuple[str, ...]])
    # Each model class's routed head row, its price per native unit (a search at the deployment's), and the classes that row serves on
    # the flex tier (asked flex, and it prices flex): their runs bill the flex units, as the meter does.
    prices: dict[ModelClass, dict[Unit, Decimal]] = Field(
        default_factory=dict[ModelClass, dict[Unit, Decimal]]
    )
    flex: tuple[ModelClass, ...] = ()


class ProposedStep(BaseModel):
    model_config = ConfigDict(frozen=True)

    label: str = Field(min_length=1, max_length=16, description="a short handle, e.g. 's1'")
    agent: str = Field(min_length=1, description="an agent NAME from the roster you were given")
    title: str = Field(min_length=1, max_length=200)
    goal: str = Field(min_length=1, description="what this step must achieve, on its own")
    acceptance: list[str] = Field(
        default_factory=list, description="what a verifier could CHECK - not aspirations"
    )
    depends_on: list[str] = Field(
        default_factory=list, description="labels of steps whose OUTPUT this step needs"
    )
    publishes: bool = Field(
        default=False,
        description="true if this step must publish a deliverable the user receives",
    )
    filename: str = Field(
        default="",
        max_length=120,
        description="the exact file name the user asked for, e.g. 'sky.md'; empty if they named none",
    )
    produces: ArtifactType | None = Field(
        default=None,
        description="for an agent that makes several kinds of file: the kind this step publishes",
    )
    why: str = Field(
        default="",
        max_length=200,
        description="one short line: why THIS agent for this step, in the user's terms",
    )
    skills: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="skill names from the index whose 'Load when' fits this step; code pins their versions",
    )
    hosts: list[str] = Field(
        default_factory=list,
        description="sites this step must open, as the person wrote them; code decides which it may open",
    )
    browser: bool = Field(
        default=False,
        description=(
            "true only when this step must work on a website in a real browser; false for a connected "
            "service, attached files or an answer"
        ),
    )
    recipe: StepRecipe | None = Field(
        default=None,
        description=(
            "scatter: a researcher step that must find many items across the web; longform: a writer "
            "step for a long document"
        ),
    )

    @field_validator("hosts", mode="before")
    @classmethod
    def _hosts_only(cls, value: object) -> list[str]:
        return list(named_hosts(value, limit=MAX_STEP_HOSTS))


class PlanProposal(BaseModel):
    """A plan as the Lead proposes it. Flat, small, and checkable."""

    model_config = ConfigDict(frozen=True)

    must: list[str] = Field(default_factory=list, description="the task's acceptance criteria")
    tone: str = ""
    audience: str = ""
    steps: list[ProposedStep] = Field(default_factory=list[ProposedStep])
    deliverable: DeliverableKind | None = Field(
        default=None,
        description=(
            "what the plan hands the person: answer (a reply), change (a code diff), action (done on "
            "a site or in a service) or files"
        ),
    )
    answer_shape: AnswerShape = Field(
        default=AnswerShape.PROSE,
        description="for an answer: exact (one value such as a figure, a name or a date) or prose",
    )
    request: SkipJsonSchema[str] = ""
    estimate: SkipJsonSchema[PlanEstimate | None] = None
    # The plan is the floor that runs in place of the Lead's refused plans: the rule they broke.
    fallback: SkipJsonSchema[str] = ""


class PlanStepView(BaseModel):
    """A step as the API and the CLI see it. Never an ORM row."""

    model_config = ConfigDict(frozen=True)

    id: StepId
    title: str
    kind: StepKind
    state: StepState
    agent: AgentName | None = None
    attempt: int = 0
    gate_id: GateId | None = None
    # The step's own contract criteria (the progress tree's static "judged on" list) and,
    # for a fleet seat, its catalog pin as data - both additive.
    acceptance: list[str] = Field(default_factory=list)
    model_ref: str | None = None
    # The lane part this step plays - the web groups the tree by it
    # ("Writing 14 sections - 3 running, 5 done"). None for every non-lane step.
    lane_role: LaneRole | None = None

    # One's reason for choosing this agent. Empty for the deterministic lanes,
    # which pick their agents by rule and so have nothing to explain.
    why: str = ""

    # The bot version this step was pinned to at plan time - the progress tree
    # renders "reviewer v3". None for built-in agents and pre-ADR rows.
    bot_version: int | None = None


class PlanView(BaseModel):
    model_config = ConfigDict(frozen=True)

    steps: list[PlanStepView] = Field(default_factory=list[PlanStepView])
    edges: list[PlanEdge] = Field(default_factory=list[PlanEdge])
    criteria: Criteria = Field(default_factory=Criteria)


class PlanEditKind(StrEnum):
    REPLAN = "replan"  # a step failed and the Planner rewrote the path
    STEER = "steer"  # the user changed their mind; merged at a frontier boundary
    CLARIFY = "clarify"  # intake answers landed
    ASK = "ask"  # a mid-run ask_user inserted a gate
    EXPAND = "expand"  # the outline landed and the long-form lane fanned out (no model)
    HATCH = "hatch"  # stored rows only: a retired answerer escalated and the turn re-planned as work
    LEAD = "lead"  # the Lead ended with a plan: its steps joined the turn (ADR-092)


class PlanEdit(BaseModel):
    """Append-only. A replan is an *edit with history*, never a new plan."""

    model_config = ConfigDict(frozen=True)

    kind: PlanEditKind
    reason: str
    added: list[StepId] = Field(default_factory=list[StepId])
    removed: list[StepId] = Field(default_factory=list[StepId])
    amended: list[StepId] = Field(default_factory=list[StepId])
    at: dt.datetime
    command_id: str | None = None  # a steer's command - the bubble's key for its acks
    epoch: int | None = None  # the epoch a steer opened
