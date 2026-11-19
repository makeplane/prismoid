# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The expansions: a lane step's typed result fans the plan out, deterministically, in the step's
own completion transaction.

An outline lands → the long-form parts, in order; a scatter map → the gather wave and synthesis; a
Lead's ending → its rows staged (`turn/commit.py`) for the step's DONE commit. A result `expand`
refuses is a failed ATTEMPT - the message is the retry gotcha, exactly like a produce failure - never a
stuck plan and never a silent one-step fallback. Run outside the attempt's try on purpose: the
escalation raise must reach the workflow, not the attempt's denied arm.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import ModuleType
from typing import Any

import logfire

from prismoid.db.models.plan import MAX_ATTEMPTS
from prismoid.errors import StepFailed
from prismoid.operations import longform as longform_ops
from prismoid.operations import swarm as swarm_ops
from prismoid.operations.execution import finish
from prismoid.operations.execution.handlers.state import Ladder, Ran, Round, go_round
from prismoid.operations.turn import commit as turn_commit
from prismoid.operations.turn.commit import Committing
from prismoid.schemas.longform import OutlineResult
from prismoid.schemas.swarm import ScatterResult


@dataclass(frozen=True)
class Expansion:
    """One lane's fan-out: the lane, the result it expands, how, and what a refusal says."""

    lane_role: str
    result: type
    noun: ModuleType  # read per call, so the noun's verb is the one in place now
    verb: str
    refused: type[Exception]
    log: str  # the expansion's log line
    gotcha: str  # the retry gotcha's lead
    escalated: str  # the escalation's words, given the attempts


EXPANSIONS = (
    Expansion(
        "outline",
        OutlineResult,
        longform_ops,
        "expand_after_outline",
        longform_ops.InvalidOutline,
        "longform: expanded {n} steps for task {t}",
        "- **the outline cannot expand.** ",
        "the outline could not expand in {n} attempts - see the diagnosis on the stream",
    ),
    Expansion(
        "scatter_map",
        ScatterResult,
        swarm_ops,
        "expand_after_scatter",
        swarm_ops.InvalidScatter,
        "scatter: expanded {n} steps for task {t}",
        "- **the map cannot expand.** ",
        "the scatter map could not expand in {n} attempts - see the diagnosis on the stream",
    ),
)


async def expand(ladder: Ladder, ran: Ran) -> Round | None:
    """`AGAIN` when the result could not expand and the step goes round, else None (expanded, or
    nothing to expand). Raises once the refusals spent the ladder."""
    if ran.undelivered is not None:
        return None
    for lane in EXPANSIONS:
        if ladder.spec.lane_role == lane.lane_role and isinstance(ran.outline, lane.result):
            again = await _expand(ladder, lane, ran.outline)
            if again is not None:
                return again
    if ladder.spec.lane_role == "express" and isinstance(ran.outline, Committing):
        committed = await turn_commit.commit_ending(ladder.session, ladder.task, ladder.step, ran.outline)
        ran.narration, ran.interim = committed.narration, committed.interim
    return None


async def _expand(ladder: Ladder, lane: Expansion, result: Any) -> Round | None:
    session, task, step = ladder.session, ladder.task, ladder.step
    try:
        expand: Callable[..., Awaitable[int]] = getattr(lane.noun, lane.verb)
        added = await expand(session, task, step, result)
        logfire.info(lane.log, n=added, t=str(task.id))
    except lane.refused as bad:
        gotcha = f"{lane.gotcha}{bad}"
        ladder.gotchas.append(gotcha)
        if step.model_attempts() >= MAX_ATTEMPTS:
            await finish.escalate(session, task, step, ladder.spec, ladder.gotchas)
            await session.commit()
            raise StepFailed(lane.escalated.format(n=step.attempt)) from None
        return await go_round(ladder, gotcha)
    return None
