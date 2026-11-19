# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Shared plumbing for the Machine's operations: the database clock, the per-workspace cap lock, the
durable `machine.*` events, binding a Machine's frame, and what a driven Machine's refusals and
transitions say about its control lease."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import logfire
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.frames.driver import FrameDriver
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.machine import Machine
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.errors import Conflict, PrismoidError
from prismoid.operations.attribution import actor_ref
from prismoid.schemas.events import ActorRef, MachineRebuilt
from prismoid.schemas.ids import ProjectId, TaskId
from prismoid.schemas.machines import RebuildReason
from prismoid.schemas.screen import ControlEnd, Lease, end_reason, is_live


async def db_now(session: AsyncSession) -> dt.datetime:
    """Postgres `now()` - the time authority (CLAUDE.md), never the worker's clock."""
    return await Machine.db_now(session)


async def bound(frame: FrameDriver) -> bool:
    """Attach to a Machine's frame - a stopped one is started, its files intact; False when it is
    gone (a driver's `bind` raises RuntimeError for a missing frame). Any other failure propagates:
    a provider that did not answer is a failed attempt to retry, never a lost frame."""
    try:
        await frame.bind()
    except RuntimeError:
        return False
    return True


async def workspace_lock(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """Serialize the warm-Machine cap per workspace until this transaction ends."""
    await Machine.lock_workspace(session, workspace_id)


# How long a step's hand-back (`release`, `widen`, `release_parked`) waits on a Machine's row.
# They run on their own sessions while the step's may hold the task's row, so a
# wait here can close a cycle across connections that Postgres cannot see: past this it gives up,
# logs, and leaves the hold to the reconciler - never a hang.
LOCK_WAIT_S = 5.0


def required[T](value: T | None, what: str) -> T:
    """`value`, or a `PrismoidError` saying `what` invariant broke - an `assert` vanishes under `python -O`."""
    if value is None:
        raise PrismoidError(what)
    return value


class RowBusy(Exception):
    """The Machine's row stayed locked past `LOCK_WAIT_S`."""


async def lock_briefly[T](session: AsyncSession, take: Callable[[], Awaitable[T]]) -> T:
    """`take` a row lock waiting at most `LOCK_WAIT_S` for this transaction; `RowBusy` past it."""
    await Machine.lock_timeout(session, LOCK_WAIT_S)
    try:
        return await take()
    except DBAPIError as failed:
        if getattr(failed.orig, "sqlstate", None) == "55P03":  # lock_not_available
            raise RowBusy(str(failed.orig)) from failed
        raise


async def locked_machine(session: AsyncSession, machine_id: uuid.UUID) -> Machine | None:
    """The row, locked, from a job's `row_scope` - or None when it is gone. A deleted row leaves
    the scope unbound, and a fenced read on an unbound session fails closed (TenantUnbound), so ask
    first."""
    if tenant.bound_workspace(session) is None:
        return None
    return await Machine.locked(session, machine_id)


async def actor_of(session: AsyncSession, actor_id: uuid.UUID | None) -> ActorRef | None:
    if actor_id is None:
        return None
    actor = await session.get(Actor, actor_id)
    return actor_ref(actor) if actor is not None else None


async def record(
    session: AsyncSession,
    task_id: uuid.UUID,
    event_type: type[Any],
    *,
    actor: ActorRef | None = None,
    **fields: Any,
) -> None:
    """Append one durable `machine.*` event to the task's record. A deleted task records nothing."""
    task = await session.get(Task, task_id)
    if task is None:
        return
    seq = await task.next_seq(session)
    await TaskEvent.append(
        session,
        event_type(
            task_id=TaskId(task.id),
            project_id=ProjectId(task.project_id) if task.project_id else None,
            at=await db_now(session),
            seq=seq,
            actor=actor,
            **fields,
        ),
    )


async def record_after(
    task_id: uuid.UUID,
    event_type: type[Any],
    *,
    actor: ActorRef | None = None,
    **fields: Any,
) -> None:
    """One durable `machine.*` event, said AFTER the commit that caused it: in a transaction of its
    own, bound to the task's workspace, outside any Machine row's lock (`meter.settle`'s rule: a record
    takes the task's row, which a step may hold while it waits on the Machine's). Best-effort: a
    deleted task records nothing, and a failure is logged, never raised."""
    try:
        async with row_scope(Task, task_id) as session:
            if tenant.bound_workspace(session) is None:  # the task is gone
                return
            await record(session, task_id, event_type, actor=actor, **fields)
            await session.commit()
    except Exception:
        logfire.exception(
            "task {task}: a {event} event could not be recorded", task=str(task_id), event=event_type.__name__
        )


async def record_lost(task_id: uuid.UUID) -> None:
    """`machine.rebuilt{lost}`: a frame died with the Machine's unsaved work - what `/work` held
    since its last checkpoint - found by the reconciler, a rewake or a sleep.
    Recorded after the Machine row's commit (`record_after`). The caller leaves `gone_reason`
    unset, so the next wake does not say it twice. Never raises."""
    await record_after(task_id, MachineRebuilt, reason=str(RebuildReason.LOST))


# ── the control lease, where the lifecycle meets it ─────────────────────────────


async def driving_text(session: AsyncSession, actor_id: uuid.UUID) -> str:
    """The 409 every refusal on a driven Machine answers: `{name} is driving the browser.`"""
    holder = await actor_of(session, actor_id)
    return f"{holder.name if holder is not None else 'Someone'} is driving the browser."


async def refuse_while_driven(session: AsyncSession, row: Machine, now: dt.datetime) -> None:
    """Sleep now and Reset answer 409 while someone drives the browser: a Machine being driven
    never sleeps under its driver. A lease whose clock has run out does not refuse - the transition
    clears it and `lease_cleared` says its ending."""
    lease = row.lease()
    if lease is not None and is_live(lease, now):
        raise Conflict(await driving_text(session, lease.actor_id))


def cleared_reason(lease: Lease, at: dt.datetime) -> ControlEnd:
    """Why a lease a transition cleared ended: its own clock's reason when it had already run out
    (the lease-end job had not got to it yet), else `lost` - the frame went from under it."""
    return ControlEnd.LOST if is_live(lease, at) else end_reason(lease)


async def lease_cleared(task_id: uuid.UUID, machine_id: uuid.UUID, lease: Lease, at: dt.datetime) -> None:
    """After the commit of a transition that ended the Machine's control lease in its own
    transaction (`Machine.move` to `asleep` or `gone`, or a step that found its frame dead): its
    ending is said like every other, through `screen.after_end` - the record, `screen.state` and
    the CONTROL gate - by `cleared_reason`. Never raises."""
    from prismoid.operations.machines import screen  # screen imports this module

    try:
        await screen.after_end(screen.Ended(lease, cleared_reason(lease, at), None, at, task_id, machine_id))
    except Exception:
        logfire.exception("machine {id}: a cleared lease's ending could not be said", id=str(machine_id))
