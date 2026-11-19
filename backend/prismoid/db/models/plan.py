# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The plan: PlanStep (a node), PlanEdge (a typed dependency), PlanEdit (an append-only replan).

Three tables, one file, because they are one thing: a plan is a graph and its history. A replan
is an **edit with history**, never a fresh plan - which is why `plan_edits` is append-only
and why "what did it change its mind about, and when" is answerable at all.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import ForeignKey, Index, String, UniqueConstraint, delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, uuid_pk, vocabulary
from prismoid.db.models import tenant
from prismoid.errors import InvalidTransition
from prismoid.schemas.plan import StepKind, StepState

# The step lifecycle (ADR-019):  pending ▶ ready ▶ running ▶ done | retrying(n≤3) | escalated | skipped
# `running → done` is the STANDARD lane: published means done, and the verification receipt is
# signed after delivery by the `verify_artifact` job. `verifying` survives as the FLEET's
# in-line lane (a seat's corrective rung is part of the mode's contract).
_ALLOWED: Final[dict[StepState, frozenset[StepState]]] = {
    StepState.PENDING: frozenset({StepState.READY, StepState.SKIPPED}),
    StepState.READY: frozenset({StepState.RUNNING, StepState.SKIPPED}),
    # `skipped` from a run: the task ended under it (failed, cancelled) and its safe point stopped it.
    StepState.RUNNING: frozenset(
        {StepState.VERIFYING, StepState.DONE, StepState.RETRYING, StepState.ESCALATED, StepState.SKIPPED}
    ),
    StepState.VERIFYING: frozenset(
        {StepState.DONE, StepState.RETRYING, StepState.ESCALATED, StepState.SKIPPED}
    ),
    # A retry re-enters the machine at `ready` - it is a fresh attempt, not a resumed one, which
    # is what makes the attempt counter meaningful and the frame snapshot the unit of recovery.
    StepState.RETRYING: frozenset({StepState.READY, StepState.ESCALATED}),
    StepState.DONE: frozenset(),
    # A human resolved it (ready - try again), or the plan tolerates the loss and moves on
    # (skipped - a fleet seat's failure costs the seat, never the fleet).
    StepState.ESCALATED: frozenset({StepState.READY, StepState.SKIPPED}),
    StepState.SKIPPED: frozenset(),
}

MAX_ATTEMPTS: Final = 3  # ≤3 targeted retries, then escalate with a diagnosis
# The `resume_state` key of the step workflow's hand-back record (`execution.record`): read here too,
# because a step waiting out a pace is in flight however its state column reads.
HANDBACKS: Final = "handbacks"


def paced(state: str, resume_state: dict[str, Any] | None) -> bool:
    """A `ready` step whose last hand-back is a pace: its workflow is asleep on it, alive."""
    record = (resume_state or {}).get(HANDBACKS) or []
    return state == StepState.READY.value and bool(record) and str(record[-1]).startswith("pace:")


