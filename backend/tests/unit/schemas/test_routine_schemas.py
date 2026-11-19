# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Routine fire math (schemas/routines.py) - pure, clock-injected, DST-golden.

The DST rows are the reason cronsim is a dependency: Debian cron's rules, exactly -
spring-forward jobs in the gap run ONCE shortly after the change; fall-back jobs in the
repeated hour are NOT re-run. US 2026: spring forward Mar 8 (02:00→03:00 EDT), fall back
Nov 1 (02:00→01:00 EST).
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from prismoid.schemas.routines import (
    ScheduleSpec,
    fire_workflow_id,
    mint_client_token,
    missed_fires,
    next_fire_at,
    occurrence_for_clock,
    parse_duration_s,
    preview,
)

NY = ZoneInfo("America/New_York")
UTC = dt.UTC


# ── ADR-048's own laws ────────────────────────────────────────────────────────────────────────


def test_exactly_one_of_cron_every_at() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        ScheduleSpec(cron="0 9 * * *", every_s=3600)
    with pytest.raises(ValueError, match="exactly one"):
        ScheduleSpec()


def test_cron_is_validated_by_cronsim_and_field_count() -> None:
    with pytest.raises(ValueError, match="five fields"):
        ScheduleSpec(cron="0 9 * *")
    with pytest.raises(ValueError, match="cron rejected"):
        ScheduleSpec(cron="99 9 * * *")


def test_timezone_must_be_iana_and_one_shots_carry_offsets() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        ScheduleSpec(cron="0 9 * * *", timezone="Mars/Olympus")
    with pytest.raises(ValueError, match="carry its timezone"):
        ScheduleSpec(at=dt.datetime(2026, 9, 1, 9, 0))  # naive - refused


def test_duration_grammar() -> None:
    assert parse_duration_s("30m") == 1800
    assert parse_duration_s("4h") == 14400
    assert parse_duration_s("2d") == 172800
    with pytest.raises(ValueError, match="duration"):
        parse_duration_s("fortnight")


# ── next-fire math ────────────────────────────────────────────────────────────────────────────


def test_cron_next_fire_respects_the_wall_clock_timezone() -> None:
    spec = ScheduleSpec(cron="0 9 * * 1-5", timezone="America/New_York")
    # A Sunday evening in NY → Monday 09:00 EST = 14:00 UTC (winter).
    after = dt.datetime(2026, 1, 4, 23, 0, tzinfo=NY)
    fire = next_fire_at(spec, after=after)
    assert fire == dt.datetime(2026, 1, 5, 14, 0, tzinfo=UTC)
    # Summer: the same wall clock is 13:00 UTC - the wall clock is preserved, not the offset.
    july = next_fire_at(spec, after=dt.datetime(2026, 7, 5, 23, 0, tzinfo=NY))
    assert july is not None and july.hour == 13


def test_interval_and_one_shot() -> None:
    every = ScheduleSpec(every_s=3600)
    t0 = dt.datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
    assert next_fire_at(every, after=t0) == t0 + dt.timedelta(hours=1)

    once = ScheduleSpec(at=dt.datetime(2026, 9, 1, 9, 0, tzinfo=UTC))
    assert next_fire_at(once, after=t0) == dt.datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
    assert next_fire_at(once, after=dt.datetime(2026, 9, 2, tzinfo=UTC)) is None, "exhausted"


def test_dst_spring_forward_gap_fires_once_shortly_after() -> None:
    """The 02:30 job on the day 02:00→03:00 does not exist - Debian's rule: run once, soon
    after the change; and exactly once for the whole day."""
    spec = ScheduleSpec(cron="30 2 * * *", timezone="America/New_York")
    after = dt.datetime(2026, 3, 8, 1, 0, tzinfo=NY)  # 01:00 EST, before the jump
    fire = next_fire_at(spec, after=after)
    assert fire is not None
    local = fire.astimezone(NY)
    assert local.date() == dt.date(2026, 3, 8)
    assert dt.time(3, 0) <= local.time() <= dt.time(3, 30), f"soon after the gap, got {local}"
    # And the day holds exactly one fire: the next is Mar 9, 02:30 EDT.
    second = next_fire_at(spec, after=fire)
    assert second is not None and second.astimezone(NY).day == 9


