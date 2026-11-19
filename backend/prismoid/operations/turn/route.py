# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A new turn, routed: the code-made recipe intake names, else the Lead's one step (ADR-092).

Intake reads the turn's rows once (its dials, never its words) and names the recipe that answers with no
model call: a direct Bot's or a fleet's first turn, a long-form size dial, a deliverable dial its one step
reaches. Every other turn is the Lead's: one frameless step that decides how the turn ends. A recipe wears
`route_source=dial`, the Lead's step `route_source=lead`. `run_task` asks `lead_decides` first, on the same
rows, so a Lead turn skips the intake round: the Lead asks for itself.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.frames.layout import input_paths
from prismoid.ai.rules.plan import Plan
from prismoid.ai.skills.deliverables import document_formats
from prismoid.db.models.task import Task
from prismoid.errors import NotFound
from prismoid.operations import context as context_ops
from prismoid.operations import longform as longform_ops
from prismoid.operations import thread as thread_ops
from prismoid.operations.execution import mounts
from prismoid.operations.execution.lead_rung import LEAD
from prismoid.operations.plan_shapes import lead_plan, one_step_plan
from prismoid.operations.planning_steps import TurnAsk, bot_roster
from prismoid.operations.planning_turns import Routed, consensus_architects, fleet_rung
from prismoid.operations.turn import recipes
from prismoid.operations.turn.intake import (
    Bypass,
    TurnRows,
    bypass_of,
    intake,
    read_rows,
    turn_kind,
    turn_size,
)
from prismoid.schemas import sizing
from prismoid.schemas.inputs import InputSet, TaskInput
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.sizing import SizeBucket

LEAD_WHY: Final = "the Lead decides how this turn ends"


async def lead_decides(session: AsyncSession, task: Task) -> bool:
    """Whether the Lead decides this turn (no recipe answers it); reads rows, writes nothing."""
    return (await intake(session, task)).bypass is None


async def route_turn(session: AsyncSession, task: Task, ask: TurnAsk) -> Routed:
    """This turn's steps and routed record, nothing written but the row's size stamp."""
    rows = await read_rows(session, task)  # the rows `lead_decides` read
    bypass = bypass_of(rows)
    inputs = InputSet(items=[TaskInput.model_validate(i) for i in task.inputs])
    transcript = await thread_ops.render_thread(session, task, budget_tokens=thread_ops.PLANNER_THREAD_TOKENS)
    # Read only: the freeze lands in `commit_plan`, so no row lock spans a model call.
    pack = await context_ops.assemble(session, task, thread=transcript)
    message = ask.message or task.goal
    lane = (
        await _recipe(session, task, bypass, rows=rows, inputs=inputs, message=message)
        if bypass is not None
        else None
    )
    if lane is None:
        return Routed.of(recipes.lane_of(lead_plan(message), "express", LEAD, LEAD_WHY), pack)
    return Routed.of(replace(lane, source="dial"), pack)


async def _recipe(
    session: AsyncSession, task: Task, bypass: Bypass, *, rows: TurnRows, inputs: InputSet, message: str
) -> recipes.Lane | None:
    """The code-made plan `bypass` names; None when its rung finds the turn is not its own after all."""
    match bypass:
        case Bypass.DIRECT:
            return await direct_rung(session, task, message, inputs)
        case Bypass.FLEET:
            return await fleet_rung(session, task, message, inputs)
        case Bypass.LONGFORM:
            return await longform_rung(session, task, rows, message, inputs)
        case Bypass.DIAL:
            kind = turn_kind(rows)
            return (
                None
                if kind is None
                else recipes.dial_lane(
                    kind,
                    message=message,
                    inputs=inputs.items,
                    deliverable_format=task.deliverable_format,
                    scale=task.scale,
                    target_pages=task.target_pages,
                )
            )


async def direct_rung(
    session: AsyncSession, task: Task, message: str, inputs: InputSet
) -> recipes.Lane | None:
    """The DM lane: a direct task's work turn is answered by its one bot at the pinned version, a code-made
    plan (its text built into a dialed file format after it); a bot that no longer resolves fails the turn honestly."""
    if not task.direct_bot:
        return None
    bot_defs = await bot_roster(session, task)
    direct = next((a for a in bot_defs if str(a.name) == task.direct_bot), None)
    if direct is None:
        raise NotFound(
            f"{task.direct_bot!r} is not on this task's roster any more - it was archived or "
            "unshared. The conversation stays readable; publish the bot again to continue it."
        )
    doc_format = task.deliverable_format
    plan = one_step_plan(
        message,
        inputs.items,
        agent=task.direct_bot,
        produces=direct.produces,
        doc_format=None if direct.publishes_output else doc_format,
    )
    # A bot whose text the system publishes writes markdown: a dialed file format is the format maker's build.
    if direct.publishes_output and doc_format is not None and doc_format in document_formats():
        plan = recipes.rendered(plan, doc_format)
    return recipes.lane_of(
        plan, "direct", "dial", f"the {task.direct_bot} DM lane - only this bot answers here"
    )


async def longform_rung(
    session: AsyncSession, task: Task, rows: TurnRows, message: str, inputs: InputSet
) -> recipes.Lane | None:
    """The long-form lane: one outline step now, the parts at its completion; the size dial's bucket and
    count are stamped on the row so retries and replans read the same size."""
    size = turn_size(rows)
    if size is None:
        return None
    bucket, units = size
    task.scale, task.target_pages = bucket.value, units
    kind = turn_kind(rows) or ArtifactType.DOCUMENT
    spec = sizing.spec_for(kind, bucket)
    pages = units or (spec.target_lo + spec.target_hi) // 2
    plan = await longform_shape(session, task, message, inputs, kind=kind, bucket=bucket, target_pages=pages)
    return recipes.lane_of(
        plan, "longform", "dial", f"sized for the long-form lane ({bucket.value}, ~{units or '?'} pp)"
    )


async def longform_shape(
    session: AsyncSession,
    task: Task,
    goal: str,
    inputs: InputSet,
    *,
    kind: ArtifactType,
    bucket: SizeBucket,
    target_pages: int,
) -> Plan:
    """The long-form lane's first steps for `goal`: its outline, or at `quality=max` the proposals and their merge."""
    return longform_ops.longform_plan(
        goal,
        inputs.items,
        target_pages=target_pages,
        consensus=await consensus_architects(session, task),
        kind=kind,
        bucket=bucket,
        doc_format=(task.deliverable_format if kind is ArtifactType.DOCUMENT else None),
        # A revision keeps or rewrites the last document's sections: its outline step reads their text.
        prior_sections=input_paths(
            mounts.upstream_filename(a) for a in await mounts.prior_sections(session, task)
        ),
    )
