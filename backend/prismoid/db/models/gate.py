# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Gate - a designed human interrupt (`approve | input | budget | connect | pause`) that parks a job
until resolved.

A gate costs nothing while it waits. The job is parked on `DBOS.recv`, not spinning - minutes or
days at ~zero cost - which is what makes "ask the human" a cheap move rather
than a last resort, and is the difference between a system that asks and one that guesses.

`input` gates carry a numbered QuestionSet with an expiry and an `on_timeout` policy; the three
actions (accept | decline | cancel) resolve every kind, with approval as the degenerate case.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import ColumnElement, ForeignKey, Index, String, and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, uuid_pk, vocabulary
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.task import Task
from prismoid.schemas.device_away import DEVICE_AWAY
from prismoid.schemas.plan import WORKSPACE_SCOPE, GateKind, GateState


class Gate(Base):
    __tablename__ = "gates"
    __table_args__ = (
        vocabulary("kind", GateKind),
        vocabulary("state", GateState),
        Index("ix_gates_state", "state"),
        Index("ix_gates_task_id", "task_id"),
        # `step_id` is a CASCADE FK to `plan_steps`: a replan drops steps and the gate rows go with
        # them - unindexed that cascade full-scanned the table (audit finding), and the resume path
        # reads it (`latest_for_step`/`count_for_step`). Partial - only step-bound gates need it.
        Index("ix_gates_step_id", "step_id", postgresql_where=text("step_id IS NOT NULL")),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    # The step the job is parked on. `DBOS.recv(topic=f"gate:{step_id}")` is the other half.
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("plan_steps.id", ondelete="CASCADE"), default=None
    )

    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=GateState.OPEN)

    spec: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)  # GateSpec
    resolution: Mapped[dict[str, Any] | None] = mapped_column(default=None)  # GateResolution

    expires_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    resolved_at: Mapped[dt.datetime | None] = mapped_column(default=None)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def get_in_workspace(cls, session: AsyncSession, gate_id: uuid.UUID) -> Gate | None:
        """A gate by id, fenced through its task to the bound workspace (a child fences through
        its root). Another workspace's gate is None - indistinguishable from one that never existed."""
        return await session.scalar(
            select(cls)
            .join(Task, Task.id == cls.task_id)
            .where(cls.id == gate_id, Task.workspace_id == tenant.workspace_of(session))
        )

    @classmethod
    async def open_for(cls, session: AsyncSession, actor: Actor) -> list[Gate]:
        """Every gate waiting on this human, newest last - the queue, not the notification.

        Scoped through the task, because a gate is not a side door: you can answer a question about
        a task you can see, and about no other. A PAUSE gate is not in it: nobody answers a pause,
        the task's resume lifts it.
        """
        return list(
            await session.scalars(
                select(cls)
                .join(Task, Task.id == cls.task_id)
                .where(
                    cls.state == GateState.OPEN.value,
                    cls.kind != GateKind.PAUSE.value,
                    Task.workspace_id == tenant.workspace_of(session),  # the bound workspace's queue
                    Task.creator_actor_id == actor.id,
                )
                .order_by(cls.created_at)
            )
        )

    @classmethod
    async def for_task(cls, session: AsyncSession, task_id: uuid.UUID) -> list[Gate]:
        return list(await session.scalars(select(cls).where(cls.task_id == task_id).order_by(cls.created_at)))

    @classmethod
    async def expired(cls, session: AsyncSession, now: dt.datetime) -> list[Gate]:
        """Open gates whose clock has run out. Indexed on state; `expires_at` is a real column so
        this is a range scan, not a full-table sweep of JSONB."""
        return list(
            await session.scalars(
                select(cls).where(
                    cls.state == GateState.OPEN.value,
                    cls.expires_at.is_not(None),
                    cls.expires_at <= now,
                )
            )
        )

    @classmethod
    async def control_open_for(
        cls, session: AsyncSession, task_ids: set[uuid.UUID], now: dt.datetime
    ) -> set[uuid.UUID]:
        """A platform read (the Machine sweep's): which of these tasks
        have an open CONTROL gate still inside its TTL - a person asked to the browser, not yet
        answered. One past `expires_at` the gate sweep has yet to close holds nothing awake."""
        if not task_ids:
            return set()
        return set(
            await session.scalars(
                select(cls.task_id).where(
                    cls.task_id.in_(task_ids),
                    cls.state == GateState.OPEN.value,
                    cls.kind == GateKind.CONTROL.value,
                    or_(cls.expires_at.is_(None), cls.expires_at > now),
                )
            )
        )

    @classmethod
    async def open_for_computer_use(cls, session: AsyncSession) -> list[Gate]:
        """A platform read (the gate sweep's kill-switch arm): every open CONTROL gate
        and every open browser-sourced APPROVE gate, in every workspace - what the switch declines. A
        connector's approval (`source` "connector", or a row from before the field) is not here."""
        return list(
            await session.scalars(
                select(cls).where(
                    cls.state == GateState.OPEN.value,
                    or_(
                        cls.kind == GateKind.CONTROL.value,
                        and_(cls.kind == GateKind.APPROVE.value, cls.spec["source"].astext == "browser"),
                    ),
                )
            )
        )

    @classmethod
    async def device_away_before(cls, session: AsyncSession, cutoff: dt.datetime) -> list[Gate]:
        """A platform read (the gate sweep's gone-for-good arm): every
        open PAUSE gate of a step waiting for its device (`device_away:` reason), opened before
        `cutoff`, in every workspace."""
        return list(
            await session.scalars(
                select(cls).where(
                    cls.state == GateState.OPEN.value,
                    cls.kind == GateKind.PAUSE.value,
                    cls.spec["reason"].astext.startswith(DEVICE_AWAY, autoescape=True),
                    cls.created_at <= cutoff,
                )
            )
        )

    @classmethod
    async def open_budget_count(cls, session: AsyncSession) -> int:
        """Every task parked on money, system-wide - the admin alerts' read."""
        return int(
            await session.scalar(
                select(func.count()).where(cls.kind == "budget", cls.state == GateState.OPEN.value)
            )
            or 0
        )

    @classmethod
    async def lock_pause_slots(cls, session: AsyncSession) -> None:
        """Take the deployment's pause-slot lock until the caller's commit: two steps parking at
        once queue here, so the second counts the first's gate (`open_pause_count`)."""
        await session.execute(select(func.pg_advisory_xact_lock(func.hashtext("gates:pause-slots"))))

    @classmethod
    async def open_pause_count(cls, session: AsyncSession) -> int:
        """Every step parked on a pause, system-wide: each holds a queue slot, and the
        step inbox parks a new one only under `settings.steering.max_pause_parks`. A
        step waiting for its device (a `device_away:` reason) is not a
        person's pause and never uses up the cap people pause under."""
        reason = cls.spec["reason"].astext
        return int(
            await session.scalar(
                select(func.count()).where(
                    cls.kind == GateKind.PAUSE.value,
                    cls.state == GateState.OPEN.value,
                    or_(reason.is_(None), ~reason.startswith(DEVICE_AWAY, autoescape=True)),
                )
            )
            or 0
        )

    @classmethod
    async def open_budget_exists(
        cls, session: AsyncSession, task_id: uuid.UUID, *, step_id: uuid.UUID | None = None
    ) -> bool:
        """An open budget gate on the task - or, for a step's ladder (`step_id`), on that step:
        a sibling parked on its own gate must not wave this step's next attempt through."""
        query = select(cls.id).where(
            cls.task_id == task_id,
            cls.state == GateState.OPEN.value,
            cls.kind == "budget",
        )
        if step_id is not None:
            query = query.where(cls.step_id == step_id)
        return await session.scalar(query) is not None

    @classmethod
    async def open_budget_for(cls, session: AsyncSession, task_id: uuid.UUID) -> Gate | None:
        """The task's open budget gate, if any (at most one: `check_budget` never piles another on) -
        what a raise hands back to a caller that resumes (B3)."""
        return await session.scalar(
            select(cls)
            .where(cls.task_id == task_id, cls.state == GateState.OPEN.value, cls.kind == "budget")
            .limit(1)
        )

    @classmethod
    def _allowance_park(cls) -> ColumnElement[bool]:
        """An open budget gate that parked because its workspace spent its allowance (the Bursar's
        `workspace` scope, ADR-103)."""
        return and_(
            cls.state == GateState.OPEN.value,
            cls.kind == GateKind.BUDGET.value,
            cls.spec["budget_scope"].astext == WORKSPACE_SCOPE,
        )

    @classmethod
    async def open_allowance_parks(cls, session: AsyncSession, workspace_id: uuid.UUID) -> list[Gate]:
        """Every allowance park in the workspace, oldest first: what the allowance coming back wakes.
        The workspace is named, not read from the session: a wake may run on a platform session, the
        sweep on its own."""
        return list(
            await session.scalars(
                select(cls)
                .join(Task, Task.id == cls.task_id)
                .where(Task.workspace_id == workspace_id, cls._allowance_park())
                .order_by(cls.created_at)
            )
        )

    @classmethod
    async def workspaces_with_allowance_parks(cls, session: AsyncSession) -> list[uuid.UUID]:
        """Every workspace holding an allowance park, across workspaces: the gate sweep's allowance
        arm reads this one list, then decides per workspace."""
        return list(
            await session.scalars(
                select(Task.workspace_id)
                .join(cls, cls.task_id == Task.id)
                .where(cls._allowance_park())
                .distinct()
            )
        )

    @classmethod
    async def lock_allowance_parks(
        cls, session: AsyncSession, workspace_id: uuid.UUID, *, shared: bool
    ) -> None:
        """The workspace's allowance-park lock, held to commit (ADR-103). A park that found the
        allowance spent takes it `shared` before it reads it again and opens its gate; a wake takes it
        alone before it lists the parks. So a park still opening is either listed or sees the
        allowance back. Advisory: it orders against no row lock, so it cannot invert the Machine, task
        or workspace row order."""
        lock = func.pg_advisory_xact_lock_shared if shared else func.pg_advisory_xact_lock
        await session.execute(select(lock(func.hashtext(f"gates:allowance-parks:{workspace_id}"))))

    @classmethod
    async def budget_gate_titled(cls, session: AsyncSession, task_id: uuid.UUID, title: str) -> Gate | None:
        """The newest budget gate wearing this title, any state - how a once-per-task checkpoint
        (the long-form cost confirmation) knows it already asked."""
        rows = await session.scalars(
            select(cls).where(cls.task_id == task_id, cls.kind == "budget").order_by(cls.created_at.desc())
        )
        for row in rows:
            if (row.spec or {}).get("title") == title:
                return row
        return None

    @classmethod
    async def open_for_task(cls, session: AsyncSession, task_id: uuid.UUID) -> list[Gate]:
        return list(
            await session.scalars(
                select(cls).where(cls.task_id == task_id, cls.state == GateState.OPEN.value)
            )
        )

    @classmethod
    async def count_for_step(
        cls, session: AsyncSession, step_id: uuid.UUID, *, kind: str | None = None, source: str | None = None
    ) -> int:
        """How many gates this step has already opened - the ask/approval/connect/handoff budgets'
        ledger: a step that keeps asking is a step that never
        finishes. `kind` narrows to one budget - the counters must not eat each other (an
        approval is the product working; a question is the product stalling). `source` narrows
        further by `GateSpec.source`: a CONTROL gate the agent asked for (`handoff`) is budgeted,
        one a person's grab opened (`takeover`) is not the agent's spend."""
        query = select(func.count()).select_from(cls).where(cls.step_id == step_id)
        if kind is not None:
            query = query.where(cls.kind == kind)
        if source is not None:
            query = query.where(cls.spec["source"].astext == source)
        return (await session.scalar(query)) or 0

    @classmethod
    async def latest_for_step(cls, session: AsyncSession, step_id: uuid.UUID) -> Gate | None:
        """The step's newest gate - where the prompts live when a resume pairs answers back up."""
        return await session.scalar(
            select(cls).where(cls.step_id == step_id).order_by(cls.created_at.desc()).limit(1)
        )

    def is_open(self) -> bool:
        return self.state == GateState.OPEN

    @classmethod
    async def accepted_browser_grants(cls, session: AsyncSession, task_id: uuid.UUID) -> list[str]:
        """The hosts this task's own browser gates were granted for the whole task:
        `source == "browser"`, resolved `accept`, `content.scope == "task"` - the ONE shape an
        allowlist ask's accept can carry (`once`/`always` never reach this table; `always` writes
        an `EgressAllowance` row instead, read through `hosts_for_owner`). Task-scoped, never
        step-scoped: a later step on the same task inherits an earlier step's task grant."""
        rows = await session.scalars(
            select(cls).where(
                cls.task_id == task_id,
                cls.kind == GateKind.APPROVE.value,
                cls.state == GateState.RESOLVED.value,
                cls.spec["source"].astext == "browser",
                cls.resolution["action"].astext == "accept",
                cls.resolution["content"]["scope"].astext == "task",
            )
        )
        return sorted({str(r.spec.get("host", "")) for r in rows if r.spec.get("host")})
