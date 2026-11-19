# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The dataset row: what a person should get, and what it may cost - never which door fires.

A row says what the user receives (`expected_outcome`), what else is acceptable
(`allowed_outcomes`), how much work it may take (the limits) and which deterministic `checks` the
answer must pass. It never names a rule, a lane or an agent: `route` and `route_source` are
diagnostic on the trace and nothing here scores them, so the datasets survive every rewrite of the
code that decides.

Every vocabulary is an enum declared here once; a row that says something outside it fails to load.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class Outcome(StrEnum):
    """What the user got, in the order of the misroute-cost matrix (`metrics.MISROUTE_COST`)."""

    REPLY = "reply"  # answered in text, no tool
    LIGHT_WORK = "light_work"  # answered itself, using read tools only: search, fetch, memory, compute
    PLAN = "plan"  # a plan ran: files, research at breadth, a service write
    PLAN_WITH_MACHINE = "plan_with_machine"  # a plan ran and a step drove a browser or a shell
    ASK = "ask"  # the turn stopped to ask a question
    PROPOSE = "propose"  # the turn proposed work (a card or a paused routine) and started nothing


class Unscored(StrEnum):
    """Labels that are not routing outcomes. A row carrying one is never scored."""

    REJECT = "reject"  # the API refused the ask (HTTP 422): no task, no route
    MECHANISM = "mechanism"  # a row about routing mechanics, which scripted-loop tests own


class Surface(StrEnum):
    TASK = "task"
    CONVERSATION = "conversation"


class Reach(StrEnum):
    """What the deployment can reach, for the rows that say its reach is limited."""

    SEARCH = "search"
    NONE = "none"
    DEVICE = "device"
    MACHINE = "machine"


class RowGroup(StrEnum):
    TRIVIAL = "trivial"  # a talk-sized ask that must be a reply
    TRAP = "trap"  # looks like talk but needs a tool


class OptionalMachine(StrEnum):
    """`expected_machines` for a plan that may or may not lease one."""

    ZERO_OR_ONE = "0-1"


# Strict, because a lax integer parse reads the string "0-1" as -1.
type MachineCount = Annotated[int, Field(strict=True, ge=0)] | OptionalMachine


class CheckKind(StrEnum):
    """The deterministic check vocabulary. A check reads the trace; none reads the user's text."""

    # text
    CONTAINS_ALL = "contains_all"
    CONTAINS_ANY = "contains_any"
    CONTAINS_IN_ORDER = "contains_in_order"
    LINES_EQUAL = "lines_equal"
    MAX_WORDS = "max_words"
    MAX_CHARS = "max_chars"
    MAX_SENTENCES = "max_sentences"
    LANGUAGE = "language"
    CODE_BLOCK = "code_block"
    REGEX_ACCEPTS = "regex_accepts"
    # shape
    MIN_BULLETS = "min_bullets"
    BULLETS_EXACT = "bullets_exact"
    NUMBERED_LIST = "numbered_list"
    NUMBERED_OR_BULLETS = "numbered_or_bullets"
    MARKDOWN_TABLE = "markdown_table"
    SLACK_MRKDWN = "slack_mrkdwn"
    BOLD = "bold"
    SHORTER_THAN_PRIOR = "shorter_than_prior"
    # trace
    NO_TOOL_CALLS = "no_tool_calls"
    TOOL_CALLED = "tool_called"
    NO_FETCH = "no_fetch"
    NO_CONNECTOR_CALL = "no_connector_call"
    NO_CONNECTOR_ATTACH = "no_connector_attach"
    CITES_SOURCE = "cites_source"
    NO_MACHINE = "no_machine"
    MACHINE_STARTED_OR_GATE = "machine_started_or_gate"
    NO_ARTIFACT = "no_artifact"
    ARTIFACT = "artifact"
    XLSX_CELL_EQUALS = "xlsx_cell_equals"
    MEMORY_WRITTEN = "memory_written"
    APPROVAL_GATE_OR_POST = "approval_gate_or_post"
    EVENT = "event"
    NEVER = "never"
    ASKS = "asks"


class _Spec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ContainsAll(_Spec):
    type: Literal[CheckKind.CONTAINS_ALL] = CheckKind.CONTAINS_ALL
    value: tuple[str, ...]


class ContainsAny(_Spec):
    type: Literal[CheckKind.CONTAINS_ANY] = CheckKind.CONTAINS_ANY
    value: tuple[str, ...]


class ContainsInOrder(_Spec):
    type: Literal[CheckKind.CONTAINS_IN_ORDER] = CheckKind.CONTAINS_IN_ORDER
    value: tuple[str, ...]


