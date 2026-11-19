# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The fire gate's pure checks: each answers None (the fire goes on) or the reason its journal row
wears. No I/O - the locked row's values and `now` are arguments."""

from __future__ import annotations

import datetime as dt

import pytest

from prismoid.operations import routine_gate
from prismoid.operations.routine_gate import OverlapMove, Refusal
from prismoid.schemas.routines import Overlap, RoutineStatus, RunDecision, occurrence_for_clock

NOW = dt.datetime(2026, 11, 4, 9, 0, tzinfo=dt.UTC)


def test_an_active_routine_fires_and_a_stopped_one_is_superseded() -> None:
    assert routine_gate.stopped(RoutineStatus.ACTIVE, manual_on_paused=False) is None
    assert routine_gate.stopped(RoutineStatus.RETIRED, manual_on_paused=False) == Refusal(
        RunDecision.SUPERSEDED, "routine is retired - the fire raced the stop"
    )


def test_run_now_on_a_paused_routine_is_the_one_exception() -> None:
    assert routine_gate.stopped(RoutineStatus.PAUSED, manual_on_paused=True) is None
    refusal = routine_gate.stopped(RoutineStatus.PAUSED, manual_on_paused=False)
    assert refusal is not None and refusal.decision is RunDecision.SUPERSEDED


def test_a_chain_at_the_cap_fires_and_one_past_it_is_suppressed() -> None:
    assert routine_gate.chain_too_deep(3, 3) is None
    refusal = routine_gate.chain_too_deep(4, 3)
    assert refusal is not None
    assert refusal.decision is RunDecision.SUPPRESSED_LOOP
    assert refusal.detail.startswith("chain depth 4 exceeds the cap of 3")


def test_a_clock_fire_must_still_be_the_rows_next_fire() -> None:
    assert routine_gate.schedule_moved(NOW, occurrence_for_clock(NOW)) is None
    later = NOW + dt.timedelta(hours=1)
    assert routine_gate.schedule_moved(later, occurrence_for_clock(NOW)) == Refusal(
        RunDecision.SUPERSEDED, "the schedule changed before this fire ran"
    )
    assert routine_gate.schedule_moved(None, occurrence_for_clock(NOW)) is not None


def test_a_season_ends_at_its_until_date_or_its_run_count() -> None:
    assert routine_gate.season_over(until_at=None, max_runs=None, runs_minted=99, now=NOW) is None
    assert (
        routine_gate.season_over(until_at=NOW, max_runs=None, runs_minted=0, now=NOW)
        == "its until-date passed"
    )
    assert (
        routine_gate.season_over(until_at=None, max_runs=5, runs_minted=5, now=NOW)
        == "its 5-run season completed"
    )
    assert (
        routine_gate.season_over(until_at=NOW + dt.timedelta(days=1), max_runs=5, runs_minted=4, now=NOW)
        is None
    )


def test_the_until_date_is_named_before_the_run_count() -> None:
    assert (
        routine_gate.season_over(until_at=NOW, max_runs=1, runs_minted=1, now=NOW) == "its until-date passed"
    )


def test_a_miss_is_older_than_the_grace_window() -> None:
    assert not routine_gate.missed(NOW - dt.timedelta(seconds=300), NOW, grace_s=300)
    assert routine_gate.missed(NOW - dt.timedelta(seconds=301), NOW, grace_s=300)


def test_the_floor_throttles_only_inside_the_interval() -> None:
    assert routine_gate.throttled(None, 300, NOW) is None, "a routine that never fired is never throttled"
    assert routine_gate.throttled(NOW - dt.timedelta(seconds=300), 300, NOW) is None
    assert routine_gate.throttled(NOW - dt.timedelta(seconds=299), 300, NOW) == Refusal(
        RunDecision.SKIPPED_THROTTLE, "under the 300s floor between fires"
    )


def _move(overlap: str, **over: object) -> OverlapMove:
    queueable = bool(over.get("queueable", True))
    is_parked = bool(over.get("is_parked", False))
    buffer_taken = bool(over.get("buffer_taken", False))
    waited = over.get("waited_s", 0.0)
    assert isinstance(waited, float)
    return routine_gate.overlap_move(
        overlap,
        queueable=queueable,
        is_parked=is_parked,
        buffer_taken=buffer_taken,
        waited_s=waited,
        buffer_max_s=60.0,
    )


@pytest.mark.parametrize(
    ("overlap", "over", "expected"),
    [
        (Overlap.SKIP, {}, OverlapMove.SKIP),
        (Overlap.REPLACE, {}, OverlapMove.REPLACE),
        (Overlap.QUEUE, {}, OverlapMove.TAKE_SLOT),
        (Overlap.QUEUE, {"buffer_taken": True}, OverlapMove.BUFFER_FULL),
        (
            Overlap.QUEUE,
            {"is_parked": True, "buffer_taken": True, "waited_s": 60.0},
            OverlapMove.PARK_WAITING,
        ),
        (
            Overlap.QUEUE,
            {"is_parked": True, "buffer_taken": True, "waited_s": 61.0},
            OverlapMove.PARK_EXPIRED,
        ),
        # queue on a fire with no scheduled instant (run-now, a token, an event) skips, honestly
        (Overlap.QUEUE, {"queueable": False}, OverlapMove.SKIP),
        (Overlap.REPLACE, {"queueable": False}, OverlapMove.REPLACE),
    ],
)
def test_the_overlap_move(overlap: str, over: dict[str, object], expected: OverlapMove) -> None:
    assert _move(overlap, **over) is expected


def test_details_join_by_clause() -> None:
    assert routine_gate.joined("", "buffered") == "buffered"
    assert routine_gate.joined("coalesced 2", "buffered") == "coalesced 2 · buffered"


def test_only_a_clock_occurrence_has_an_instant() -> None:
    assert routine_gate.fire_instant_of(occurrence_for_clock(NOW)) == NOW
    assert routine_gate.fire_instant_of("man:abc") is None


def test_the_tolerant_schedule_read_never_raises() -> None:
    assert routine_gate.schedule_of("api", {}) is None
    assert routine_gate.schedule_of("schedule", {"cron": 42}) is None, "a corrupt trigger reads as none"
