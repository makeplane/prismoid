# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Intake: what code knows about one turn, read once from the rows, before any model is asked.

Facts only: the turn's dials, its attachments, where it came from, the clarify dial, the budget left
and whether the turn already asked. No text is read for meaning; the Lead decides intent (ADR-092).
Two pure rules sit on the facts: `bypass_of`, the turns a code-made plan answers with no Lead call
(a fleet or a direct Bot on turn 1, a long-form size dial, a deliverable dial whose material the one
dialed step reaches), and `LeadFacts.allowed_endings` for every other turn.

`read_rows` is the only I/O; `intake_of` is pure, so every rule is a row in, facts out. The turn's own
record says the rest: the credential kind that sent it and the files it attached ride `task.created`
and `task.continued` (a frame from before those fields reads as a web turn that attached nothing).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final, get_args

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.tools.convert import CONVERTS
from prismoid.db.models.conversation import Conversation
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.task import Task
from prismoid.operations import usage as usage_ops
from prismoid.operations.gate_budget import budget_of
from prismoid.operations.turn import recipes
from prismoid.schemas import sizing
from prismoid.schemas.events import GateOpened, TaskContinued, TaskCreated
from prismoid.schemas.inputs import TaskInput
from prismoid.schemas.lead import DialKind, LeadFacts, TurnOrigin
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import GateKind, TaskMode
from prismoid.schemas.principal import CredKind
from prismoid.schemas.sizing import SizeBucket

_DIAL_KINDS: Final[tuple[DialKind, ...]] = get_args(DialKind)
# The gates that are the turn asking the person: one batch of questions, or one service to connect.
_ASKING: Final = frozenset({GateKind.INPUT, GateKind.CONNECT})
_FLEET_MODES: Final = frozenset({TaskMode.FLEET, TaskMode.COUNCIL})  # council: legacy fleet rows
_HTML: Final = "html"  # a page never runs the long-form lane: there is no section walker for it
# A file past this is never opened for the Lead: the work of reading it is a step's, whatever it holds.
READ_MAX_BYTES: Final = 5_000_000


class Bypass(StrEnum):
    """The code-made plan that answers a turn with no Lead call."""

    DIRECT = "direct"  # a direct Bot's first turn is the Bot's own work
    FLEET = "fleet"  # one seat per model, then the chair's table
    LONGFORM = "longform"  # a size dial the long-form lane serves: outline, sections, assembly
    DIAL = "dial"  # a deliverable dial whose material the one dialed step reaches


@dataclass(frozen=True, slots=True)
class TurnRows:
    """The values intake reads for one turn: plain row values, no ORM row and no message text."""

    turn: int
    mode: TaskMode
    clarify: str
    from_routine: bool  # a routine minted the task
    from_channel: bool  # the task was born in a conversation a channel carries
    from_api: bool  # THIS turn's sender used an API key
    direct_bot: str | None
    bot_pins: bool  # the task pins Bots its plan may seat
    connectors: bool  # the task holds connectors
    dial: str | None  # the deliverable kind THIS turn's person dialed
    carried_dial: str | None  # the deliverable kind a person dialed on any turn: the row's user stamp
    deliverable_format: str | None
    scale: str | None  # THIS turn's size dial, raw ("auto" or None: untouched)
    target_pages: int | None  # THIS turn's explicit unit count
    inputs: tuple[TaskInput, ...]  # THIS turn's attachments
    budget_usd: Decimal | None  # the task's ceiling
    spent_usd: Decimal
    asked: bool  # a question or a connect gate opened since the turn began


@dataclass(frozen=True, slots=True)
class Intake:
    """One turn's facts, and the code-made plan that answers it instead of the Lead, if any."""

    facts: LeadFacts
    bypass: Bypass | None
    inputs: tuple[TaskInput, ...] = ()  # this turn's attachments, which the Lead's rung reads


def dial_kind(value: str | None) -> DialKind | None:
    """A stored deliverable value as a dial kind; None for no dial or a kind no dial offers."""
    return next((kind for kind in _DIAL_KINDS if kind == value), None)


def origin_of(rows: TurnRows) -> TurnOrigin:
    """Where the turn came from: a routine or a channel starts turn 1; else an API key or a person in the app."""
    if rows.turn == 1 and rows.from_routine:
        return TurnOrigin.ROUTINE
    if rows.turn == 1 and rows.from_channel:
        return TurnOrigin.CHANNEL
    return TurnOrigin.API if rows.from_api else TurnOrigin.WEB


def turn_kind(rows: TurnRows) -> DialKind | None:
    """The deliverable kind a person dialed for this turn: this turn's dial, else one an earlier turn carried."""
    return dial_kind(rows.dial) or dial_kind(rows.carried_dial)


def turn_size(rows: TurnRows) -> tuple[SizeBucket, int | None] | None:
    """This turn's own size dial as a bucket and a count clamped to it; None when the turn dialed no size."""
    return recipes.dialed_size(rows.scale, turn_kind(rows), rows.target_pages)