class LinesEqual(_Spec):
    type: Literal[CheckKind.LINES_EQUAL] = CheckKind.LINES_EQUAL
    value: tuple[str, ...]


class MaxWords(_Spec):
    type: Literal[CheckKind.MAX_WORDS] = CheckKind.MAX_WORDS
    value: int


class MaxChars(_Spec):
    type: Literal[CheckKind.MAX_CHARS] = CheckKind.MAX_CHARS
    value: int


class MaxSentences(_Spec):
    type: Literal[CheckKind.MAX_SENTENCES] = CheckKind.MAX_SENTENCES
    value: int


class Language(_Spec):
    type: Literal[CheckKind.LANGUAGE] = CheckKind.LANGUAGE
    value: str  # an ISO 639-1 code


class CodeBlock(_Spec):
    type: Literal[CheckKind.CODE_BLOCK] = CheckKind.CODE_BLOCK


class RegexAccepts(_Spec):
    type: Literal[CheckKind.REGEX_ACCEPTS] = CheckKind.REGEX_ACCEPTS
    pattern_from: str
    accept: tuple[str, ...]
    reject: tuple[str, ...]


class MinBullets(_Spec):
    type: Literal[CheckKind.MIN_BULLETS] = CheckKind.MIN_BULLETS
    value: int


class BulletsExact(_Spec):
    type: Literal[CheckKind.BULLETS_EXACT] = CheckKind.BULLETS_EXACT
    value: int


class NumberedList(_Spec):
    type: Literal[CheckKind.NUMBERED_LIST] = CheckKind.NUMBERED_LIST
    min: int


class NumberedOrBullets(_Spec):
    type: Literal[CheckKind.NUMBERED_OR_BULLETS] = CheckKind.NUMBERED_OR_BULLETS
    min: int


class MarkdownTable(_Spec):
    type: Literal[CheckKind.MARKDOWN_TABLE] = CheckKind.MARKDOWN_TABLE
    columns: tuple[str, ...] | None = None
    rows: int | None = None


class SlackMrkdwn(_Spec):
    type: Literal[CheckKind.SLACK_MRKDWN] = CheckKind.SLACK_MRKDWN


class Bold(_Spec):
    type: Literal[CheckKind.BOLD] = CheckKind.BOLD
    value: tuple[str, ...]


class ShorterThanPrior(_Spec):
    type: Literal[CheckKind.SHORTER_THAN_PRIOR] = CheckKind.SHORTER_THAN_PRIOR


class NoToolCalls(_Spec):
    type: Literal[CheckKind.NO_TOOL_CALLS] = CheckKind.NO_TOOL_CALLS


class ToolCalled(_Spec):
    type: Literal[CheckKind.TOOL_CALLED] = CheckKind.TOOL_CALLED
    value: str


class NoFetch(_Spec):
    type: Literal[CheckKind.NO_FETCH] = CheckKind.NO_FETCH


class NoConnectorCall(_Spec):
    type: Literal[CheckKind.NO_CONNECTOR_CALL] = CheckKind.NO_CONNECTOR_CALL


class NoConnectorAttach(_Spec):
    type: Literal[CheckKind.NO_CONNECTOR_ATTACH] = CheckKind.NO_CONNECTOR_ATTACH


class CitesSource(_Spec):
    type: Literal[CheckKind.CITES_SOURCE] = CheckKind.CITES_SOURCE


class NoMachine(_Spec):
    type: Literal[CheckKind.NO_MACHINE] = CheckKind.NO_MACHINE


class MachineStartedOrGate(_Spec):
    type: Literal[CheckKind.MACHINE_STARTED_OR_GATE] = CheckKind.MACHINE_STARTED_OR_GATE
    value: bool = True


class NoArtifact(_Spec):
    type: Literal[CheckKind.NO_ARTIFACT] = CheckKind.NO_ARTIFACT


class Artifact(_Spec):
    type: Literal[CheckKind.ARTIFACT] = CheckKind.ARTIFACT
    ext: str


class XlsxCellEquals(_Spec):
    type: Literal[CheckKind.XLSX_CELL_EQUALS] = CheckKind.XLSX_CELL_EQUALS
    sheet: int
    cell: str
    value: int | float | str


class MemoryWritten(_Spec):
    type: Literal[CheckKind.MEMORY_WRITTEN] = CheckKind.MEMORY_WRITTEN
    contains: str


class ApprovalGateOrPost(_Spec):
    type: Literal[CheckKind.APPROVAL_GATE_OR_POST] = CheckKind.APPROVAL_GATE_OR_POST
    tool: str


class Event(_Spec):
    type: Literal[CheckKind.EVENT] = CheckKind.EVENT
    value: str


