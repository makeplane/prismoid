# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What the run ahead is projected to cost: the long-form checkpoint's decision and the wave
projections the budget envelope's boundary reads (`gates.check_budget`). Reads only: nothing here opens a
gate or takes a lock.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.gateway.run_cost import seat_estimate, with_prices
from prismoid.ai.rules.budget import BudgetVerdict, over
from prismoid.ai.tools.providers.search import search_price
from prismoid.db.models.task import Task
from prismoid.schemas import sizing
from prismoid.schemas.longform import LaneEstimate
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.plan import PlanFacts, StepSpec, StepState
from prismoid.schemas.usage import cap_is_default


async def confirmation_due(session: AsyncSession, task: Task, task_limit: Decimal | None) -> str | None:
    """The checkpoint's decision, as data: the gate's reason when it should open, else None.
    One home, shared by the gate and by `gates.confirmation_ahead`, so they can never disagree."""
    from prismoid.conf import settings
    from prismoid.db.models.plan import PlanStep
    from prismoid.schemas.plan import StepSpec, StepState

    steps = await PlanStep.for_task(session, task.id)
    sections = [s for s in steps if StepSpec.model_validate(s.spec).lane_role == "section"]
    if not sections or any(s.step_state is StepState.DONE for s in sections):
        return None  # pre-expansion, or the burn already started - the checkpoint's moment passed

    est = await _lane_estimate(session, task)
    threshold = Decimal(str(settings.worker.longform_confirm_usd))
    if est.ceiling_usd <= threshold:
        return None  # small runs flow uninterrupted (the user's threshold choice)
    if task_limit is not None and task_limit >= est.ceiling_usd and not cap_is_default(task.budget):
        return None  # an explicit budget covering the projection IS the consent; a defaulted one is nobody's
    return (
        f"the outline is ready: {est.sections} sections, {est.figures} figures, "
        f"~${est.usd:.2f}–${est.ceiling_usd:.2f}. "
        "Accept to write the document, or cancel."
    )


async def estimate_high(session: AsyncSession, task: Task) -> Decimal:
    """The top of the lane's projection: the cost the confirmation names and a person accepts."""
    return (await _lane_estimate(session, task)).ceiling_usd


async def _lane_estimate(session: AsyncSession, task: Task) -> LaneEstimate:
    """The lane's projection for THIS task: its kind, bucket and units at the routed head rows' prices."""
    from prismoid.operations import longform
    from prismoid.schemas.outputs import ArtifactType as _Kind
    from prismoid.schemas.sizing import SizeBucket as _SB

    kind = _Kind(task.deliverable) if task.deliverable else _Kind.DOCUMENT
    bucket = sizing.normalize(task.scale) or _SB.LONG
    spec = sizing.spec_for(kind, bucket)
    return longform.lane_estimate(
        task.target_pages or (spec.target_lo + spec.target_hi) // 2,
        await head_book(session),
        kind=kind,
        bucket=bucket,
    )


async def head_book(session: AsyncSession) -> PlanFacts:
    """Each class's prices at the head of its routed chain, one search included, and its flex tier."""
    from prismoid.operations import models as model_ops
    from prismoid.operations import routing

    catalog, order = await model_ops.routed_catalog(session)
    settings = await routing.settings_for(session)
    return with_prices(PlanFacts(), catalog, search=search_price(), order=order, settings=settings)


async def measured_seats(
    session: AsyncSession, task_id: uuid.UUID, done: Sequence[uuid.UUID]
) -> list[Decimal]:
    """What this task's finished gather seats each spent - the map and synthesis are not seats."""
    from prismoid.db.models.usage import UsageEvent

    if not done:
        return []
    wanted = set(done)
    spend = await UsageEvent.task_spend(session, task_id)
    return [cost for step_id, cost in spend.by_step if step_id in wanted]


async def longform_projection(
    session: AsyncSession,
    task: Task,
    task_limit: Decimal | None,
    *,
    task_spent: Decimal,
    project_limit: Decimal | None,
    project_spent: Decimal,
) -> BudgetVerdict | None:
    """`over` the sections still to run - stop BEFORE the wave, at fan-out width."""
    from prismoid.db.models.plan import PlanStep
    from prismoid.schemas.plan import StepSpec, StepState

    if task_limit is None and project_limit is None:
        return None
    steps = await PlanStep.for_task(session, task.id)
    remaining = sum(
        1
        for s in steps
        if StepSpec.model_validate(s.spec).lane_role == "section"
        and s.step_state in (StepState.READY, StepState.PENDING)
    )
    if not remaining:
        return None
    est = await _lane_estimate(session, task)
    per_section = est.ceiling_usd / max(1, est.sections)
    return over(
        task_limit=task_limit,
        task_spent=task_spent,
        project_limit=project_limit,
        project_spent=project_spent,
        projection=(per_section * remaining).quantize(Decimal("0.01")),
    )


async def scatter_projection(
    session: AsyncSession,
    task: Task,
    task_limit: Decimal | None,
    *,
    task_spent: Decimal,
    project_limit: Decimal | None,
    project_spent: Decimal,
) -> BudgetVerdict | None:
    """`over` for the gather seats about to dispatch: priced at the fast-executor head's prices, raised to
    what this task's finished seats measured; `swarm_seat_est_usd` only when a price is unknown."""
    from prismoid.conf import settings
    from prismoid.db.models.plan import PlanStep

    if task_limit is None and project_limit is None:
        return None
    seats = [
        s
        for s in await PlanStep.for_task(session, task.id)
        if StepSpec.model_validate(s.spec).lane_role == "gather"
    ]
    remaining = sum(1 for s in seats if s.step_state in (StepState.READY, StepState.PENDING))
    if not remaining:
        return None
    done = [s.id for s in seats if s.step_state is StepState.DONE]
    per_seat = seat_estimate(
        await head_book(session),
        ModelClass.FAST_EXECUTOR,
        measured=await measured_seats(session, task.id, done),
        fallback=Decimal(str(settings.worker.swarm_seat_est_usd)),
    )
    return over(
        task_limit=task_limit,
        task_spent=task_spent,
        project_limit=project_limit,
        project_spent=project_spent,
        projection=(per_seat * remaining).quantize(Decimal("0.01")),
    )
