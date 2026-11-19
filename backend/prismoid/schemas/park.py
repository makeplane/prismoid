# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A parked step's record: what a park saves on `plan_steps.resume_state["park"]` (ADR-089). The run
itself is on the step's call log; the record names the park's kind, the gate it waits on and how far
the run was told, so the resume replays the run and answers the deferred calls its replay ends on.
`OwedRun` is the record's other run shape, under `resume_state["owed_run"]`: what a dead run owes the
step's next attempt.

A park saved before the call log (v1) carries the run's `history`, the ids its resume answers and no
record. `V1Park.of` is the one decoder of those keys: such a park resumes from its saved conversation
until those parks are gone.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, cast

from pydantic import BaseModel, ConfigDict

from prismoid.schemas.failures import Pickup
from prismoid.schemas.steering import SteerNote

PARK: Final = "park"  # the record's key on `resume_state`
# What a guarded run's death owes the step's next attempt (`OwedRun`): kept with the record, like the
# owed cut, through every park, pace and replacement of the run's state, until an attempt's
# `step.prompt` commits - so a rung that dies before its run, or a park between rungs, never loses it.
OWED_RUN: Final = "owed_run"


class ParkKind(StrEnum):
    QUESTION = "question"
    CONNECT = "connect"
    APPROVAL = "approval"
    PAUSE = "pause"
    CONTROL = "control"
    BUDGET = "budget"


class ParkRecord(BaseModel):
    """One park: its kind, its gate (None: the call was answered in place, no gate opened), the
    newest steer epoch the run was told, the browser's `once` widens it carries to the resume, and who
    paused the task when a pause deferred calls of the parked turn."""

    model_config = ConfigDict(frozen=True)

    kind: ParkKind
    gate_id: uuid.UUID | None
    told_epoch: int = 0
    # The live allowlist less what its homes vouched for at the park: neither the replayed run nor
    # the gate's resolution holds it.
    browser_once: tuple[str, ...] = ()
    paused_by: str = ""

    def saved(self) -> dict[str, object]:
        """The record as its `resume_state` share."""
        return {PARK: self.model_dump(mode="json")}


class OwedRun(BaseModel):
    """How a guarded step's next attempt takes up the run that died on its call log (`pickup`: re-enter
    it, or continue it with one line), why it stopped, and the steers taken at a rung's start since: a
    continued run never hears its contract again, so it is told them instead."""

    model_config = ConfigDict(frozen=True)

    pickup: Pickup
    reason: str = ""
    steers: tuple[SteerNote, ...] = ()

    def owe(self, taken: Sequence[SteerNote]) -> OwedRun:
        """With the steers a rung took at its start, once each."""
        known = {note.command_id for note in self.steers}
        return self.model_copy(
            update={"steers": (*self.steers, *(n for n in taken if n.command_id not in known))}
        )


def park_of(state: Mapping[str, object]) -> ParkRecord:
    """The park a resume state saved; a v1 park's decoded from the keys it saved instead."""
    saved = state.get(PARK)
    if saved is not None:
        return ParkRecord.model_validate(saved)
    return _v1_record(state)


def parked_kind(state: Mapping[str, object]) -> ParkKind | None:
    """The kind of the park a state holds, or None for a state that holds no park."""
    if PARK in state or _V1_HISTORY in state:
        return park_of(state).kind
    return None


# A v1 park's dumped conversation, and its kind by the first of its keys it saved (the wake's order).
_V1_HISTORY: Final = "history"
_V1_KINDS: Final = (
    ("control", ParkKind.CONTROL),
    ("budget_calls", ParkKind.BUDGET),
    ("connect", ParkKind.CONNECT),
    ("pause_calls", ParkKind.PAUSE),
    ("approval_calls", ParkKind.APPROVAL),
)


def _v1_record(state: Mapping[str, object]) -> ParkRecord:
    kind = next((kind for key, kind in _V1_KINDS if key in state), ParkKind.QUESTION)
    told = state.get("told")
    return ParkRecord(
        kind=kind,
        gate_id=None,
        told_epoch=told if isinstance(told, int) else 0,
        browser_once=_ids(state, "browser_once"),
        paused_by=str(state.get("paused_by") or ""),
    )


@dataclass(frozen=True, slots=True)
class V1Park:
    """A park saved before the call log, decoded: its record, its conversation (`history`, the
    library's JSON stored latin-1) and the ids of the deferred calls its resume answers."""

    record: ParkRecord
    history: str
    own: str | None  # the call it parked on
    extra_calls: tuple[str, ...]
    extra_asks: tuple[str, ...]
    denied_approvals: tuple[str, ...]
    paused_calls: tuple[str, ...]  # a pause park's own calls, or those a pause deferred beside it
    held_calls: tuple[str, ...]
    held_approvals: tuple[str, ...]
    approval_calls: dict[str, dict[str, object]]  # id → {connector, tool, fingerprint}
    budget_calls: tuple[str, ...]
    budget_asks: tuple[str, ...]
    budget_approvals: tuple[str, ...]

    @classmethod
    def of(cls, state: Mapping[str, object]) -> V1Park | None:
        """The v1 park a resume state holds, or None for a park saved on the call log."""
        if PARK in state or _V1_HISTORY not in state:
            return None
        own = state.get("tool_call_id")
        approvals = cast(dict[str, object], state.get("approval_calls") or {})
        return cls(
            record=_v1_record(state),
            history=str(state[_V1_HISTORY]),
            own=None if own is None else str(own),
            extra_calls=_ids(state, "extra_calls"),
            extra_asks=_ids(state, "extra_asks"),
            denied_approvals=_ids(state, "denied_approvals"),
            paused_calls=_ids(state, "pause_calls") or _ids(state, "paused_calls"),
            held_calls=_ids(state, "held_calls"),
            held_approvals=_ids(state, "held_approvals"),
            approval_calls={
                c: cast(dict[str, object], meta) if isinstance(meta, dict) else {}
                for c, meta in approvals.items()
            },
            budget_calls=_ids(state, "budget_calls"),
            budget_asks=_ids(state, "budget_asks"),
            budget_approvals=_ids(state, "budget_approvals"),
        )


def _ids(state: Mapping[str, object], key: str) -> tuple[str, ...]:
    ids = state.get(key)
    return tuple(str(i) for i in cast(list[object], ids)) if isinstance(ids, list | tuple) else ()