class Never(_Spec):
    """A judge criterion: said in words, decided by a calibrated judge or a person."""

    type: Literal[CheckKind.NEVER] = CheckKind.NEVER
    value: str


class Asks(_Spec):
    """A judge criterion: the conversation reply asks this. A reply that passes it counts as `ask`."""

    type: Literal[CheckKind.ASKS] = CheckKind.ASKS
    value: str


type Check = Annotated[
    ContainsAll
    | ContainsAny
    | ContainsInOrder
    | LinesEqual
    | MaxWords
    | MaxChars
    | MaxSentences
    | Language
    | CodeBlock
    | RegexAccepts
    | MinBullets
    | BulletsExact
    | NumberedList
    | NumberedOrBullets
    | MarkdownTable
    | SlackMrkdwn
    | Bold
    | ShorterThanPrior
    | NoToolCalls
    | ToolCalled
    | NoFetch
    | NoConnectorCall
    | NoConnectorAttach
    | CitesSource
    | NoMachine
    | MachineStartedOrGate
    | NoArtifact
    | Artifact
    | XlsxCellEquals
    | MemoryWritten
    | ApprovalGateOrPost
    | Event
    | Never
    | Asks,
    Field(discriminator="type"),
]


class PriorTurn(BaseModel):
    """One earlier turn of the thread a follow-up row continues."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: Literal["user", "assistant"]
    text: str


class RowContext(BaseModel):
    """The facts a row sets up before the ask: where it is said, what is attached, the dials."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    surface: Surface = Surface.TASK
    turn: int = 1
    locale: str | None = None
    connectors: tuple[str, ...] = ()  # slugs of the connectors attached live
    inputs: tuple[str, ...] = ()  # attached files, by repo path
    turns: tuple[str, ...] = ()  # earlier user turns played before this one
    dials: dict[str, str] = Field(default_factory=dict[str, str])  # deliverable, mode, scale, clarify
    facts: dict[str, str | int | bool] = Field(default_factory=dict[str, str | int | bool])
    prior_host: str | None = None  # the site the thread's last work drove
    transact: bool = False
    deployment: str | None = None  # a deployment condition, in words
    deployment_reach: Reach | None = None


class RowBudget(BaseModel):
    """What one trial may spend; a dearer trial fails. Latency is judged over the suite, never per row."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    usd_max: float | None = None


class OutcomeRow(BaseModel):
    """One case. Limits left unset are not checked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: Annotated[str, Field(min_length=1)]
    message: str
    context: RowContext = Field(default_factory=RowContext)
    prior_turns: tuple[PriorTurn, ...] = ()

    expected_outcome: Outcome | Unscored
    allowed_outcomes: tuple[Outcome, ...] = ()

    max_calls_before_delivery: int | None = None
    max_tool_calls: int | None = None
    min_tool_calls: int | None = None
    expected_tools: tuple[str, ...] = ()
    max_machines: int | None = None
    expected_machines: MachineCount | None = None
    max_artifacts: int | None = None
    budget: RowBudget | None = None

    trap_words: tuple[str, ...] = ()  # the words in the message that tempt a keyword rule
    checks: tuple[Check, ...] = ()
    judge_rubric: str | None = None
    # What a read tool answers in the decision runner, by the name the model calls it (`web_search`).
    fixtures: dict[str, str] = Field(default_factory=dict[str, str])

    group: RowGroup | None = None
    category: str | None = None
    variant: str | None = None

    # where the row came from and how it was relabelled
    source: str | None = None
    source_id: str | None = None
    today_label: str | None = None
    label_changed: bool | None = None
    rule_encoded: bool = False
    rule_note: str = ""
    duplicate_of: str | None = None

    # who vouched for the label, and when: set by a label review, empty on a row nobody has reviewed
    reviewed_by: str | None = None
    label_note: str | None = None  # why the label and its allowed set are what they are
    fresh_as_of: date | None = None  # the day a reviewer last checked the row against the world

    @property
    def scored(self) -> bool:
        """Only a row that expects a routing outcome is scored."""
        return isinstance(self.expected_outcome, Outcome)

    @property
    def dedupe_key(self) -> str:
        """Rows that are the same ask share one key, so one ask is not counted twice."""
        return self.duplicate_of or self.id


def load_rows(path: Path) -> list[OutcomeRow]:
    """The rows of one JSONL file, validated; a bad line or a repeated id names itself."""
    rows: list[OutcomeRow] = []
    seen: set[str] = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = OutcomeRow.model_validate_json(line)
        except ValueError as error:
            raise ValueError(f"{path}:{number}: {error}") from error
        if row.id in seen:
            raise ValueError(f"{path}:{number}: duplicate id {row.id!r}")
        seen.add(row.id)
        rows.append(row)
    return rows
