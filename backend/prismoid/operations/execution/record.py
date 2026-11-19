# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The step workflow's hand-back record (ADR-027): what `execute_step` hands its `run_step`
workflow back - a park's gate (`ASKING`) or a pace (`PACING`) - and the list of them it keeps on the
step's row, so a replay re-issues each in order. `__init__` imports these under private names."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from typing import Any

from prismoid.db.base import utcnow
from prismoid.db.models.plan import HANDBACKS, PlanStep
from prismoid.schemas.failures import FailureKind
from prismoid.schemas.park import OWED_RUN, OwedRun

ASKING = "\x00asking:"
# The ladder's other hand-back to its workflow: the step could not get its Machine
# at once, so the workflow waits out the rest of `wait_s` (seconds follow) before the next rung.
PACING = "\x00pacing:"

# **The step workflow's hand-backs, in the order it took them.** A park (`ASKING`: the
# workflow recv's its gate's wake) and a pace (`PACING`: it sleeps on DBOS's clock) are journaled
# DBOS calls; the ladder that hands them back is not. So the row keeps the workflow's list on
# `resume_state` (`HANDBACKS`: `park:<gate id>`, `pace:<deadline>`) and how many of them had their
# wake stored (`WOKE`). A worker death replays the workflow's body from the top: it counts its
# hand-backs, and while the count is behind the list the door re-issues the next one instead of
# running - each recorded recv or sleep hands its own result back - and `store_answer` leaves a
# wake it already stored alone. A park's dump, the resume that takes it and a fresh rung replace
# the run's state, never the list; a settled step drops both (`PlanStep.transition`). The key,
# `HANDBACKS`, lives with the row: a replan reads it to keep a step that is waiting out a pace.
WOKE = "woke"
# What a crash cut off on the device (`_Cut`), owed to the step's next fresh attempt: kept
# with the record through every park, pace and replacement of the run's state, until an attempt's
# `step.prompt` naming it commits - so neither a rung that dies before its prompt nor a crash loses it.
OWED_CUT = "owed_cut"
# What a provider death's backoff keeps for the attempt after it (`parks.back_off`): the gotchas and
# their kinds, restored when the paced step re-enters `ready` (`entry._by_state`).
BACKOFF = "backoff"


def owed_run(step: PlanStep) -> OwedRun | None:
    saved = (step.resume_state or {}).get(OWED_RUN)
    return OwedRun.model_validate(saved) if saved is not None else None


def owe_run(step: PlanStep, owed: OwedRun | None) -> None:
    """Put `owed` on the row's record (None: nothing owed any more); the caller commits."""
    state = {k: v for k, v in (step.resume_state or {}).items() if k != OWED_RUN}
    if owed is not None:
        state[OWED_RUN] = owed.model_dump(mode="json")
    step.resume_state = state or None


def owed_gotchas(saved: Mapping[str, Any]) -> tuple[list[str], list[FailureKind]]:
    """What a park or a pace kept for the attempt after it: the last failures' gotchas and kinds."""
    return [str(g) for g in saved.get("gotchas") or []], [FailureKind(k) for k in saved.get("failures") or []]


def handbacks(step: PlanStep) -> list[str]:
    return list((step.resume_state or {}).get(HANDBACKS, []))


def woke(step: PlanStep) -> int:
    return int((step.resume_state or {}).get(WOKE, 0))


def with_record(step: PlanStep, state: dict[str, Any] | None) -> dict[str, Any] | None:
    """`state` as the row's next `resume_state`, the workflow's hand-back record kept."""
    record = {
        k: v for k, v in (step.resume_state or {}).items() if k in (HANDBACKS, WOKE, OWED_CUT, OWED_RUN)
    }
    return {**record, **state} if state else (record or None)


def hand_back(step: PlanStep, entry: str) -> str:
    """Record a hand-back on the row (the caller commits it before returning) and say it."""
    step.resume_state = {**(step.resume_state or {}), HANDBACKS: [*handbacks(step), entry]}
    return reissue(entry)


def reissue(entry: str) -> str:
    """A recorded hand-back in the workflow's words: the park's gate, or what is left of the pace -
    at least a second, since DBOS journals no sleep of no time (a replayed sleep waits on its own
    recorded deadline anyway)."""
    kind, _, value = entry.partition(":")
    if kind == "park":
        return f"{ASKING}{value}"
    left = (dt.datetime.fromisoformat(value) - utcnow()).total_seconds()
    return f"{PACING}{max(1, math.ceil(left))}"