class PlanStep(Base):
    """A node in a Task's plan (`step | gate | artifact`). A plan node, not a durability
    primitive - durability is the job's, and the job's alone."""

    __tablename__ = "plan_steps"
    __table_args__ = (
        vocabulary("kind", StepKind),
        vocabulary("state", StepState),
        Index("ix_plan_steps_task_id_state", "task_id", "state"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)

    # **The plan's order is a fact about the plan, not a fact about the clock.** These rows were
    # sorted by `created_at`, which is `server_default=now()` - and Postgres' `now()` is
    # *transaction-stable*, so every step in a plan carries the identical timestamp and the sort was
    # whatever the index felt like that morning. `tasks show` duly printed "#1 (after #3)". The
    # executor was right only because it iterated the insert list rather than re-reading.
    #
    # The policy layer returns steps in topological order. That order is written down here, once, and
    # every reader agrees with every other reader.
    ordinal: Mapped[int] = mapped_column(nullable=False, default=0)

    kind: Mapped[str] = mapped_column(String(16), nullable=False, default=StepKind.STEP)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default=StepState.PENDING)
    title: Mapped[str] = mapped_column(String(400), nullable=False, default="")

    # The StepSpec value (schemas/plan.py) - agent name, contract, skill pins. ORM rows never
    # reach an agent; this JSONB, validated back into a StepSpec, is what does.
    spec: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)

    attempt: Mapped[int] = mapped_column(nullable=False, default=0)

    # The subset of `attempt` that was INFRASTRUCTURE - a worker died mid-step, a parked frame
    # was reaped. `attempt` keeps total-sandboxes-ever (frame-name uniqueness);
    # `model_attempts()` is what every retry cap reads: a restart never spends the user's budget.
    infra_attempts: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    # The infra deaths the ladder itself caught - a provider that failed, a sandbox or service that
    # stopped answering, a wall-clock stop: the part of `infra_attempts` the infra cap reads
    # (`settings.worker.max_infra_attempts`). A worker restart and a steer's re-run stay out of it.
    infra_failures: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")

    # The epoch this step was drafted under (unstarted) or has applied (running and after), and who
    # opened it - the step's spend label. A fresh attempt takes the task's; each steer
    # the run takes advances both.
    epoch: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    epoch_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )
    # the epoch the step's turn opened at - the verify receipt's floor
    turn_epoch: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")

    def model_attempts(self) -> int:
        """How many attempts the MODEL has actually had - the ladder's honest denominator."""
        return max(0, self.attempt - self.infra_attempts)

    # **Where a paused agent's mind is kept**. When `ask_user` defers, the run's whole
    # message history is serialised here (pydantic-ai's own format) alongside the deferred
    # `tool_call_id`, so that resuming after the human answers finishes the *same* run instead of
    # starting a new one. It is per-attempt state, not plan state - a retry starts a fresh run and
    # clears it - beside one record that outlives the attempts: the step workflow's hand-backs in
    # order (parks, and paces while it could not get its Machine), which a replay
    # re-issues (`execution.HANDBACKS`). Null until the first hand-back; a settled step
    # holds nothing.
    resume_state: Mapped[dict[str, Any] | None] = mapped_column(default=None)

    created_at: Mapped[dt.datetime] = created_at()

    @property
    def step_state(self) -> StepState:
        return StepState(self.state)

    def can(self, to: StepState) -> bool:
        return to in _ALLOWED[self.step_state]

    def transition(self, to: StepState) -> None:
        if not self.can(to):
            raise InvalidTransition(
                f"step {self.id} cannot go {self.state} → {to}",
                step_id=str(self.id),
                from_state=self.state,
                to_state=to.value,
            )
        self.state = to.value
        if to in (StepState.DONE, StepState.SKIPPED, StepState.ESCALATED):
            self.resume_state = None  # its workflow is over: nothing will resume or replay it

    def attempts_exhausted(self) -> bool:
        return self.attempt >= MAX_ATTEMPTS

    def waiting_out_a_pace(self) -> bool:
        """`ready`, but its workflow is alive and asleep on the pace it handed back last (a provider's
        backoff, or a Machine it could not get yet): in flight, not unstarted, so a replan that
        deleted it would leave the woken workflow without a step. A step that re-entered after its pace is `running` again."""
        return paced(self.state, self.resume_state)

    @classmethod
    async def for_task(cls, session: AsyncSession, task_id: uuid.UUID) -> list[PlanStep]:
        return list(await session.scalars(select(cls).where(cls.task_id == task_id).order_by(cls.ordinal)))

    @classmethod
    async def any_started(cls, session: AsyncSession, task_id: uuid.UUID) -> bool:
        """Has any step of the task taken an attempt?"""
        return bool(await session.scalar(select(exists().where(cls.task_id == task_id, cls.attempt > 0))))

    @classmethod
    async def runs_of_agent(
        cls,
        session: AsyncSession,
        agent: str,
        *,
        creator_actor_id: uuid.UUID,
        limit: int = 20,
    ) -> list[tuple[PlanStep, uuid.UUID, str, Decimal]]:
        """The recent steps this agent name ran, with their task's goal and metered spend -
        `prismoid bots logs`'s feed. Per-bot observability is a JOIN, not a table:
        `spec->>'agent'` names the role, `usage_events.step_id` carries the money.

        Scoped to the CREATOR'S OWN tasks: a bot may be shared, but a run belongs to whoever
        paid for it, and this view must not become a side-door into a project's task list.
        """
        from prismoid.db.models.task import Task
        from prismoid.db.models.usage import UsageEvent

        spend = (
            select(UsageEvent.step_id, func.sum(UsageEvent.cost_usd).label("usd"))
            .group_by(UsageEvent.step_id)
            .subquery()
        )
        rows = await session.execute(
            select(cls, Task.id, Task.goal, func.coalesce(spend.c.usd, 0))
            .join(Task, Task.id == cls.task_id)
            .outerjoin(spend, spend.c.step_id == cls.id)
            .where(
                cls.spec["agent"].as_string() == agent,
                Task.workspace_id == tenant.workspace_of(session),  # the bound workspace's runs only
                Task.creator_actor_id == creator_actor_id,
            )
            .order_by(cls.created_at.desc())
            .limit(limit)
        )
        return [(step, task_id, goal, Decimal(usd)) for step, task_id, goal, usd in rows.all()]

    @classmethod
    async def spend_of_agent(
        cls,
        session: AsyncSession,
        agent: str,
        *,
        creator_actor_id: uuid.UUID,
    ) -> tuple[Decimal, Decimal, int, dt.datetime | None]:
        """(total_usd, usd_30d, runs, last_run_at) for one agent name - the bot page's stats
        strip in one SELECT. Still a JOIN, never a table, and
        version-blind by name: `spec->>'agent'` cannot say which version served, so the strip
        says "as {name}", not "as {name}@3". Creator-scoped like `runs_of_agent` - spend
        belongs to whoever paid."""
        from prismoid.db.models.task import Task
        from prismoid.db.models.usage import UsageEvent

        cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=30)
        spend = (
            select(UsageEvent.step_id, func.sum(UsageEvent.cost_usd).label("usd"))
            .group_by(UsageEvent.step_id)
            .subquery()
        )
        row = (
            await session.execute(
                select(
                    func.coalesce(func.sum(spend.c.usd), 0),
                    func.coalesce(func.sum(spend.c.usd).filter(cls.created_at >= cutoff), 0),
                    func.count(cls.id),
                    func.max(cls.created_at),
                )
                .select_from(cls)
                .join(Task, Task.id == cls.task_id)
                .outerjoin(spend, spend.c.step_id == cls.id)
                .where(
                    cls.spec["agent"].as_string() == agent,
                    Task.workspace_id == tenant.workspace_of(session),  # the bound workspace's runs only
                    Task.creator_actor_id == creator_actor_id,
                )
            )
        ).one()
        total, month, runs, last = row
        return Decimal(total), Decimal(month), int(runs), last

    @classmethod
    async def delete_unfinished(cls, session: AsyncSession, task_id: uuid.UUID) -> None:
        """Drop every step that is not `done` - a replan re-drafts the unfinished work and leaves
        the verified work alone. CASCADE removes the dangling edges."""
        await session.execute(delete(cls).where(cls.task_id == task_id, cls.state != StepState.DONE.value))

    @classmethod
    async def any_pacing(cls, session: AsyncSession, task_id: uuid.UUID) -> bool:
        """Whether a step of the task is waiting out a pace (`waiting_out_a_pace`), as committed now:
        read fresh, like `states`, so the session's own copies never answer it."""
        rows = await session.execute(
            select(cls.state, cls.resume_state).where(
                cls.task_id == task_id, cls.state == StepState.READY.value, cls.resume_state.is_not(None)
            )
        )
        return any(paced(state, resume) for state, resume in rows.all())

    @classmethod
    async def states(cls, session: AsyncSession, task_id: uuid.UUID) -> dict[uuid.UUID, StepState]:
        """Each step's state as committed now - a column read, so the session's own copies of the
        rows (expire_on_commit=False) never answer it: the boundary re-plan's re-check."""
        rows = await session.execute(select(cls.id, cls.state).where(cls.task_id == task_id))
        return {step_id: StepState(state) for step_id, state in rows.all()}

    @classmethod
    async def frontier(cls, session: AsyncSession, task_id: uuid.UUID) -> list[PlanStep]:
        """Every step whose dependencies are satisfied - what the kernel dispatches next.

        A one-step plan's frontier is that step. A twelve-step DAG's frontier is whatever can run
        *now*, which is what makes the fan-out parallel by construction rather than by scheduling.
        """
        return list(
            await session.scalars(
                select(cls).where(cls.task_id == task_id, cls.state == StepState.READY.value)
            )
        )

    @classmethod
    async def running_attempts(cls, session: AsyncSession) -> list[tuple[uuid.UUID, int]]:
        """A platform read (the reconciler's), deliberately workspace-wide: every step attempt
        running now - a parked step is `running` too - on a task that is not finished. A `running`
        step of a delivered, failed or cancelled task is a park nobody will resume so its hold is dead."""
        from prismoid.db.models.task import Task
        from prismoid.schemas.plan import TaskStatus

        rows = await session.execute(
            select(cls.id, cls.attempt)
            .join(Task, Task.id == cls.task_id)
            .where(
                cls.state == StepState.RUNNING.value,
                Task.status.not_in(
                    [TaskStatus.DELIVERED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value]
                ),
            )
        )
        return [(r[0], r[1]) for r in rows.all()]


