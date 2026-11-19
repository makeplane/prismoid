# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Lead's seams: the facts code knows about a turn, and the typed ways a Lead run ends.

One Lead run decides a turn and ends in exactly one `LeadEnding`: a reply (plain text, which
streams), a plan, one batch of questions, an answer with a proposed task, or (unattended only) a
result for the owner or a typed failure. The run never parks and never waits; code commits the ending (ADR-092). `LeadFacts` is
what intake computes without guessing at intent, and `allowed_endings` is the one pure rule that
narrows the run's output type from those facts.

The `ending` discriminator is code's bookkeeping and the wire value (`step.state.ending`); it is
skipped from each model's JSON schema, so a model writing a tool output never sees or sets it.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.json_schema import SkipJsonSchema

from prismoid.schemas.conversation import MAX_PROPOSAL_GOAL_CHARS, DocumentFormat
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import GateKind, PlanProposal, ProposedQuestion


class LeadEndingKind(StrEnum):
    """How a Lead run ended: the wire field `ending` on the Lead step's terminal `step.state`."""

    REPLY = "reply"
    PLAN = "plan"
    ASK = "ask"
    PROPOSE = "propose"
    FAIL = "fail"
    DELIVER = "deliver"  # an unattended run's result, handed to its owner
    DECLINE = "decline"  # an attended answer that turns the request down or says it cannot be done


class FailKind(StrEnum):
    """Why an unattended run stopped: the typed failure its owner reads."""

    NEEDS_OWNER = "needs_owner"  # a sign-in, a connection or a decision only the owner can give
    UNAVAILABLE = "unavailable"  # a source or a service the work needs did not answer
    REFUSED = "refused"  # the instruction asks for something unsafe or out of bounds


class TurnOrigin(StrEnum):
    """Where a turn came from."""

    WEB = "web"  # a person in the app or the CLI
    API = "api"  # an API key
    ROUTINE = "routine"  # a routine fire (ADR-048)
    CHANNEL = "channel"  # a line on a bot's channel (ADR-056)


# The kinds a deliverable dial accepts (the task door's set): a proposal's Start sets this dial.
DialKind = Literal[
    ArtifactType.DOCUMENT,
    ArtifactType.COLLECTION,
    ArtifactType.SPREADSHEET,
    ArtifactType.DECK,
    ArtifactType.CODE,
]


class LeadFacts(BaseModel):
    """What code knows about one turn before the Lead runs: values only, never a guess at intent."""

    model_config = ConfigDict(frozen=True)

    turn: int = Field(ge=1)
    origin: TurnOrigin
    unattended: bool = False  # nobody can answer a question: a routine fire, an API turn alone
    clarify: Literal["auto", "never", "always"] = "auto"
    asked: bool = False  # this turn already asked once; this run follows the answer
    # A user-sourced deliverable dial the Lead must honour: intake bypasses the Lead when the
    # material is reachable, so a dial seen here needs hands (a plan) or a question.
    deliverable: DialKind | None = None
    deliverable_format: DocumentFormat | None = None
    inputs_unread: int = Field(default=0, ge=0)  # this turn's attachments the Lead did not read inline
    size: str | None = None  # the size the person dialed, as every surface names it ("standard · ~5 pages")
    direct_bot: str | None = None  # a direct thread's Bot: the plan's work step is its own
    budget_left_usd: Decimal | None = None  # None: no ceiling on this task

    def allowed_endings(self) -> frozenset[LeadEndingKind]:
        """The endings this turn's run may take; a plan is always one of them. Unattended, nobody reads
        free text, a question or a card: the run hands its owner a result or a typed failure."""
        if self.clarify == "always" and self.turn == 1 and not (self.asked or self.unattended):
            return frozenset({LeadEndingKind.ASK})
        allowed = {LeadEndingKind.PLAN}
        answers = self.deliverable is None and not self.inputs_unread
        if self.unattended:
            allowed.add(LeadEndingKind.FAIL)
            if answers:
                allowed.add(LeadEndingKind.DELIVER)
            return frozenset(allowed)
        allowed.add(LeadEndingKind.DECLINE)  # a refusal or a cannot-do is never work, whatever was dialed
        if answers:
            allowed |= {LeadEndingKind.REPLY, LeadEndingKind.PROPOSE}
        if self.clarify != "never" and not self.asked:
            allowed.add(LeadEndingKind.ASK)
        return frozenset(allowed)


class Reply(BaseModel):
    """The text the Lead wrote, which streamed as it was written: the turn's answer."""

    model_config = ConfigDict(frozen=True)

    ending: SkipJsonSchema[Literal[LeadEndingKind.REPLY]] = LeadEndingKind.REPLY
    text: str = Field(min_length=1)


class SubmitPlan(BaseModel):
    """End the turn with a plan when the outcome needs hands or pages read: steps run after you, and the
    person reads `say` now."""

    model_config = ConfigDict(frozen=True)

    ending: SkipJsonSchema[Literal[LeadEndingKind.PLAN]] = LeadEndingKind.PLAN
    say: str = Field(
        min_length=1,
        max_length=300,
        description="the one line the person reads now: what happens next, in their terms",
    )
    plan: PlanProposal

    @model_validator(mode="after")
    def _has_steps(self) -> SubmitPlan:
        if not self.plan.steps:
            raise ValueError("a plan needs at least one step; to answer, reply with text instead")
        return self


