# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The control lease's and the relay's pure decisions (ADR-065): live,
claim, re-claim, renew, extend, grace, cap, the input gate and the settle, and each viewer's
frame budget. No I/O and no clock - the lease and `now` are arguments - so every edge is exact:
`until == now` is over, the renew throttle turns at exactly 5 s, a private lease's grace never
passes its cap, a re-claim never moves the cap, and an extend after the cap moves both."""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
import uuid

import pytest

from prismoid.schemas.screen import (
    CONN_PATTERN,
    ControlEnd,
    ControlState,
    Lease,
    LeaseLimits,
    LeaseView,
    Pacer,
    claim_window,
    end_reason,
    extend_window,
    grace_until,
    held_seconds,
    held_text,
    is_live,
    may_drive,
    reclaim_until,
    renew_until,
    reopen_until,
    upstream_fps,
    viewer_fps,
)

NOW = dt.datetime(2026, 10, 2, 12, 0, tzinfo=dt.UTC)
LIMITS = LeaseLimits(silence_s=60, private_cap_s=600, grace_s=30, renew_every_s=5, settle_ms=300)
ADA, BOB = uuid.uuid4(), uuid.uuid4()
TAB = "tab-one-0001"


def at(seconds: float) -> dt.datetime:
    return NOW + dt.timedelta(seconds=seconds)


def lease(
    *,
    private: bool = False,
    since: float = -10,
    until: float = 50,
    cap_at: float | None = None,
    actor: uuid.UUID = ADA,
    conn: str = TAB,
) -> Lease:
    return Lease(
        actor_id=actor,
        conn=conn,
        private=private,
        since=at(since),
        until=at(until),
        cap_at=None if cap_at is None else at(cap_at),
    )


# ── live, claim, re-claim ─────────────────────────────────────────────────────────────────


def test_a_lease_is_live_strictly_before_until() -> None:
    assert is_live(None, NOW) is False
    assert is_live(lease(until=0.001), NOW) is True
    assert is_live(lease(until=0), NOW) is False  # until == now is already over
    assert is_live(lease(until=-1), NOW) is False


def test_a_claim_gets_a_minute_or_a_private_cap() -> None:
    assert claim_window(private=False, now=NOW, limits=LIMITS) == (at(60), None)
    assert claim_window(private=True, now=NOW, limits=LIMITS) == (at(600), at(600))


def test_a_private_reclaim_restores_until_to_the_cap_and_never_moves_it() -> None:
    in_grace = lease(private=True, since=-120, until=10, cap_at=480)  # the holder's tab reloaded
    assert reclaim_until(in_grace, NOW, LIMITS) == at(480)  # restored to the cap, which stays put
    near_cap = lease(private=True, since=-595, until=5, cap_at=5)
    assert reclaim_until(near_cap, NOW, LIMITS) == at(5)  # never past the cap


def test_a_non_private_reclaim_gets_its_minute_back() -> None:
    in_grace = lease(until=20)  # the socket-close grace had shortened it
    assert reclaim_until(in_grace, NOW, LIMITS) == at(60)


# ── renew, extend, grace, the ending's reason ─────────────────────────────────────────────


def test_renewal_is_throttled_to_every_five_seconds_exactly_at_the_edge() -> None:
    renewed_5s_ago = lease(until=55)  # until - silence = 5 s ago
    assert renew_until(renewed_5s_ago, NOW, LIMITS) == at(60)
    just_under = dataclasses.replace(renewed_5s_ago, until=at(55) + dt.timedelta(microseconds=1))
    assert renew_until(just_under, NOW, LIMITS) is None


def test_a_private_or_finished_lease_is_never_renewed() -> None:
    assert renew_until(lease(private=True, until=300, cap_at=300), NOW, LIMITS) is None
    assert renew_until(lease(until=0), NOW, LIMITS) is None  # over: the sweep ends it
    assert renew_until(lease(until=-5), NOW, LIMITS) is None


def test_an_extend_moves_until_and_the_cap_together_even_after_the_old_cap() -> None:
    until, cap_at = extend_window(NOW, LIMITS)
    assert until == cap_at == NOW + dt.timedelta(seconds=LIMITS.private_cap_s)
    _, old_cap = claim_window(private=True, now=NOW, limits=LIMITS)
    assert old_cap is not None
    later = old_cap + dt.timedelta(seconds=1)  # the old cap has passed
    until, cap_at = extend_window(later, LIMITS)
    assert until == cap_at == later + dt.timedelta(seconds=LIMITS.private_cap_s)


def test_the_grace_shortens_and_never_lengthens_or_passes_the_cap() -> None:
    assert grace_until(lease(until=60), NOW, LIMITS) == at(30)
    assert grace_until(lease(until=10), NOW, LIMITS) == at(10)
    assert grace_until(lease(private=True, until=500, cap_at=500), NOW, LIMITS) == at(30)
    near_cap = lease(private=True, until=10, cap_at=10)
    assert grace_until(near_cap, NOW, LIMITS) == at(10) == near_cap.cap_at


def test_the_holders_connection_coming_back_restores_what_the_grace_took() -> None:
    """A reconnect on the holder's own connection (a 4408 drop, a reload that kept
    its connection) undoes the grace - a private lease back to its cap, which never moves; a
    non-private one gets its minute back. A lease the grace never touched, or one already over, is
    left alone (None: no write)."""
    graced = grace_until(lease(private=True, until=500, cap_at=500), NOW, LIMITS)
    assert reopen_until(lease(private=True, until=30, cap_at=500), at(5), LIMITS) == at(500)
    assert graced == at(30)
    assert reopen_until(lease(private=True, until=500, cap_at=500), NOW, LIMITS) is None
    assert reopen_until(lease(until=30), at(5), LIMITS) == at(65)
    assert reopen_until(lease(until=50), at(5), LIMITS) is None, "not shortened: past the grace's reach"
    assert reopen_until(lease(until=30), at(30), LIMITS) is None, "until == now is over"
    assert reopen_until(lease(private=True, until=30, cap_at=500), at(30), LIMITS) is None
    wide = dataclasses.replace(LIMITS, grace_s=90)  # a grace longer than the silence never shortens
    assert reopen_until(lease(until=80), NOW, wide) is None


def test_an_expired_lease_says_why_from_its_row() -> None:
    assert end_reason(lease(private=True, until=-1, cap_at=-1)) is ControlEnd.CAP
    assert end_reason(lease(private=True, until=-1, cap_at=400)) is ControlEnd.GRACE
    assert end_reason(lease(until=-1)) is ControlEnd.EXPIRED
    assert {e.value for e in ControlEnd} == {
        "released",
        "expired",
        "grace",
        "cap",
        "ended",
        "demoted",
        "disabled",
        "lost",
    }


def test_a_lease_is_held_until_it_ended_or_its_clock_ran_out() -> None:
    """`machine.control_ended.held_s`: a live lease ended at `at` was held until `at`; one whose
    clock ran out was held until its `until`, however late its ending was noticed (the lease-end
    job, a claim that beat it, a sleep that cleared it)."""
    assert held_seconds(lease(since=-10, until=50), NOW) == 10  # released, ended, lost: at `at`
    assert held_seconds(lease(since=-120, until=-60), NOW) == 60  # ran out a minute ago
    assert held_seconds(lease(since=-60, until=0), NOW) == 60  # `until == now`: the same either way
    assert held_seconds(lease(private=True, since=-700, until=-100, cap_at=-100), NOW) == 600  # the cap
    assert held_seconds(lease(since=5, until=65), NOW) == 0  # a clock that stepped back: never negative


# ── the input gate and the settle ─────────────────────────────────────────────────────────


def test_a_conn_is_8_to_64_url_safe_characters() -> None:
    matches = [c for c in (TAB, "a" * 64, "short", "a" * 65, "tab one 01") if re.fullmatch(CONN_PATTERN, c)]
    assert matches == [TAB, "a" * 64]


def test_only_the_holders_own_connection_drives_a_live_lease() -> None:
    live = lease()
    assert may_drive(live, actor_id=ADA, conn=TAB, now=NOW, limits=LIMITS) is True
    assert may_drive(live, actor_id=ADA, conn="tab-two-0002", now=NOW, limits=LIMITS) is False  # a 2nd tab
    assert may_drive(live, actor_id=BOB, conn=TAB, now=NOW, limits=LIMITS) is False
    assert may_drive(None, actor_id=ADA, conn=TAB, now=NOW, limits=LIMITS) is False
    assert may_drive(lease(until=0), actor_id=ADA, conn=TAB, now=NOW, limits=LIMITS) is False


def test_a_private_claim_drops_input_for_the_settle_and_not_a_moment_longer() -> None:
    fresh = lease(private=True, since=0, until=600, cap_at=600)
    almost = NOW + dt.timedelta(milliseconds=299)
    settled = NOW + dt.timedelta(milliseconds=300)
    assert may_drive(fresh, actor_id=ADA, conn=TAB, now=almost, limits=LIMITS) is False
    assert may_drive(fresh, actor_id=ADA, conn=TAB, now=settled, limits=LIMITS) is True
    assert may_drive(lease(since=0), actor_id=ADA, conn=TAB, now=NOW, limits=LIMITS) is True  # no settle


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (-5, "0s"),
        (0, "0s"),
        (45, "45s"),
        (60, "1m"),
        (100, "1m 40s"),
        (130, "2m 10s"),
        (3599, "59m 59s"),
        (3600, "1h"),
        (7380, "2h 3m"),
    ],
)
def test_held_text(seconds: int, text: str) -> None:
    assert held_text(seconds) == text


# ── pacing ────────────────────────────────────────────────────────────────────────────────


def test_the_bucket_starts_full_and_skips_what_does_not_fit() -> None:
    pacer = Pacer(rate_bps=300_000, fps=10)
    assert pacer.admit(0.0, 200_000) is True  # full at the start
    assert pacer.admit(0.1, 200_000) is False  # 100 kB left + 30 kB refilled: skipped, not queued
    assert pacer.admit(0.5, 200_000) is True  # 130 kB + 120 kB refilled


def test_a_frame_bigger_than_the_bucket_goes_when_it_is_full_and_the_debt_keeps_the_rate() -> None:
    """A dense page (a map, a photo) can make every frame bigger than one second of a viewer's
    budget: it is shown slower, never not at all (the screen froze silently on it, holder
    included). The bucket goes into debt, so the average stays at `rate_bps`."""
    pacer = Pacer(rate_bps=300_000, fps=10)
    assert pacer.admit(0.0, 500_000) is True  # a full bucket: 200 kB into debt
    assert pacer.admit(0.5, 1) is False  # the debt is paid first: -50 kB
    assert pacer.admit(1.0, 500_000) is False  # 100 kB back: not full yet
    assert pacer.admit(1.7, 500_000) is True  # full again after 500 kB / 300 kB/s
    assert pacer.admit(1.8, 200_000) is False


def test_the_bucket_is_one_second_deep() -> None:
    pacer = Pacer(rate_bps=300_000, fps=10)
    assert pacer.admit(0.0, 1) is True
    assert pacer.admit(100.0, 300_000) is True  # an idle minute refills one second, no more
    assert pacer.admit(100.1, 300_000) is False


def test_the_rate_cap_keeps_a_minimum_gap_with_a_little_slack_for_jitter() -> None:
    pacer = Pacer(rate_bps=10**9, fps=5)
    assert pacer.admit(0.0, 1) is True
    assert pacer.admit(0.1, 1) is False
    assert pacer.admit(0.17, 1) is False
    assert pacer.admit(0.19, 1) is True  # 10 ms early at 5 fps is on time
    pacer.fps = 1  # a thumbnail's hint
    assert pacer.admit(0.5, 1) is False
    assert pacer.admit(1.2, 1) is True


def test_a_paused_viewer_gets_nothing() -> None:
    pacer = Pacer(rate_bps=300_000, fps=0)
    assert pacer.admit(0.0, 1) is False
    assert pacer.admit(10.0, 1) is False


def test_the_viewer_caps_and_the_upstream_rate() -> None:
    rates = {"fps_thumb": 1, "fps_pane": 5, "fps_holder": 10}
    assert viewer_fps(pane=False, holder=False, **rates) == 1
    assert viewer_fps(pane=True, holder=False, **rates) == 5
    assert viewer_fps(pane=True, holder=True, **rates) == 10
    assert viewer_fps(pane=False, holder=True, **rates) == 10
    assert upstream_fps([], default=1) == 1
    assert upstream_fps([1, 5], default=1) == 5
    assert upstream_fps([1, 10, 5], default=1) == 10


def test_the_views_are_frozen_values() -> None:
    state = ControlState(holder_name="Ada", private=True, since=NOW)
    view = LeaseView(holder_id=ADA, holder_name="Ada", private=False, since=NOW, until=at(60), cap_at=None)
    assert state == ControlState(holder_name="Ada", private=True, since=NOW)
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.private = True  # exactly the point  # pyright: ignore[reportAttributeAccessIssue]
