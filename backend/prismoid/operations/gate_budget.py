# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Budget gates - the Bursar's park and the long-form cost confirmation.

`check_budget` consults the budget envelope (`ai/rules/budget.over`) at every
boundary a wave or a further attempt is bought, and parks the task on a `budget` gate when a
ceiling - the task's, or its Project's - is reached or the coming wave would pass it, or when its
workspace has spent its allowance (`allowance.py`, ADR-103: only the allowance coming back answers that
gate). The confirmation checkpoint is the same gate wearing different words. `advance_status` is the status change with its durable event that a
park needs. `gates.py` re-exports what callers use.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.rules.budget import BudgetVerdict, over
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.project import Project
from prismoid.db.models.task import Task
from prismoid.db.models.usage import ProjectUsageDaily
from prismoid.errors import NotFound
from prismoid.operations import allowance
from prismoid.operations import inbox as inbox_ops
from prismoid.operations import usage as usage_ops
from prismoid.operations.attribution import actor_ref
from prismoid.operations.budget_estimate import (
    confirmation_due,
    estimate_high,
    longform_projection,
    scatter_projection,
)
from prismoid.schemas import sizing
from prismoid.schemas.events import GateOpened, TaskBudgetSet
from prismoid.schemas.events import TaskStatusChanged as _StatusChanged
from prismoid.schemas.ids import GateId, ProjectId, TaskId
from prismoid.schemas.outputs import ArtifactType
from prismoid.schemas.plan import (
    WORKSPACE_SCOPE,
    BudgetFigures,
    GateKind,
    GateSpec,
    GateState,
    TaskStatus,
    said_usd,
)
from prismoid.schemas.usage import cap_is_default, usd_ceiling


async def advance_status(session: AsyncSession, task: Task, to: TaskStatus) -> None:
    """A status change with its durable event - the budget park needs `running → waiting`. Read
    under the task row lock: a paused task stays paused (`Task.stays_paused`), so a budget gate
    opened after a pause waits behind the person's resume."""
    task = await Task.held(session, task.id)
    if task.stays_paused(to):
        return
    task.transition(to)
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        _StatusChanged(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            status=to,
        ),
    )


async def check_budget(
    session: AsyncSession, task_id: uuid.UUID, *, step_id: uuid.UUID | None = None
) -> uuid.UUID | None:
    """Park on a `budget` gate when the ceiling is reached (before every wave, and before a step's further
    attempt with `step_id`); returns the gate id or None. Never opens a second gate for one caller."""
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFound("no such task")

    task_limit = budget_of(task.budget)
    task_spent = (await usage_ops.for_task(session, task_id)).total_usd
    project_limit, project_spent = await _project_ceiling(session, task)

    verdict = over(
        task_limit=task_limit,
        task_spent=task_spent,
        project_limit=project_limit,
        project_spent=project_spent,
        workspace_left=await _allowance_left(session, task),
    )
    _bucket = sizing.normalize(task.scale)
    _kind = ArtifactType(task.deliverable) if task.deliverable else ArtifactType.DOCUMENT
    if verdict is None and _bucket is not None and sizing.runs_longform(_kind, _bucket, task.target_pages):
        # THE LANE'S TWO MONEY LAWS, at the boundary the loop already owns:
        # (1) the confirmation checkpoint - before the FIRST section wave, when the outline's
        #     honest projection exceeds the threshold and no explicit budget already covers it,
        #     the user confirms with the REAL numbers (their chosen consent moment);
        # (2) the wave projection - the Bursar compares LIMIT against SPEND + THE COMING WAVE,
        #     because a 22-section wave can overrun a cap by the whole wave between boundaries.
        confirmation = await _longform_confirmation(session, task, task_limit, step_id)
        if confirmation is not None:
            return confirmation
        verdict = await longform_projection(
            session,
            task,
            task_limit,
            task_spent=task_spent,
            project_limit=project_limit,
            project_spent=project_spent,
        )
    if verdict is None:
        # THE SCATTER WAVE PROJECTION: the same money law, the gather lane's
        # wave - a re-scatter mints whole new seats BETWEEN boundaries, and the ceiling
        # check alone would dispatch them all at $0.01 under the cap (the
        # "per-seat budgets" once planned did not exist; this arm is the control).
        verdict = await scatter_projection(
            session,
            task,
            task_limit,
            task_spent=task_spent,
            project_limit=project_limit,
            project_spent=project_spent,
        )
    if verdict is None:
        return None

    if await Gate.open_budget_exists(session, task_id, step_id=step_id):
        return None  # already parked on a budget question; do not pile another on

    gate = await open_budget(session, task, verdict, step_id)
    return gate.id


