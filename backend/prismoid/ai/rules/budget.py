# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The budget envelope: one per step, and the one place that decides whether a step may keep spending.

Money is the task's live spend against its ceilings, never an estimate of what a run has done: the
ledger holds only what providers billed, so a replayed response costs nothing and moves nothing.
`over` is the decision, taken before work is bought - at the frontier, before a step's further
attempt, and when a gate's accept would buy a wave - and `reached` is the same decision on the run's
own figures, which the run guard reads at every safe point and a `partial` ending is parked on.
"Over" is `>=`: a ceiling is the last dollar a task may spend, not the first it may not. A step has no
dollar ceiling of its own: a plan's estimate is what a person is shown, never a stop.

Requests and time are the step's `Envelope`. The request ceiling is pydantic-ai's `UsageLimits`, set
per segment: `UsageLimits` count replayed responses, so a `Continue` gets fresh limits and a replayed
segment trips only where its original run did. Tokens are not capped on their own: the meter prices
every one into the spend. The wall bounds the whole attempt, replay included, and its web tools read
it as `TaskDeps.deadline` (`ai/tools/wall.py`).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable
from dataclasses import dataclass, replace
from decimal import Decimal

from pydantic_ai import RunContext
from pydantic_ai.usage import UsageLimits

from prismoid.ai import config
from prismoid.ai.deps import TaskDeps
from prismoid.schemas.plan import WORKSPACE_SCOPE, BudgetFigures


@dataclass(frozen=True, slots=True)
class BudgetVerdict:
    """The ceiling that stops the spend: what it has spent (never the projection added to it), its
    limit, which ceiling it is, and the coming wave's estimate that counted against it (0 for none).
    `WORKSPACE_SCOPE` is the workspace's allowance floor (ADR-103), in the allowance's unit: `limit`
    is the floor, 0, and `spent` how far below it the workspace is."""

    spent: Decimal
    limit: Decimal
    scope: str  # "task", "project" or `WORKSPACE_SCOPE`: which stops it, for the gate's explanation
    projected: Decimal = Decimal(0)

    @property
    def exhausted(self) -> bool:
        """The spend alone reached the ceiling, with no wave counted."""
        return self.spent >= self.limit


def over(
    *,
    task_limit: Decimal | None,
    task_spent: Decimal,
    project_limit: Decimal | None = None,
    project_spent: Decimal = Decimal(0),
    projection: Decimal = Decimal(0),
    workspace_left: Decimal | None = None,
) -> BudgetVerdict | None:
    """The ceiling the spend plus `projection` reaches, the task's before its Project's (the more
    specific promise); None when nothing stops it. An unset ceiling never constrains.

    The workspace's allowance comes first (`workspace_left`, ADR-103): a workspace with none left
    stops every task in it, whatever the task's or the Project's ceiling says. It is a floor in the
    allowance's unit, not a ceiling in dollars, so the projection does not count against it. `None`
    is a deployment that sets no allowance."""
    if workspace_left is not None and workspace_left <= 0:
        return BudgetVerdict(spent=Decimal(0) - workspace_left, limit=Decimal(0), scope=WORKSPACE_SCOPE)
    if task_limit is not None and task_spent + projection >= task_limit:
        return BudgetVerdict(spent=task_spent, limit=task_limit, scope="task", projected=projection)
    if project_limit is not None and project_spent + projection >= project_limit:
        return BudgetVerdict(spent=project_spent, limit=project_limit, scope="project", projected=projection)
    return None


def reached(figures: BudgetFigures | None) -> bool:
    """May the run buy nothing more? Its workspace spent its allowance (`exhausted`, ADR-103), or its
    live spend reached its task's cap. False with no figures, or no cap and allowance left."""
    if figures is None:
        return False
    if figures.exhausted:
        return True
    if figures.ceiling_usd <= 0:
        return False
    return over(task_limit=figures.ceiling_usd, task_spent=figures.spent_usd) is not None


def requests_left(ctx: RunContext[TaskDeps]) -> int | None:
    """The model requests this segment's ceiling still allows; None when it has no ceiling."""
    limit = ctx.usage_limits.request_limit if ctx.usage_limits is not None else None
    return None if limit is None else max(0, limit - ctx.usage.requests)


@dataclass(frozen=True, slots=True)
class Envelope:
    """A step's request ceiling per segment (None: the library's own) and its wall in seconds."""

    requests: int | None
    wall_s: float

    @classmethod
    def for_step(cls, *, guarded: bool) -> Envelope:
        """The deployment's envelope: a guarded agent gets its own request ceiling, every agent the wall."""
        worker = config.current().worker
        return cls(worker.request_ceiling if guarded else None, float(worker.agent_wall_clock_s))

    def narrowed(self, wall_s: float) -> Envelope:
        """The same envelope with a shorter wall, for a lane that must answer sooner."""
        return replace(self, wall_s=min(self.wall_s, wall_s))

    def limits(self) -> UsageLimits | None:
        """One segment's limits, fresh each time."""
        return None if self.requests is None else UsageLimits(request_limit=self.requests)

    async def within[T](self, deps: TaskDeps, attempt: Awaitable[T]) -> T:
        """The attempt inside the wall: its tools see the deadline, and past it `TimeoutError`."""
        deps.deadline = time.monotonic() + self.wall_s
        return await asyncio.wait_for(attempt, timeout=self.wall_s)