def test_dst_fall_back_repeated_hour_never_fires_twice() -> None:
    """01:30 exists twice on Nov 1 (EDT then EST) - Debian's rule: not re-run."""
    spec = ScheduleSpec(cron="30 1 * * *", timezone="America/New_York")
    cursor = dt.datetime(2026, 10, 31, 23, 0, tzinfo=NY)
    fires_on_nov_1 = []
    for _ in range(4):
        nxt = next_fire_at(spec, after=cursor)
        assert nxt is not None
        if nxt.astimezone(NY).date() == dt.date(2026, 11, 1):
            fires_on_nov_1.append(nxt)
        cursor = nxt
    assert len(fires_on_nov_1) == 1, f"the repeated hour must fire once, got {fires_on_nov_1}"


# ── misses, previews, identity ────────────────────────────────────────────────────────────────


def test_missed_fires_counts_and_names_the_most_recent() -> None:
    spec = ScheduleSpec(cron="0 * * * *")  # hourly, UTC
    occurrence = dt.datetime(2026, 8, 24, 3, 0, tzinfo=UTC)
    now = dt.datetime(2026, 8, 24, 7, 30, tzinfo=UTC)
    count, latest = missed_fires(spec, occurrence=occurrence, now=now)
    assert count == 5  # 03,04,05,06,07
    assert latest == dt.datetime(2026, 8, 24, 7, 0, tzinfo=UTC), "the coalesce target"

    every = ScheduleSpec(every_s=3600)
    count, latest = missed_fires(every, occurrence=occurrence, now=now)
    assert count == 5 and latest == dt.datetime(2026, 8, 24, 7, 0, tzinfo=UTC)


def test_preview_echoes_the_resolved_instants() -> None:
    spec = ScheduleSpec(cron="0 9 * * *", timezone="UTC")
    after = dt.datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    fires = preview(spec, after=after, n=3)
    assert [f.day for f in fires] == [25, 26, 27]
    once = ScheduleSpec(at=dt.datetime(2026, 9, 1, 9, 0, tzinfo=UTC))
    assert len(preview(once, after=after, n=3)) == 1, "a one-shot previews itself once"


def test_jitter_is_deterministic_bounded_and_cron_only() -> None:
    import uuid

    from prismoid.schemas.routines import jitter_offset_s

    rid = uuid.UUID("00000000-0000-0000-0000-00000000abcd")
    off = jitter_offset_s(rid, 120)
    assert off == jitter_offset_s(rid, 120), "FixedRandomDelay: stable per routine"
    assert 0 <= off < 120
    assert jitter_offset_s(rid, 0) == 0

    after = dt.datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
    cron = ScheduleSpec(cron="0 12 * * *")
    assert next_fire_at(cron, after=after, jitter_s=90) == dt.datetime(2026, 8, 24, 12, 1, 30, tzinfo=UTC), (
        "cron fires carry the offset"
    )
    every = ScheduleSpec(every_s=3600)
    assert next_fire_at(every, after=after, jitter_s=90) == after + dt.timedelta(hours=1), (
        "intervals are already relative - no jitter"
    )
    once = ScheduleSpec(at=dt.datetime(2026, 9, 1, 9, 0, tzinfo=UTC))
    fired = next_fire_at(once, after=after, jitter_s=90)
    assert fired is not None and fired.minute == 0, "a one-shot is the user's exact instant"


def test_day_and_month_boundaries_are_the_routines_own(  # the cap windows
) -> None:
    from prismoid.schemas.routines import day_start, month_start, next_day_start

    now = dt.datetime(2026, 8, 24, 22, 30, tzinfo=UTC)  # 04:00 Aug 25 in Kolkata (+05:30)
    assert day_start("Asia/Kolkata", now) == dt.datetime(2026, 8, 24, 18, 30, tzinfo=UTC)
    assert next_day_start("Asia/Kolkata", now) == dt.datetime(2026, 8, 25, 18, 30, tzinfo=UTC)
    assert month_start("Asia/Kolkata", now) == dt.datetime(2026, 7, 31, 18, 30, tzinfo=UTC)
    assert day_start("UTC", now) == dt.datetime(2026, 8, 24, 0, 0, tzinfo=UTC)


