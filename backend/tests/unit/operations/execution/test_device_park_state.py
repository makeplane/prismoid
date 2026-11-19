# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a device park keeps: which cut-off command it names (the journal's, an earlier park's owed
cut, or the one it was handed) and the `device_away` state the resumed attempt reads. Pure."""

from __future__ import annotations

import datetime as dt

import pytest

from prismoid.operations.execution.device import Cut, away_state, parked_cut
from prismoid.schemas.device import InterruptedOut

FOUND = InterruptedOut(
    exec_id="e9",
    command_head="npm test",
    started_at=dt.datetime(2026, 11, 1, tzinfo=dt.UTC),
    still_running=True,
)
OWED = Cut(head="make build", exec_id="e1")
LOOK = Cut(look=True)


@pytest.mark.parametrize(
    ("found", "owed", "given", "kept"),
    [
        (FOUND, OWED, LOOK, Cut(head="npm test", still_running=True, exec_id="e9")),
        (None, None, None, None),
        (None, None, LOOK, LOOK),
        (None, OWED, None, OWED),
        (None, OWED, LOOK, Cut(head="make build", exec_id="e1", look=True)),
        (None, Cut(look=True), None, Cut(look=True)),
        (None, OWED, Cut(head="newer"), Cut(head="newer")),
    ],
)
def test_the_cut_a_park_keeps(
    found: InterruptedOut | None, owed: Cut | None, given: Cut | None, kept: Cut | None
) -> None:
    assert parked_cut(found, owed, given) == kept


def test_a_bare_park_says_only_why_and_where() -> None:
    state = away_state("away", name="Ada's Mac", cut=None, resume=None, gotchas=None)
    assert state == {"why": "away", "name": "Ada's Mac"}


def test_a_park_keeps_everything_the_resumed_attempt_is_owed() -> None:
    state = away_state("interrupted", name=None, cut=OWED, resume={"run": 1}, gotchas=["verify once"])
    assert state == {
        "why": "interrupted",
        "name": None,
        **OWED.state(),
        "resume": {"run": 1},
        "gotchas": ["verify once"],
    }
    assert list(state)[:2] == ["why", "name"]


def test_an_empty_gotcha_list_is_not_kept() -> None:
    assert "gotchas" not in away_state("away", name="x", cut=None, resume=None, gotchas=[])