async def _allowance_left(session: AsyncSession, task: Task) -> Decimal | None:
    """The workspace's allowance for the Bursar's floor; None where no allowance applies. Found spent,
    it is read again under the workspace's allowance-park lock (`Gate.lock_allowance_parks`, shared),
    left held to this session's commit - the gate's, when it parks. A wake takes that lock alone before
    it lists the parks, so it either waits and finds this gate, or committed first and this second
    read sees the allowance back: no park is opened after its wake has passed."""
    found = await allowance.left(session, task.workspace_id)
    if found is None or found > 0:
        return found
    await Gate.lock_allowance_parks(session, task.workspace_id, shared=True)
    return await allowance.left(session, task.workspace_id)


async def _project_ceiling(session: AsyncSession, task: Task) -> tuple[Decimal | None, Decimal]:
    """The task's Project's monthly ceiling and its month-to-date spend - the Bursar's Project inputs,
    read in one place for the boundary (`check_budget`) and a Project-scope accept (`resolve`).
    `(None, 0)` for a spaceless task."""
    if task.project_id is None:
        return None, Decimal(0)
    project = await session.get(Project, task.project_id)
    spent = await ProjectUsageDaily.month_to_date(session, task.project_id, on=dt.date.today())
    return (project.budget_usd if project else None), spent


async def project_over(
    session: AsyncSession, task: Task, figures: BudgetFigures | None
) -> BudgetVerdict | None:
    """The Bursar's verdict on this task's Project ceiling alone, counting the wave the gate parked
    (its projection; 0 when the spend alone hit a ceiling): an accept dispatches that wave with no
    re-check, so it is the wave the Project must hold. The task's own ceiling is the accept's consent,
    not asked here. None when nothing stops it, and for a task with no Project."""
    project_limit, project_spent = await _project_ceiling(session, task)
    return over(
        task_limit=None,
        task_spent=Decimal(0),
        project_limit=project_limit,
        project_spent=project_spent,
        projection=figures.projected_usd if figures else Decimal(0),
    )


BUDGET_TITLE = "Budget reached"  # the Bursar's park - the one budget gate a raise resumes (B3)
PROJECT_STILL_SPENT = (
    "The Project's monthly budget is still spent; its owner can raise it in Project settings"
)
PROJECT_WAVE_TOO_BIG = (
    "The next wave would take the Project past its monthly budget; its owner can raise it in Project settings"
)
CONFIRM_TITLE = "Confirm the document's cost"  # the marker that makes the checkpoint once-per-task


async def _longform_confirmation(
    session: AsyncSession, task: Task, task_limit: Decimal | None, step_id: uuid.UUID | None
) -> uuid.UUID | None:
    """The expansion-time checkpoint: open (or re-return) the confirm gate before the first
    section wave. Fires once - a resolved confirm gate never re-asks; an OPEN one re-parks
    (that is the fix for the open-gate/proceed hole this path would otherwise inherit)."""
    existing = await Gate.budget_gate_titled(session, task.id, CONFIRM_TITLE)
    if existing is not None:
        return existing.id if existing.is_open() else None
    reason = await confirmation_due(session, task, task_limit)
    if reason is None:
        return None
    gate = await open_budget_gate(session, task, title=CONFIRM_TITLE, reason=reason, step_id=step_id)
    return gate.id


async def confirmation_ahead(session: AsyncSession, task: Task) -> bool:
    """Read-only twin of `_longform_confirmation`: will the next boundary (re)park this task
    on the cost checkpoint? The figures enqueue asks at outline completion so no
    image money is spent ahead of the user's consent - `resolve`'s accept releases the hold.
    The rare race (a budget raised between this read and the boundary) costs only the
    assembly fallback: the degrade ladder, never the task."""
    existing = await Gate.budget_gate_titled(session, task.id, CONFIRM_TITLE)
    if existing is not None:
        return existing.is_open()
    return await confirmation_due(session, task, budget_of(task.budget)) is not None


async def consent_to_estimate(session: AsyncSession, task: Task, actor: Actor) -> None:
    """Accepting the cost confirmation consents to its estimate. A cap the deployment defaulted (which
    nobody chose) rises to the estimate's high end, on the record as a budget set by the person who
    accepted, so the document they consented to is not stopped again by "Budget reached"; one that
    already covers it stays, and is now their consent. Never lowered. A cap a person set is theirs
    and stays. The caller holds the task's row lock and commits."""
    if not cap_is_default(task.budget):
        return
    previous = budget_of(task.budget)
    usd = (await estimate_high(session, task)).quantize(Decimal("0.01"))
    if previous is not None and previous >= usd:
        task.budget = person_cap(task.budget, previous)
        return
    task.budget = person_cap(task.budget, usd)
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        TaskBudgetSet(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            actor=actor_ref(actor),
            usd=usd,
            previous_usd=previous,
        ),
    )


def person_cap(budget: dict[str, Any] | None, usd: Decimal) -> dict[str, Any]:
    """The stored budget with a cap a person chose: `usd` set, and the default's marker gone, so the
    cap stands for their consent from now on (`cap_is_default`)."""
    return {**{k: v for k, v in (budget or {}).items() if k != "source"}, "usd": str(usd)}


def budget_of(budget: dict[str, Any] | None) -> Decimal | None:
    """The task's USD ceiling, or None when it has none - the Bursar's read, and the raise's
    `previous_usd` (B3)."""
    return usd_ceiling(budget)


