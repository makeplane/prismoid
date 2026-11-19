# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""machines - a task's remembered computer: which frame is its
current incarnation, who holds it, what it may reach, and where its checkpoint lives.

The sandbox itself is a frame (`ai/frames`); this row is the policy's truth. A tenant root:
`workspace_id NOT NULL`, fenced on every read, and pinned to its task's workspace by the composite
`(task_id, workspace_id)` FK, so a Machine can never belong to another workspace's task.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Collection
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    and_,
    func,
    or_,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from prismoid.db.base import Base, created_at, uuid_pk, vocabulary
from prismoid.db.models import tenant
from prismoid.errors import PrismoidError
from prismoid.schemas.machines import WARM_STATES, MachineProfile, MachineState, holder_step
from prismoid.schemas.screen import Lease, is_live


class Machine(Base):
    __tablename__ = "machines"
    __table_args__ = (
        vocabulary("state", MachineState),
        vocabulary("profile", MachineProfile),
        ForeignKeyConstraint(
            ["task_id", "workspace_id"],
            ["tasks.id", "tasks.workspace_id"],
            name="fk_machines_task_workspace",
            ondelete="CASCADE",
        ),
        Index("ix_machines_workspace_state", "workspace_id", "state"),
        Index("ix_machines_state_since", "state", "state_since"),
        # The sweep's lease arm reads only rows a control lease is on .
        Index(
            "ix_machines_control_until",
            "control_until",
            postgresql_where=text("control_actor_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = uuid_pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="RESTRICT"), index=True, nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(nullable=False, unique=True)
    driver: Mapped[str] = mapped_column(String(20), nullable=False)
    profile: Mapped[str] = mapped_column(String(20), nullable=False, default="base")
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    state_since: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    frame_ref: Mapped[str | None] = mapped_column(String(200), default=None)
    epoch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    held_by: Mapped[str | None] = mapped_column(String(120), default=None)
    held_since: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    egress_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    egress_hosts: Mapped[list[str]] = mapped_column(ARRAY(String(253)), nullable=False, default=list)
    # The list the last step ran under (its boundary's, and any live widen), kept when its hold
    # ends: what a person driving the browser of an idle Machine may reach
    # (`stepless_egress`). `egress_hosts` is what the frame has now.
    step_hosts: Mapped[list[str]] = mapped_column(
        ARRAY(String(253)), nullable=False, default=list, server_default="{}"
    )
    checkpoint_object: Mapped[str | None] = mapped_column(String(300), default=None)
    checkpoint_sha256: Mapped[str | None] = mapped_column(String(64), default=None)
    checkpoint_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    checkpoint_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    browser_object: Mapped[str | None] = mapped_column(String(300), default=None)
    browser_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    browser_cipher_key_id: Mapped[str | None] = mapped_column(String(40), default=None)
    last_active_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    metered_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    # Why a `gone` Machine is gone, so its next wake can say so (`machine.rebuilt`): `expired`
    # (retention), `oversize` (over the cap at sleep) or `lost` (the reconciler found no frame and
    # no checkpoint). None after a reset (the timeline already says who reset it) or for a new row.
    gone_reason: Mapped[str | None] = mapped_column(String(20), default=None)
    # The computer a device Machine runs on, as its helper names it,
    # written with the frame it names. None on every cloud driver.
    device_name: Mapped[str | None] = mapped_column(String(80), default=None)
    # The control lease: who drives the browser, from which
    # connection, privately or not, and until when. The six are set and cleared together
    # (`set_lease`, `clear_lease`), only by compare-and-set under this row's lock, and only from
    # `operations/machines`. Live while `control_until > now()`; a private lease also has a hard
    # cap. Always a human: only a Project editor can claim (operations/machines/screen.py).
    control_actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("actors.id", ondelete="SET NULL"), default=None
    )
    control_conn: Mapped[str | None] = mapped_column(String(64), default=None)
    control_private: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="false")
    control_since: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    control_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    control_cap_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    created_at: Mapped[dt.datetime] = created_at()

    # ── the clock and the cap lock (queries live here: the audit's "no select() outside
    #    db/models/") ─────────────────────────────────────────────────────────────────────

    @staticmethod
    async def db_now(session: AsyncSession) -> dt.datetime:
        """Postgres `now()` - the time authority (CLAUDE.md), never the worker's clock."""
        value = await session.scalar(select(func.now()))
        if value is None:
            raise PrismoidError("the database answered no clock")
        return value

    @staticmethod
    async def lock_workspace(session: AsyncSession, workspace_id: uuid.UUID) -> None:
        """Serialize the warm-Machine cap per workspace until this transaction ends."""
        await session.execute(select(func.pg_advisory_xact_lock(func.hashtext(f"machines:{workspace_id}"))))

    @staticmethod
    async def lock_timeout(session: AsyncSession, seconds: float) -> None:
        """`SET LOCAL lock_timeout` for this transaction: a lock wait longer than this fails the
        statement (SQLSTATE 55P03) instead of waiting forever."""
        await session.execute(select(func.set_config("lock_timeout", f"{int(seconds * 1000)}ms", True)))

    # ── reads (every one fenced to the bound workspace) ─────────────────────────────────────────

    @classmethod
    async def for_task(cls, session: AsyncSession, task_id: uuid.UUID) -> Machine | None:
        return await session.scalar(
            select(cls).where(cls.task_id == task_id, cls.workspace_id == tenant.workspace_of(session))
        )

    @classmethod
    async def locked(cls, session: AsyncSession, machine_id: uuid.UUID) -> Machine | None:
        """The row, `FOR UPDATE`, re-read from the database (never a stale identity-map copy)."""
        return await session.scalar(
            select(cls)
            .where(cls.id == machine_id, cls.workspace_id == tenant.workspace_of(session))
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    @classmethod
    async def current(cls, session: AsyncSession, machine_id: uuid.UUID) -> Machine | None:
        """The row as it is now, WITHOUT a lock (re-read, never a stale identity-map copy) - for a
        decision that only needs to be current enough, taken while another lock is held (the cap's
        check on a pending victim, under the workspace's lock: a row lock there would invert the order
        every claim takes, row then workspace)."""
        return await session.scalar(
            select(cls)
            .where(cls.id == machine_id, cls.workspace_id == tenant.workspace_of(session))
            .execution_options(populate_existing=True)
        )

    @classmethod
    async def locked_for_task(cls, session: AsyncSession, task_id: uuid.UUID) -> Machine | None:
        return await session.scalar(
            select(cls)
            .where(cls.task_id == task_id, cls.workspace_id == tenant.workspace_of(session))
            .with_for_update()
            .execution_options(populate_existing=True)
        )

    @classmethod
    async def with_usage(
        cls, session: AsyncSession, task_id: uuid.UUID
    ) -> tuple[Machine, int, Decimal, str | None, dt.datetime] | None:
        """The task's Machine; the totals of EVERY `family == "machine"` usage row of the task -
        the Machine's segments, and its seats', qa, figures and verify frames; the control lease's
        holder's name (None when nobody holds one); and the database clock to judge the lease by -
        in ONE query, so the task page gains at most one."""
        from prismoid.db.models.actor import Actor
        from prismoid.db.models.usage import UsageEvent

        seconds = func.coalesce(func.sum(UsageEvent.units["second"].as_integer()), 0)
        usd = func.coalesce(func.sum(UsageEvent.cost_usd), 0)
        row = (
            await session.execute(
                select(cls, seconds, usd, Actor.display_name, func.now())
                .outerjoin(
                    UsageEvent, and_(UsageEvent.task_id == cls.task_id, UsageEvent.family == "machine")
                )
                .outerjoin(Actor, Actor.id == cls.control_actor_id)
                .where(cls.task_id == task_id, cls.workspace_id == tenant.workspace_of(session))
                .group_by(cls.id, Actor.display_name)
            )
        ).first()
        return (row[0], int(row[1]), Decimal(row[2]), row[3], row[4]) if row is not None else None

    @classmethod
    async def warm_count(cls, session: AsyncSession) -> int:
        return int(
            await session.scalar(
                select(func.count()).where(
                    cls.workspace_id == tenant.workspace_of(session),
                    cls.state.in_([str(s) for s in WARM_STATES]),
                )
            )
            or 0
        )

    @classmethod
    async def lru_idle(cls, session: AsyncSession) -> Machine | None:
        """The workspace's least-recently-active idle Machine, locked, skipping one another worker
        already has - the cap's eviction candidate. Re-read from the
        database, like `locked`: a retried cap check must never act on a stale epoch. A Machine
        with a control lease on its row is never the victim: a live one is being
        driven, and a run-out one is ended by the sweep first - its ending restarts the idle clock."""
        return await session.scalar(
            select(cls)
            .where(
                cls.workspace_id == tenant.workspace_of(session),
                cls.state == MachineState.IDLE,
                cls.control_actor_id.is_(None),
            )
            .order_by(cls.last_active_at)
            .limit(1)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )

    # ── platform reads (the sweep's, the reconciler's, the orphan arm's) ──────────────────
    #    Deliberately workspace-wide, on a `platform=True` session: these jobs keep every
    #    workspace's Machines, and each only dispatches or compare-and-sets one row at a time, under
    #    that row's own workspace (`row_scope`).

    @classmethod
    async def sweepable(cls, session: AsyncSession) -> list[Machine]:
        """A platform read (the sweep's): rows an arm could act on - warm, or asleep/gone with
        objects; and any row a control lease is on, whatever its state
        (a running step's Machine whose driver went quiet), and every running browser Machine
        (the kill switch stops its Chromium)."""
        return list(
            (
                await session.scalars(
                    select(cls).where(
                        or_(
                            cls.state.in_(
                                [
                                    MachineState.IDLE,
                                    MachineState.PARKED,
                                    MachineState.WAKING,
                                    MachineState.SLEEPING,
                                ]
                            ),
                            and_(
                                cls.state.in_([MachineState.ASLEEP, MachineState.GONE]),
                                or_(cls.checkpoint_object.is_not(None), cls.browser_object.is_not(None)),
                            ),
                            cls.control_actor_id.is_not(None),
                            and_(cls.state == MachineState.RUNNING, cls.profile == MachineProfile.BROWSER),
                        )
                    )
                )
            ).all()
        )

    @classmethod
    async def for_reconcile(cls, session: AsyncSession, ids: Collection[uuid.UUID]) -> list[Machine]:
        """A platform read (the reconciler's): every warm row, every row that holds a hold (a dead
        one is the reconciler's to let go, whatever the state), plus any row a listed frame names."""
        return list(
            (
                await session.scalars(
                    select(cls).where(
                        or_(
                            cls.state.in_([str(s) for s in WARM_STATES]),
                            cls.held_by.is_not(None),
                            cls.id.in_(list(ids)),
                        )
                    )
                )
            ).all()
        )

    @classmethod
    async def frame_drivers(cls, session: AsyncSession) -> set[str]:
        """A platform read (the reconciler's): every driver a row names a frame on (`frame_ref` set)
        - the providers it must list, whatever this worker makes. A row
        asleep on its archive names no frame, so a retired driver's sleepers never make a pass ask
        a provider that may be gone. One unindexed DISTINCT per pass: fine at today's row counts."""
        drivers = await session.scalars(select(cls.driver).where(cls.frame_ref.is_not(None)).distinct())
        return set(drivers.all())

    @classmethod
    async def object_index(cls, session: AsyncSession) -> tuple[set[uuid.UUID], set[str]]:
        """A platform read (the orphan arm's): every Machine id, and every object key a row holds."""
        rows = (await session.execute(select(cls.id, cls.checkpoint_object, cls.browser_object))).all()
        return {r[0] for r in rows}, {k for r in rows for k in (r[1], r[2]) if k}

    @classmethod
    async def references(cls, session: AsyncSession, key: str) -> bool:
        """A platform read (the orphan arm's, just before a delete): does any row hold this key?"""
        found = await session.scalar(
            select(cls.id).where(or_(cls.checkpoint_object == key, cls.browser_object == key)).limit(1)
        )
        return found is not None

    # ── writes ───────────────────────────────────────────────────────────────────────────

    def move(self, state: MachineState, now: dt.datetime) -> Lease | None:
        """The state transition. Moving to `asleep` or `gone` also ends the control lease, in the
        same transaction (a Machine with no frame has no browser to drive; `end_lease`
        restarts the idle clock), and returns it, so the caller says its ending after the commit
        (`common.lease_cleared`). None otherwise."""
        self.state = state
        self.state_since = now
        if state in (MachineState.ASLEEP, MachineState.GONE):
            return self.end_lease(now)
        return None

    def lease(self) -> Lease | None:
        """The control lease as a value, live or run out (`schemas.screen.is_live` decides, against
        Postgres `now()`); None when nobody holds one - including a holder SET NULL removed."""
        if self.control_actor_id is None or self.control_since is None or self.control_until is None:
            return None
        return Lease(
            actor_id=self.control_actor_id,
            conn=self.control_conn or "",
            private=self.control_private,
            since=self.control_since,
            until=self.control_until,
            cap_at=self.control_cap_at,
        )

    def set_lease(self, lease: Lease) -> None:
        self.control_actor_id = lease.actor_id
        self.control_conn = lease.conn
        self.control_private = lease.private
        self.control_since = lease.since
        self.control_until = lease.until
        self.control_cap_at = lease.cap_at

    def clear_lease(self) -> Lease | None:
        """Empty all six columns; returns the lease that was there (None if none was)."""
        ended = self.lease()
        self.control_actor_id = None
        self.control_conn = None
        self.control_private = False
        self.control_since = None
        self.control_until = None
        self.control_cap_at = None
        return ended

    def end_lease(self, now: dt.datetime) -> Lease | None:
        """A lease's ending, on the row: the six columns cleared and - when there was one - the idle
        clock restarted at `now` (every ending moves `last_active_at`, so a Machine
        someone just drove never sleeps the moment they let go). Returns the lease that ended."""
        ended = self.clear_lease()
        if ended is not None:
            self.last_active_at = now
        return ended

    def stepless_egress(self, now: dt.datetime) -> int | None:
        """What a Machine no step holds may reach: nothing - an idle Machine's background
        processes reach nothing - except while a person drives its browser, who gets the
        list its last step ran under (`step_hosts`): a driver goes where the agent could, and a host
        outside it is still refused. Moves the row's egress one version on when that changes and
        returns the version, for the caller to push to the frame after its commit; None when nothing
        moved - a step or a seed holds the Machine (the list is the step's), it is not idle, it has
        no frame, or it already has that list."""
        if self.held_by is not None or self.state != MachineState.IDLE or not self.frame_ref:
            return None
        lease = self.lease()
        hosts = sorted(self.step_hosts) if lease is not None and is_live(lease, now) else []
        if hosts == sorted(self.egress_hosts):
            return None
        self.egress_version += 1
        self.egress_hosts = hosts
        return self.egress_version

    def let_go(self, now: dt.datetime) -> int | None:
        """A hold ends with the Machine where its hand-back put it (idle, or asleep under a park): a
        step's hold leaves the list it ran under for the next driver (`step_hosts`; a seed's never
        widened anything), and the frame gets what a Machine no step holds may reach. Returns
        `stepless_egress`'s version for the caller to push after its commit."""
        if holder_step(self.held_by) is not None:
            self.step_hosts = list(self.egress_hosts)
        self.held_by, self.held_since = None, None
        return self.stepless_egress(now)

    @property
    def has_checkpoint(self) -> bool:
        return self.checkpoint_object is not None

    @property
    def has_objects(self) -> bool:
        return self.checkpoint_object is not None or self.browser_object is not None
