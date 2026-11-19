# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The log scrubber knows every minted credential: a Project join link's `si_`
code and a workspace invite's `oi_` code are whole authentications, shown once and
never logged - so the pattern both processes hand logfire must match what the minters mint, and
a log line carrying one must come out with the code gone."""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from prismoid.observability import SCRUB_PATTERNS
from prismoid.schemas.ids import new_project_invite_code, new_stream_ticket, new_workspace_invite_code

_SCRUB = re.compile("|".join(f"(?:{p})" for p in SCRUB_PATTERNS))


@pytest.mark.parametrize(
    "mint", [new_project_invite_code, new_workspace_invite_code, new_stream_ticket], ids=["si_", "oi_", "et_"]
)
def test_a_minted_invite_code_is_scrubbed(mint: Callable[[], str]) -> None:
    for _ in range(50):  # token_urlsafe can mint '-' and '_': every draw must match whole
        code = mint()
        assert _SCRUB.fullmatch(code), code
        line = f"redeemed {code} for project 42"
        assert code not in _SCRUB.sub("[scrubbed]", line)


def test_the_prefixes_are_the_ones_minted() -> None:
    assert new_project_invite_code().startswith("si_")
    assert new_workspace_invite_code().startswith("oi_")
    assert new_stream_ticket().startswith("et_")


def test_an_identifier_that_merely_contains_et_is_left_alone() -> None:
    """The stream ticket's pattern is anchored at a word start: a long snake_case name such as a
    function in a log line is not a ticket."""
    line = "budget_estimate_for_the_whole_long_running_turn_of_this_task_and_more called"
    assert _SCRUB.sub("[scrubbed]", line) == line