# The most questions one Lead ask may hold: past this, the batch is a form, not the few things only the person has.
MAX_LEAD_QUESTIONS: Final = 3


class AskUser(BaseModel):
    """End the turn with one batch of questions, or one service to connect, when a wrong guess would ruin
    the work; the answer starts a new run."""

    model_config = ConfigDict(frozen=True)

    ending: SkipJsonSchema[Literal[LeadEndingKind.ASK]] = LeadEndingKind.ASK
    kind: Literal[GateKind.INPUT, GateKind.CONNECT] = GateKind.INPUT
    context: str = Field(
        min_length=1, max_length=300, description="one sentence the person reads first: why you ask"
    )
    questions: list[ProposedQuestion] = Field(
        default_factory=list[ProposedQuestion],
        max_length=MAX_LEAD_QUESTIONS,
        description=(
            "kind=input: 1-3 questions, only what a wrong guess would ruin; in a batch of two or more, what "
            "has a safe default is an assumption you state in `context`, never a question"
        ),
    )
    service: str = Field(
        default="", max_length=48, description="kind=connect: the service to connect, e.g. 'gmail'"
    )

    @model_validator(mode="after")
    def _one_shape(self) -> AskUser:
        if self.kind is GateKind.INPUT and (not self.questions or self.service):
            raise ValueError("kind=input asks 1-3 questions and names no service")
        if self.kind is GateKind.CONNECT and (self.questions or not self.service.strip()):
            raise ValueError("kind=connect names one service and asks no questions")
        return self


class ProposeTask(BaseModel):
    """End the turn with your full answer and a task the person can start with one tap, when a file would
    serve them better than the answer."""

    model_config = ConfigDict(frozen=True)

    ending: SkipJsonSchema[Literal[LeadEndingKind.PROPOSE]] = LeadEndingKind.PROPOSE
    answer: str = Field(min_length=1, description="your answer to what was asked, in full")
    goal: str = Field(
        min_length=1,
        max_length=MAX_PROPOSAL_GOAL_CHARS,
        description="the work, self-contained: what to produce and what good looks like",
    )
    deliverable: DialKind
    format: DocumentFormat | None = Field(
        default=None, description="a document's file format, when the person named one or one fits best"
    )

    @model_validator(mode="after")
    def _format_is_a_document(self) -> ProposeTask:
        if self.format is not None and self.deliverable is not ArtifactType.DOCUMENT:
            raise ValueError("format belongs to deliverable=document only")
        return self


class Fail(BaseModel):
    """Only when nobody is there to answer: the work cannot be done on its own, so the run stops and its
    owner reads why."""

    model_config = ConfigDict(frozen=True)

    ending: SkipJsonSchema[Literal[LeadEndingKind.FAIL]] = LeadEndingKind.FAIL
    kind: FailKind = Field(
        description="needs_owner: only the owner can unblock it (sign in, connect, decide); unavailable: a "
        "source or service did not answer; refused: the instruction is unsafe or out of bounds"
    )
    reason: str = Field(min_length=1, max_length=300, description="one line the owner reads: what stopped it")


class Decline(BaseModel):
    """End the turn by turning the request down, or by saying it cannot be done here: the person reads
    `answer` in full, and the task ends as could-not-do instead of done."""

    model_config = ConfigDict(frozen=True)

    ending: SkipJsonSchema[Literal[LeadEndingKind.DECLINE]] = LeadEndingKind.DECLINE
    why: Literal["refused", "cannot"] = Field(
        description="refused: you will not do it (unsafe, harmful, someone's privacy); cannot: it cannot be "
        "done with the tools and services here"
    )
    answer: str = Field(
        min_length=1,
        description="what the person reads: that you won't or can't, why, and what they can do instead",
    )


class Deliver(BaseModel):
    """Only when nobody is there to answer: the run's result, handed to its owner."""

    model_config = ConfigDict(frozen=True)

    ending: SkipJsonSchema[Literal[LeadEndingKind.DELIVER]] = LeadEndingKind.DELIVER
    result: str = Field(min_length=1, description="the result in full, as the owner will read it (markdown)")
    findings: bool = Field(
        description="true when this run found something the owner should hear about; false when there is "
        "nothing new, and the result says so"
    )


LeadEnding = Annotated[
    Reply | SubmitPlan | AskUser | ProposeTask | Fail | Deliver | Decline, Field(discriminator="ending")
]


class PlanRevision(BaseModel):
    """A steer changes the work still to do: `plan`'s steps replace every step not yet started (none drops
    them), the done work stands and feeds the first new steps, and the person reads `say`."""

    model_config = ConfigDict(frozen=True)

    say: str = Field(
        min_length=1, max_length=300, description="the one line the person reads now: what changes"
    )
    plan: PlanProposal = Field(
        description="the steps that replace the ones not yet started, never a done one again; no steps drops them"
    )


class KeepPlan(BaseModel):
    """The steps not yet started stand: the plan already follows the steer, or the steer cannot change them."""

    model_config = ConfigDict(frozen=True)

    reason: str = Field(min_length=1, max_length=200, description="one line: why the plan stands")