def test_occurrence_and_token_are_deterministic_and_fit_their_columns() -> None:
    import uuid

    rid = uuid.UUID("00000000-0000-0000-0000-000000000001")
    fire = dt.datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
    occ = occurrence_for_clock(fire)
    assert occ == "2026-08-24T09:00:00+00:00" and len(occ) <= 64
    assert occurrence_for_clock(fire.astimezone(NY)) == occ, "UTC-normalized whatever the tz"
    token = mint_client_token(rid, occ)
    assert token == mint_client_token(rid, occ) and len(token) <= 64
    tick = dt.datetime(2026, 8, 24, 9, 1, tzinfo=UTC)
    wf = fire_workflow_id(rid, occ, tick=tick)
    assert wf == f"routine-{rid}-{occ}~20260824T0901"
    assert wf == fire_workflow_id(rid, occ, tick=tick), "replay-stable within a tick"
    # across ticks the id CHANGES - a terminally-errored fire is re-offered fresh instead of
    # wedging its occurrence on a consumed id
    assert wf != fire_workflow_id(rid, occ, tick=tick + dt.timedelta(minutes=1))


def test_api_occurrences_sanitize_fit_and_never_collide() -> None:
    from prismoid.schemas.routines import occurrence_for_api

    assert occurrence_for_api("deploy-77").startswith("api:deploy-77.")
    assert occurrence_for_api("deploy-77") == occurrence_for_api("deploy-77"), "deterministic"
    weird = occurrence_for_api("a b/c\\d" + "x" * 100)
    assert len(weird) <= 64 and " " not in weird and "/" not in weird
    # DISTINCT keys stay distinct - sanitize+clip alone collided "deploy 42" with "deploy/42"
    # and any two long keys sharing a prefix
    assert occurrence_for_api("deploy 42") != occurrence_for_api("deploy/42")
    long_a = "k" * 80 + "a"
    long_b = "k" * 80 + "b"
    assert occurrence_for_api(long_a) != occurrence_for_api(long_b)
    assert occurrence_for_api(None).startswith("api:") and occurrence_for_api(None) != occurrence_for_api(
        None
    )


def test_jitter_never_swallows_a_slot() -> None:
    """The raw slot lane is jitter-invariant: an offset >= the gap between slots must not
    skip alternate occurrences (an every-minute cron with a 90s offset fired every OTHER
    minute before the walk subtracted the offset)."""
    spec = ScheduleSpec(cron="* * * * *")
    fire1 = next_fire_at(spec, after=dt.datetime(2026, 8, 24, 12, 0, 30, tzinfo=UTC), jitter_s=90)
    assert fire1 is not None
    assert fire1 == dt.datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC) + dt.timedelta(seconds=90)
    # advancing from the moment fire1 lands yields the NEXT minute's slot + offset - no skip
    fire2 = next_fire_at(spec, after=fire1, jitter_s=90)
    assert fire2 == fire1 + dt.timedelta(minutes=1)


def test_missed_fires_strips_the_jitter_before_walking() -> None:
    """A jittered occurrence (seconds != :00) must still count its own slot, and `latest`
    wears the jittered instant a catch-up run actually fires at."""
    spec = ScheduleSpec(cron="0 9 * * *")
    j = 37
    occurrence = dt.datetime(2026, 8, 24, 9, 0, 37, tzinfo=UTC)  # raw 09:00 + 37s
    now = dt.datetime(2026, 8, 26, 10, 0, tzinfo=UTC)  # two days later
    count, latest = missed_fires(spec, occurrence=occurrence, now=now, jitter_s=j)
    assert count == 3  # the 24th, 25th, 26th - the occurrence's own slot included
    assert latest == dt.datetime(2026, 8, 26, 9, 0, 37, tzinfo=UTC)


def test_upcoming_after_anchors_on_the_stored_clock() -> None:
    from prismoid.schemas.routines import upcoming_after

    spec = ScheduleSpec(every_s=4 * 3600)
    stored = dt.datetime(2026, 8, 24, 14, 0, tzinfo=UTC)  # mid-phase next_fire_at
    fires = upcoming_after(spec, first=stored, n=3)
    assert fires[0] == stored, "upcoming[0] IS the row's own truth"
    assert fires[1] == stored + dt.timedelta(hours=4)
    assert len(fires) == 3