class PlanEdge(Base):
    """A typed dependency. The frontier is every step whose edges are all satisfied."""

    __tablename__ = "plan_edges"
    __table_args__ = (
        UniqueConstraint("from_step_id", "to_step_id", name="plan_edges_pair"),
        Index("ix_plan_edges_task_id", "task_id"),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    from_step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_steps.id", ondelete="CASCADE"), nullable=False
    )
    to_step_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("plan_steps.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="depends_on")

    @classmethod
    async def for_task(cls, session: AsyncSession, task_id: uuid.UUID) -> list[PlanEdge]:
        return list(await session.scalars(select(cls).where(cls.task_id == task_id)))


class PlanEdit(Base):
    """Append-only. Every replan, steer-merge, clarification and mid-run ask lands here,
    and none of them ever rewrites a previous row."""

    __tablename__ = "plan_edits"
    __table_args__ = (Index("ix_plan_edits_task_id", "task_id"),)

    id: Mapped[uuid.UUID] = uuid_pk()
    task_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False, default="")
    payload: Mapped[dict[str, Any]] = mapped_column(nullable=False, default=dict)
    # Who made this edit - a steer or replan names its human; worker-made edits stay NULL.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )
    at: Mapped[dt.datetime] = created_at()

    @classmethod
    async def of_kind(cls, session: AsyncSession, task_id: uuid.UUID, kind: str) -> list[PlanEdit]:
        """The task's edits of one kind, oldest first - what the steer fences read: a
        `hatch` or `lead` row names the step that planned the turn, fence 2's `replan` row the sinks it re-drafted."""
        return list(
            await session.scalars(
                select(cls).where(cls.task_id == task_id, cls.kind == kind).order_by(cls.at, cls.id)
            )
        )

    @classmethod
    async def latest_expand(cls, session: AsyncSession, task_id: uuid.UUID) -> PlanEdit | None:
        """The newest EXPAND edit - its payload is the verbatim outline, which the
        figure pre-generation job re-reads as its work order."""
        return await session.scalar(
            select(cls).where(cls.task_id == task_id, cls.kind == "expand").order_by(cls.at.desc()).limit(1)
        )