def sized_for_sections(rows: TurnRows) -> bool:
    """Does this turn's own size dial put the deliverable on the long-form lane?"""
    size = turn_size(rows)
    if size is None or rows.deliverable_format == _HTML:
        return False
    return sizing.runs_longform(turn_kind(rows), *size)


def bypass_of(rows: TurnRows) -> Bypass | None:
    """The code-made plan for this turn, or None when the Lead decides it."""
    if rows.turn == 1 and rows.direct_bot:
        return Bypass.DIRECT
    if rows.turn == 1 and rows.mode in _FLEET_MODES:
        return Bypass.FLEET
    if rows.mode is not TaskMode.STANDARD or rows.direct_bot:
        return None  # a fleet thread or a Bot's thread: its later turns are the Lead's
    if sized_for_sections(rows):
        return Bypass.LONGFORM
    if dial_kind(rows.dial) is not None and not (rows.connectors or rows.bot_pins):
        return Bypass.DIAL  # the dialed step reads files and the web; a service or a Bot needs a plan
    return None


def lead_opens(item: TaskInput) -> bool:
    """Whether the Lead's rung tries to read an input: text, or a document the sidecar converts."""
    return item.bytes <= READ_MAX_BYTES and (item.is_text or item.media_type in CONVERTS)


def facts_of(rows: TurnRows) -> LeadFacts:
    """The facts the Lead runs on; the stored vocabularies (clarify, format) are validated here."""
    origin = origin_of(rows)
    kind = dial_kind(rows.dial)
    left = None if rows.budget_usd is None else max(rows.budget_usd - rows.spent_usd, Decimal(0))
    return LeadFacts.model_validate(
        {
            "turn": rows.turn,
            "origin": origin,
            "unattended": origin is TurnOrigin.ROUTINE,
            "clarify": rows.clarify,
            "asked": rows.asked,
            "deliverable": kind,
            "deliverable_format": rows.deliverable_format if kind is ArtifactType.DOCUMENT else None,
            "inputs_unread": sum(not lead_opens(i) for i in rows.inputs),  # the rung adds what did not fit
            "size": None if (size := turn_size(rows)) is None else sizing.size_marker(turn_kind(rows), *size),
            "direct_bot": rows.direct_bot,
            "budget_left_usd": left,
        }
    )


def intake_of(rows: TurnRows) -> Intake:
    """One turn's rows in, its facts and its bypass out."""
    return Intake(facts=facts_of(rows), bypass=bypass_of(rows), inputs=rows.inputs)


async def read_rows(session: AsyncSession, task: Task) -> TurnRows:
    """Read one turn's values; commits nothing and takes no lock."""
    opened = await TaskEvent.latest_of_type(
        session, task.id, "task.continued" if task.turn > 1 else "task.created"
    )
    continued = opened if isinstance(opened, TaskContinued) else None
    credential = opened.credential if isinstance(opened, TaskCreated | TaskContinued) else None
    since = continued.seq if continued is not None else (0 if task.turn == 1 else task.event_seq)
    asked = any(
        isinstance(event, GateOpened) and event.kind in _ASKING
        for event in await TaskEvent.of_types(session, task.id, ("gate.opened",), since=since)
    )
    conversation = (
        await session.get(Conversation, task.origin_conversation_id) if task.origin_conversation_id else None
    )
    return TurnRows(
        turn=task.turn,
        mode=TaskMode(task.mode),
        clarify=task.clarify,
        from_routine=task.origin_routine_id is not None,
        from_channel=conversation is not None and conversation.channel_id is not None,
        from_api=credential == CredKind.API_KEY,
        direct_bot=task.direct_bot,
        bot_pins=bool(task.bot_pins),
        connectors=bool(task.connector_ids),
        dial=_turn_dial(task, continued),
        carried_dial=task.user_deliverable,
        deliverable_format=task.deliverable_format,
        scale=task.scale if task.turn == 1 else (continued.scale if continued else None),
        target_pages=task.target_pages if task.turn == 1 else (continued.target_pages if continued else None),
        inputs=_turn_inputs(task, continued),
        budget_usd=budget_of(task.budget),
        spent_usd=(await usage_ops.for_task(session, task.id)).total_usd,
        asked=asked,
    )


def _turn_inputs(task: Task, continued: TaskContinued | None) -> tuple[TaskInput, ...]:
    """THIS turn's attachments: every input on turn 1, the files a follow-up attached on a later one."""
    inputs = map(TaskInput.model_validate, task.inputs)
    if task.turn == 1:
        return tuple(inputs)
    named = set(continued.attached or ()) if continued is not None else set()
    return tuple(i for i in inputs if i.filename in named)


def _turn_dial(task: Task, continued: TaskContinued | None) -> str | None:
    """The kind this turn dialed: turn 1's user stamp, or a follow-up's kind or format dial."""
    if task.turn == 1:
        return task.user_deliverable
    if continued is None:
        return None
    if continued.deliverable is not None:
        return continued.deliverable
    return task.deliverable if continued.deliverable_format is not None else None


async def intake(session: AsyncSession, task: Task) -> Intake:
    """The turn's facts and bypass, read from the rows; commits nothing and takes no lock."""
    return intake_of(await read_rows(session, task))