async def open_budget(session: AsyncSession, task: Task, v: BudgetVerdict, step_id: uuid.UUID | None) -> Gate:
    """Open a `budget` gate, tell the owner, and move the task to `waiting`. Never a silent overrun.
    A gate the wave projection opened says what is spent and what the wave needs, apart: its spend
    is still under the ceiling, and "spent" must never count money not yet gone. A workspace whose
    allowance is spent wears its own title and words, and no dollars: the floor it hit is not this task's."""
    if v.scope == WORKSPACE_SCOPE:
        return await _open_allowance_spent(session, task, step_id)
    if v.projected:
        said = (
            f"{said_usd(v.spent)} spent; the next wave needs about {said_usd(v.projected)} "
            f"against the {v.scope} budget of {said_usd(v.limit)}"
        )
    else:
        said = f"the {v.scope} budget of {said_usd(v.limit)} is spent ({said_usd(v.spent)} so far)"
    return await open_budget_gate(
        session,
        task,
        title=BUDGET_TITLE,
        reason=f"{said}; raise it and resume, or cancel",
        budget_scope=v.scope,
        budget_figures=BudgetFigures(ceiling_usd=v.limit, spent_usd=v.spent, projected_usd=v.projected),
        step_id=step_id,
    )


async def _open_allowance_spent(
    session: AsyncSession, task: Task, step_id: uuid.UUID | None, *, waiting: bool = True
) -> Gate:
    """The "allowance spent" gate: the allowance's own title and words (`allowance.py`), and no
    dollars - the floor it hit is not this task's."""
    return await open_budget_gate(
        session,
        task,
        title=allowance.SPENT_TITLE,
        reason=allowance.SPENT_BODY,
        budget_scope=WORKSPACE_SCOPE,
        budget_figures=BudgetFigures(ceiling_usd=Decimal(0), spent_usd=Decimal(0), exhausted=True),
        step_id=step_id,
        waiting=waiting,
    )


async def hold_unstarted(session: AsyncSession, task_id: uuid.UUID, turn: int) -> uuid.UUID | None:
    """A queued turn whose workspace has spent its allowance parks before anything spends: no
    clarifier, no namer, no planner. It asks what `allowance.exhausted` asks, under the allowance-park
    lock like the boundary (`_allowance_left`), so a wake finds this gate. The turn never ran, so it
    keeps its status (`created`, or `planning` on a follow-up) and its resume moves nothing; the
    allowance coming back answers the gate like any other allowance park, and the turn starts.

    The gate id to hold on, or None to start: an open budget gate this turn already holds on (a
    re-run after a crash), else a new one. None for a settled or superseded turn. Commits when it parks."""
    task = await session.get(Task, task_id)
    if task is None:
        raise NotFound("no such task")
    if task.is_terminal() or task.turn != turn:
        return None
    if (parked := await Gate.open_budget_for(session, task_id)) is not None:
        return parked.id
    left = await _allowance_left(session, task)
    if left is None or left > 0:
        return None
    return (await _open_allowance_spent(session, task, None, waiting=False)).id


async def open_budget_gate(
    session: AsyncSession,
    task: Task,
    *,
    title: str,
    reason: str,
    budget_scope: str = "",
    budget_figures: BudgetFigures | None = None,
    step_id: uuid.UUID | None = None,
    waiting: bool = True,
) -> Gate:
    """The budget-gate ceremony, one home: the row, `waiting`, the inbox, the durable frame, the
    commit. "Budget reached" (the Bursar) and the long-form cost confirmation are the
    same machine wearing different words - the resolve/park/wake path cannot tell them apart,
    which is the point. `waiting=False` is a turn held before it started: it never ran, so it
    keeps its status."""
    gate = Gate(
        task_id=task.id,
        step_id=step_id,  # set when a step's ladder parks here: its answer wakes that step
        kind=GateKind.BUDGET.value,
        state=GateState.OPEN.value,
        spec=GateSpec(
            kind=GateKind.BUDGET,
            title=title,
            reason=reason,
            budget_scope=budget_scope,
            budget_figures=budget_figures,
        ).model_dump(mode="json"),
        expires_at=None,  # a budget question has no safe default and no clock; only a human answers
    )
    session.add(gate)
    await session.flush()

    if waiting:
        await advance_status(session, task, TaskStatus.WAITING)
    await inbox_ops.budget_reached(session, task, reason, gate_id=gate.id)

    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        GateOpened(
            seq=seq,
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=utcnow(),
            gate_id=GateId(gate.id),
            kind=GateKind.BUDGET,
            title=title,
            questions=None,
            reason=reason,  # B3: the dock showed an empty reason - the ceiling sentence was never sent
            budget_scope=budget_scope,
            budget_figures=budget_figures,
            expires_at=None,
        ),
    )
    await session.commit()
    logfire.info("task {task} parked on a budget gate: {reason}", task=str(task.id), reason=reason)
    return gate
