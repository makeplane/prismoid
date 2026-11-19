# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A bounded run's clock, as its web tools read it.

A run with a wall (the express answer, a conversation reply, a step's wall clock) is stopped
whole when the wall passes - and a tool still waiting then takes the answer down with it (a
provider-native search given 60 s inside a 45 s express wall stops the attempt, and a retry runs
the same slow search again). So a tool call never waits past the run's wall
less `MARGIN_S` - the time the model needs to write its answer from what the tool said - and a
call with less than `FLOOR_S` left is not sent at all. A search also leaves `READ_S`: it finds pages
to read, so a slow query is cut while the pages its batch found can still be read, never after.

`deadline` is a closure, read at call time: operations sets `TaskDeps.deadline` (a
`time.monotonic()` instant) once the run's wall is known, which may be after the toolset was
built. None (no closure, or no deadline set) means the tool's own timeout stands.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Final

Deadline = Callable[[], float | None]

MARGIN_S: Final = 10.0  # left to the model after the tool returns: one answering turn
FLOOR_S: Final = 3.0  # less than this for a call: not worth sending
READ_S: Final = 10.0  # left by a search for the page reads it is for

OUT_OF_TIME: Final = (
    "this answer has too little time left to wait for the web. Answer now from the results and pages "
    "you already have, citing them, and say plainly that anything they do not back is not verified "
    "against the web."
)


def allowance(deadline: Deadline | None, own_s: float, *, reserve_s: float = 0.0) -> float | None:
    """The seconds one call may take: its own timeout, cut to what the run's wall leaves after the
    margin and `reserve_s`. None when that is under the floor - the call is not worth sending."""
    at = deadline() if deadline is not None else None
    if at is None:
        return own_s
    left = at - time.monotonic() - MARGIN_S - reserve_s
    return None if left < FLOOR_S else min(own_s, left)
