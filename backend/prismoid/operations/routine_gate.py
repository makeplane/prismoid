# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The fire gate's pure checks - what `routine_fire._fire_gate` decides from the locked row and `now`.

Each check answers None (the fire goes on) or the reason the gate journals: a `Refusal` is the
`routine_runs` row's decision and detail, so every non-fire wears its reason. The gate holds the
row lock, reads the facts, asks these, and carries out the verdict; none of them reads a row, moves
`next_fire_at` or writes anything, so each is tested without a database. Fire math stays in
`schemas/routines.py`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from prismoid.schemas.routines import (
    Overlap,
    RoutineStatus,
    RunDecision,
    ScheduleSpec,
    TriggerKind,
    occurrence_for_clock,
)


def fire_instant_of(occurrence: str) -> dt.datetime | None:
    """A clock occurrence IS its scheduled instant (isoformat); man:/api:/ev: keys have none."""
    try:
        return dt.datetime.fromisoformat(occurrence)
    except ValueError:
        return None


def schedule_of(trigger_kind: str, trigger: dict[str, Any]) -> ScheduleSpec | None:
    """A schedule routine's spec, read tolerantly for the crash handler: a corrupt trigger must
    not mask the error it is trying to journal. None for any other trigger kind."""
    if trigger_kind != TriggerKind.SCHEDULE:
        return None
    try:
        return ScheduleSpec.model_validate(trigger)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class Refusal:
    """A non-fire: the decision and the detail its journal row wears."""

    decision: RunDecision
    detail: str


def stopped(status: str, *, manual_on_paused: bool) -> Refusal | None:
    """A pause or retire raced an enqueued fire. Run-now on a PAUSED routine is the deliberate
    exception: the play button is user-caused and always present - pausing stops the clock, not
    the human. Consuming the occurrence is safe: RESUME recomputes `next_fire_at` from now."""
    if status == RoutineStatus.ACTIVE or manual_on_paused:
        return None
    return Refusal(RunDecision.SUPERSEDED, f"routine is {status} - the fire raced the stop")


def chain_too_deep(chain_depth: int, cap: int) -> Refusal | None:
    """Routine → task → event chains are bounded: a fire past the cap is suppressed."""
    if chain_depth <= cap:
        return None
    return Refusal(
        RunDecision.SUPPRESSED_LOOP,
        f"chain depth {chain_depth} exceeds the cap of {cap} "
        "(routine→task→event chains are bounded - the Exchange hop budget, made visible)",
    )


def schedule_moved(next_fire_at: dt.datetime | None, occurrence: str) -> Refusal | None:
    """A clock fire whose occurrence is no longer the row's next fire: the schedule was edited
    (or the clock already moved past it) between the sweep and this fire - the edit race."""
    if next_fire_at is not None and occurrence == occurrence_for_clock(next_fire_at):
        return None
    return Refusal(RunDecision.SUPERSEDED, "the schedule changed before this fire ran")


def season_over(
    *, until_at: dt.datetime | None, max_runs: int | None, runs_minted: int, now: dt.datetime
) -> str | None:
    """Standing is not eternal, whatever the source: why the routine retires at this touch (its
    until-date passed, or its run season completed), or None while the season lasts."""
    if until_at is not None and now >= until_at:
        return "its until-date passed"
    if max_runs is not None and runs_minted >= max_runs:
        return f"its {max_runs}-run season completed"
    return None


def missed(fire_instant: dt.datetime, now: dt.datetime, *, grace_s: float) -> bool:
    """A fire older than the grace window fell while nothing was running."""
    return (now - fire_instant).total_seconds() > grace_s


def throttled(last_fired_at: dt.datetime | None, min_interval_s: int, now: dt.datetime) -> Refusal | None:
    """The min-interval floor (launchd's ThrottleInterval): a cron written under the floor
    journals one honest row per attempted fire - bounded by its own cadence."""
    if last_fired_at is None or (now - last_fired_at).total_seconds() >= min_interval_s:
        return None
    return Refusal(RunDecision.SKIPPED_THROTTLE, f"under the {min_interval_s}s floor between fires")


class OverlapMove(StrEnum):
    """What a fire does while the routine's previous run is still un-settled."""

    PARK_EXPIRED = "park_expired"  # a parked fire waited past the buffer's limit: journal and drop
    PARK_WAITING = "park_waiting"  # a parked fire still waiting: change nothing, the drain re-offers
    BUFFER_FULL = "buffer_full"  # queue holds one, and one already waits
    TAKE_SLOT = "take_slot"  # the first arrival parks in the buffer
    REPLACE = "replace"  # cancel the live run, then mint
    SKIP = "skip"  # journal and stand down


def overlap_move(
    overlap: str,
    *,
    queueable: bool,
    is_parked: bool,
    buffer_taken: bool,
    waited_s: float,
    buffer_max_s: float,
) -> OverlapMove:
    """The overlap policy: skip journals and stands down; queue buffers EXACTLY ONE (the sweep's
    drain arm re-offers it each tick); replace cancels and mints. `queueable` says the fire has a
    scheduled instant on a schedule - queue on any other fire skips, honestly. `waited_s` is how
    long a parked fire has waited since its instant."""
    if overlap == Overlap.QUEUE and queueable:
        if is_parked:
            return OverlapMove.PARK_EXPIRED if waited_s > buffer_max_s else OverlapMove.PARK_WAITING
        return OverlapMove.BUFFER_FULL if buffer_taken else OverlapMove.TAKE_SLOT
    if overlap == Overlap.REPLACE:
        return OverlapMove.REPLACE
    return OverlapMove.SKIP


def joined(detail: str, more: str) -> str:
    """A fire's detail grows by clauses, ` · ` between them."""
    return (detail + " · " if detail else "") + more
