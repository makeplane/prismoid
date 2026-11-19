# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The control lease: who drives a Machine's browser, held in six
columns on its row.

Every write here is ONE short transaction on the Machine's row, and on that row only: `FOR UPDATE`
(waited on at most `LOCK_WAIT_S`), a compare-and-set, a commit. None takes the task's row - the
lock order is the Machine, then the task, and a record takes the task's - so what follows a change
runs after the commit, each part in a transaction of its own (`common.record_lost`'s precedent):
the durable `machine.control_*` record, the `screen.state` frame and the CONTROL gate's
resolution (the ending answers the agent's gate). Times are Postgres `now()`
(`db_now`); the lease's arithmetic is `schemas.screen`'s. Every ending restarts the Machine's idle
clock (`Machine.end_lease`). This module reads a Machine's row and never calls a lifecycle verb on
its frame (the Machine's lifecycle belongs to `machines`).

Who may do what: an editor of the task's Project (on a personal task, its creator)
claims; the holder renews, extends and hands back; the Project owner (on a personal task, its
creator) ends anyone's; a viewer watches (403); a stranger is told nothing (404).

A lease that starts or ends on an idle Machine moves its egress in the same transaction
(`Machine.stepless_egress`: the driver reaches the last step's list, and nobody's control reaches
nothing). The frame is told after the commit by the worker: a claim or an ending in the API
enqueues `machine_egress` (`tell_egress`); the lease-end job's own ending runs inside a DBOS step,
which cannot start a job, so it hands the version back for the job's next step."""

from __future__ import annotations

import contextlib
import datetime as dt
import uuid
from dataclasses import dataclass, replace
from typing import Final

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.frames import screen as frame_screen
from prismoid.ai.frames.screen import ScreenStream
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.machine import Machine
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.errors import Conflict, Forbidden, NoMachine
from prismoid.operations import live, steering, tickets
from prismoid.operations.attribution import actor_ref
from prismoid.operations.dispatch import get_dispatcher
from prismoid.operations.machines.common import (
    RowBusy,
    actor_of,
    db_now,
    driving_text,
    lock_briefly,
    locked_machine,
    record_after,
)
from prismoid.schemas.events import ActorRef, MachineControlClaimed, MachineControlEnded, ScreenState
from prismoid.schemas.ids import ActorId, TaskId
from prismoid.schemas.machines import FRAME_STATES, MachineProfile, MachineState, held_by_seed
from prismoid.schemas.screen import (
    NO_SCREEN_TEXT,
    ControlEnd,
    ControlState,
    Lease,
    LeaseLimits,
    LeaseView,
    claim_window,
    end_reason,
    extend_window,
    grace_until,
    held_seconds,
    is_live,
    reclaim_until,
    renew_until,
    reopen_until,
)

ASLEEP: Final = "The Machine is asleep."
NO_BROWSER: Final = "The Machine has no browser."
# The one "off" refusal: the claim's and Renew's here, the screen socket's.
OFF: Final = "Computer use is off."
NOT_DRIVING: Final = "Take control first."
BUSY: Final = "The Machine is busy; try again in a moment."
EDITORS_ONLY: Final = "this action needs the editor role"
NOT_YOURS: Final = "only the person driving or the project owner can end control of the browser"


def has_screen(profile: str, driver: str) -> bool:
    """THE rule for whether a Machine has a screen at all: a browser Machine on
    a driver whose door is open in this deployment (`ai/frames/screen.DOORS`) - Docker's hijacked
    exec (dev); sandbar's display route behind a display ticket, open only once the operator says
    sandbar's side is configured (`computer_use.sandbar_screen`); any other door where its own
    `available` says so. A claim refuses without one, the relay's upstream says `no_browser`, and a step on it has no lease to
    yield to and nobody to hand the browser to (`Acquired.screen`)."""
    return profile == MachineProfile.BROWSER and frame_screen.has_door(driver)


def lease_limits() -> LeaseLimits:
    cfg = settings.computer_use
    return LeaseLimits(
        silence_s=cfg.control_silence_s,
        private_cap_s=cfg.control_private_cap_s,
        grace_s=cfg.control_grace_s,
        renew_every_s=cfg.control_renew_every_s,
        settle_ms=cfg.mask_settle_ms,
    )


@dataclass(frozen=True, slots=True)
class Ended:
    """One lease that ended and was committed: what `after_end` says about it. `ended_by` is the
    owner who ended someone else's (`ENDED`), else None; `egress` the version its ending moved an
    idle Machine's egress to (`Machine.stepless_egress`), for whoever ended it to tell the frame."""

    lease: Lease
    reason: ControlEnd
    ended_by: uuid.UUID | None
    at: dt.datetime
    task_id: uuid.UUID
    machine_id: uuid.UUID
    egress: int | None = None


def view_of_lease(lease: Lease, holder_name: str) -> LeaseView:
    """The one LeaseView constructor (the page's, the routes', `meter.summary`'s)."""
    return LeaseView(
        holder_id=lease.actor_id,
        holder_name=holder_name,
        private=lease.private,
        since=lease.since,
        until=lease.until,
        cap_at=lease.cap_at,
    )


async def _lock(session: AsyncSession, task_id: uuid.UUID) -> Machine | None:
    """The task's Machine row, locked, waited on at most `LOCK_WAIT_S` (a 409 past it)."""
    try:
        return await lock_briefly(session, lambda: Machine.locked_for_task(session, task_id))
    except RowBusy:
        raise Conflict(BUSY) from None


async def _editor(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> Task:
    """The task, for an editor of it while computer use is on: 404 a stranger, 403 a viewer, 409
    while computer use is off - the claim's one "off" check."""
    task = await Task.get_for(session, task_id, actor)
    if not await steering.may_edit(session, task, actor):
        raise Forbidden(EDITORS_ONLY)
    if not settings.computer_use.enabled:
        raise Conflict(OFF)
    return task


# ── the people's (the routes') ────────────────────────────────────────────────────────────


async def claim(
    session: AsyncSession, actor: Actor, task_id: uuid.UUID, *, conn: str, private: bool
) -> LeaseView:
    """Take control of the browser: an editor, on a warm Machine with a screen (`has_screen`; one
    without is the 409 that says so, never a lease nobody can see) that no fork's seed holds
    (a seed has the frame frozen for its archive: `BUSY`, try again in a moment). Wins against an
    empty or run-out lease (a run-out one is ended first, by its clock); against someone else's live
    lease it is the 409 that names them. The holder's re-claim - a reload, or privacy switched -
    binds `conn`. Same privacy keeps `since` and restores `until` (`reclaim_until`: the cap for a
    private lease, never past it; the cap never moves); a plain rebind records nothing. Privacy
    switched is a claim of the new kind, recorded as one: switching it ON is a fresh private claim
    from now - `since` included, so the relay's settle masks every viewer before the holder's input
    lands (`may_drive` counts from `since`); switching it OFF restarts the silence clock and keeps
    `since`."""
    task = await _editor(session, actor, task_id)
    who = actor_ref(actor)  # a value, before the commit
    row = await _lock(session, task.id)
    if row is None:
        raise NoMachine("this task has no Machine yet")
    if not has_screen(row.profile, row.driver):
        raise Conflict(NO_BROWSER if row.profile != MachineProfile.BROWSER else NO_SCREEN_TEXT)
    if row.state not in FRAME_STATES:
        raise Conflict(ASLEEP)
    if held_by_seed(row.held_by):
        raise Conflict(BUSY)
    now = await db_now(session)
    limits = lease_limits()
    held = row.lease()
    live_held = held if held is not None and is_live(held, now) else None
    if live_held is not None and live_held.actor_id != actor.id:
        raise Conflict(await driving_text(session, live_held.actor_id))
    stale = held if live_held is None else None
    if live_held is not None and private == live_held.private:
        lease = replace(live_held, conn=conn, until=reclaim_until(live_held, now, limits))
    else:
        until, cap_at = claim_window(private=private, now=now, limits=limits)
        since = live_held.since if live_held is not None and not private else now
        lease = Lease(actor_id=actor.id, conn=conn, private=private, since=since, until=until, cap_at=cap_at)
    if stale is not None:
        row.end_lease(now)  # ended first, by its clock: the idle clock restarts
    row.set_lease(lease)
    egress = row.stepless_egress(now)
    machine_id = row.id
    await session.commit()
    await tell_egress(machine_id, egress)
    if stale is not None:
        await after_end(Ended(stale, end_reason(stale), None, now, task.id, machine_id))
    if live_held is None or live_held.private != private:
        await record_after(task.id, MachineControlClaimed, actor=who, private=private)
    view = view_of_lease(lease, who.name)
    await publish_state(task.id, view)
    return view


async def extend(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> LeaseView:
    """Renew, pressed - the holder's own. A private lease gets a fresh window: `until = cap_at =
    now + control_private_cap_s`, the cap moving with it. A non-private lease restarts its silence
    clock (a button, so unthrottled). Someone else's live lease is the 409 that names them; no live
    lease is `Take control first.`"""
    task = await _editor(session, actor, task_id)
    who = actor_ref(actor)
    row = await _lock(session, task.id)
    held = row.lease() if row is not None else None
    now = await db_now(session)
    if row is None or held is None or not is_live(held, now):
        raise Conflict(NOT_DRIVING)
    if held.actor_id != actor.id:
        raise Conflict(await driving_text(session, held.actor_id))
    limits = lease_limits()
    until, cap_at = (
        extend_window(now, limits) if held.private else claim_window(private=False, now=now, limits=limits)
    )
    lease = replace(held, until=until, cap_at=cap_at)
    row.set_lease(lease)
    await session.commit()
    view = view_of_lease(lease, who.name)
    await publish_state(task.id, view)
    return view


async def release(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> None:
    """Hand back (the holder: `released`), or end someone else's (the Project owner; on a personal
    task its creator: `ended`, and the record names both); anyone else is refused (403). A lease
    whose clock already ran out ends now, by that clock (`end_reason`: expired, grace or cap) -
    whoever asks, since nothing live is taken from anyone - so the page never shows it driving
    until the lease-end job gets to it. No lease - or no Machine - is a no-op, so a repeat is
    harmless."""
    task = await Task.get_for(session, task_id, actor)
    row = await _lock(session, task.id)
    held = row.lease() if row is not None else None
    if row is None or held is None:
        return
    now = await db_now(session)
    if not is_live(held, now):
        reason, by = end_reason(held), None
    elif held.actor_id == actor.id:
        reason, by = ControlEnd.RELEASED, None
    elif await steering.may_end_control(session, task, actor):
        reason, by = ControlEnd.ENDED, actor.id
    else:
        raise Forbidden(NOT_YOURS)
    row.end_lease(now)
    egress = row.stepless_egress(now)
    machine_id = row.id
    await session.commit()
    await tell_egress(machine_id, egress)
    await after_end(Ended(held, reason, by, now, task.id, machine_id, egress))


# ── the relay's and the jobs' (their own transactions) ────────────────────────────────────


async def renew(machine_id: uuid.UUID, actor_id: uuid.UUID, conn: str) -> None:
    """On the holder's input (the relay): a non-private lease's silence clock restarts, at most
    every `control_renew_every_s` (`renew_until` throttles), for the holder's own live connection
    only. No frame: a renewal changes nothing anyone else shows. A busy row skips this renewal; the
    next input renews."""
    async with row_scope(Machine, machine_id) as session:
        try:
            row = await lock_briefly(session, lambda: locked_machine(session, machine_id))
        except RowBusy:
            return
        held = row.lease() if row is not None else None
        if row is None or held is None or (held.actor_id, held.conn) != (actor_id, conn):
            return
        now = await db_now(session)
        if not is_live(held, now):
            return
        until = renew_until(held, now, lease_limits())
        if until is None:
            return
        row.control_until = until
        await session.commit()


async def socket_closed(machine_id: uuid.UUID, actor_id: uuid.UUID, conn: str) -> None:
    """The holder's socket closed (the relay): the lease now ends within `control_grace_s`, never
    later than it would have (`grace_until`: a private lease's grace never passes its cap). A
    re-claim inside the window keeps it, and so does the same connection coming back
    (`socket_reopened`). `screen.state` says the grace has started."""
    async with row_scope(Machine, machine_id) as session:
        try:
            row = await lock_briefly(session, lambda: locked_machine(session, machine_id))
        except RowBusy:
            return  # the lease ends by its own clock
        held = row.lease() if row is not None else None
        if row is None or held is None or (held.actor_id, held.conn) != (actor_id, conn):
            return
        now = await db_now(session)
        if not is_live(held, now):
            return
        until = grace_until(held, now, lease_limits())
        if until == held.until:
            return
        lease = replace(held, until=until)
        row.set_lease(lease)
        holder = await actor_of(session, actor_id)
        task_id = row.task_id
        await session.commit()
    await publish_state(task_id, view_of_lease(lease, holder.name if holder is not None else ""))


async def socket_reopened(machine_id: uuid.UUID, actor_id: uuid.UUID, conn: str) -> None:
    """The holder's own connection came back (the relay): a socket of the lease's
    bound connection attached again - a 4408 drop and its reconnect, or a pane reopened on the
    connection it kept - so the grace `socket_closed` started is lifted (`reopen_until`: a private
    lease back to its cap, a non-private one its minute). Only that connection: a different one is
    a re-claim's to rebind ("Take control here"). A lease the grace never shortened is left alone,
    with no frame. `screen.state` says the new `until`."""
    async with row_scope(Machine, machine_id) as session:
        try:
            row = await lock_briefly(session, lambda: locked_machine(session, machine_id))
        except RowBusy:
            return  # the grace runs on; the holder's Renew or a re-claim keeps the lease
        held = row.lease() if row is not None else None
        if row is None or held is None or (held.actor_id, held.conn) != (actor_id, conn):
            return
        now = await db_now(session)
        until = reopen_until(held, now, lease_limits())
        if until is None:
            return
        lease = replace(held, until=until)
        row.set_lease(lease)
        holder = await actor_of(session, actor_id)
        task_id = row.task_id
        await session.commit()
    await publish_state(task_id, view_of_lease(lease, holder.name if holder is not None else ""))


async def end_lease(
    machine_id: uuid.UUID,
    *,
    since: dt.datetime,
    reason: ControlEnd | None,
    ended_by: uuid.UUID | None = None,
) -> Ended | None:
    """End the lease claimed at `since`: the lease-end job's, and the kill switch's
    `DISABLED`. Compare-and-set on `since` under the row's lock - a lease already ended, or claimed
    afresh, is not this one. With `reason` None the lease must have run out (`until <= now`) and
    ends by its own clock (`end_reason`: expired, grace or cap); a renewal or a re-claim since the
    sweep looked keeps it. The ending when it ended, else None. It runs inside the job's DBOS step,
    which cannot start a job, so it never tells the frame itself: the job pushes `egress` next."""
    async with row_scope(Machine, machine_id) as session:
        try:
            row = await lock_briefly(session, lambda: locked_machine(session, machine_id))
        except RowBusy:
            return None  # the next tick offers it again
        held = row.lease() if row is not None else None
        if row is None or held is None or held.since != since:
            return None
        now = await db_now(session)
        if reason is None and is_live(held, now):
            return None
        why = reason if reason is not None else end_reason(held)
        row.end_lease(now)
        egress = row.stepless_egress(now)
        task_id = row.task_id
        await session.commit()
    ended = Ended(held, why, ended_by, now, task_id, machine_id, egress)
    await after_end(ended)
    return ended


async def end_live(machine_id: uuid.UUID, actor_id: uuid.UUID, *, reason: ControlEnd) -> bool:
    """The socket's endings: a holder demoted to viewer (`DEMOTED`), or computer use
    switched off (`DISABLED`, the kill switch) - only while `actor_id` holds a live lease. True when it
    ended."""
    async with row_scope(Machine, machine_id) as session:
        try:
            row = await lock_briefly(session, lambda: locked_machine(session, machine_id))
        except RowBusy:
            return False  # the next proof tries again
        held = row.lease() if row is not None else None
        if row is None or held is None or held.actor_id != actor_id:
            return False
        now = await db_now(session)
        if not is_live(held, now):
            return False  # run out: the lease-end job says it, by its clock
        row.end_lease(now)
        egress = row.stepless_egress(now)
        task_id = row.task_id
        await session.commit()
    await tell_egress(machine_id, egress)
    await after_end(Ended(held, reason, None, now, task_id, machine_id, egress))
    return True


# ── reads, and what follows an ending ─────────────────────────────────────────────────────


async def lease_view(session: AsyncSession, task_id: uuid.UUID) -> LeaseView | None:
    """The task's LIVE lease as the page shows it; None when nobody drives. Read it on a session
    that has not loaded the row before (it is not re-read past the identity map)."""
    row = await Machine.for_task(session, task_id)
    held = row.lease() if row is not None else None
    if held is None or not is_live(held, await db_now(session)):
        return None
    holder = await actor_of(session, held.actor_id)
    return view_of_lease(held, holder.name if holder is not None else "")


async def control_state(task_id: uuid.UUID) -> ControlState | None:
    """What the browser hook sees: the task's live lease, read now from the Machine's
    row - never a cached copy, so a hand-back takes effect at the very next browser call. None
    when the task has no Machine, nobody holds it, or the lease has run out (`until <= now()`,
    Postgres's clock). Only the lease decides: the row's `state` never does (1b's wait loop flips
    a Machine running<->idle on its own retries, and `running` is not "a person drives"). A read
    on its own short session: the caller is a tool mid-run, or `gates.resolve` holding the task's
    row - neither takes the Machine's row here."""
    async with row_scope(Task, task_id) as session:
        if tenant.bound_workspace(session) is None:  # the task is gone
            return None
        row = await Machine.for_task(session, task_id)
        lease = row.lease() if row is not None else None
        if lease is None or not is_live(lease, await db_now(session)):
            return None
        holder = await actor_of(session, lease.actor_id)
        return ControlState(
            holder_name=holder.name if holder is not None else "Someone",
            private=lease.private,
            since=lease.since,
        )


async def ending_of(task_id: uuid.UUID, *, since: dt.datetime) -> tuple[ControlEnd, int] | None:
    """How the lease the browser hook saw (the one claimed at `since`) ended, as
    `machine.control_ended` says it: `(reason, held_s)` - the race path's read, so the
    gate it settles tells the same story as the record. The lease is known by its `since`, never
    by who held it (a name is renamed, shared, or one-lined differently on the hook's side). A
    lease whose clock ran out that nothing has ended yet is said by its own clock (`end_reason`,
    `held_seconds`), as the lease-end job will record it. None only while a lease is live - this
    one, or a later one, whose ending answers the gate. With nothing live, the hook's lease is
    over even when its record is not there (still being written, or lost: the record is
    best-effort), and its own `after_end` may have looked for the gate before it committed - so it
    is said as handed back, held until now, rather than leaving the agent parked for the TTL; a
    late `after_end` then finds the gate closed."""
    async with row_scope(Task, task_id) as session:
        if tenant.bound_workspace(session) is None:  # the task is gone
            return None
        row = await Machine.for_task(session, task_id)
        lease = row.lease() if row is not None else None
        now = await db_now(session)
        if lease is not None and is_live(lease, now):
            return None
        if lease is not None and lease.since == since:
            return end_reason(lease), held_seconds(lease, now)
        endings = await TaskEvent.of_types(session, task_id, ["machine.control_ended"])
        for ended in reversed(endings):
            if isinstance(ended, MachineControlEnded) and ended.lease_since == since:
                return ControlEnd(ended.reason), ended.held_s
        # Unrecorded, the reason is a guess; only the kill switch's changes what the gate does
        # (it declines), so with the switch off the guess is that one, never a hand-back.
        guess = ControlEnd.RELEASED if settings.computer_use.enabled else ControlEnd.DISABLED
        return guess, max(0, int((now - since).total_seconds()))


async def after_end(ended: Ended) -> None:
    """What follows a lease's ending, after its commit, each part best-effort and in its own
    transaction: the durable `machine.control_ended` (the holder is the envelope's actor;
    `ended_by` the owner who ended it; `held_s` until the ending, or until the lease's clock ran out
    when that was first - `held_seconds`), then `screen.state` with the row's lease as it stands
    now - normally nobody, but never "nobody" over a claim that landed meanwhile - and last the
    task's open CONTROL gate, answered with the same `holder` and `held_s`."""
    held_s = held_seconds(ended.lease, ended.at)
    holder: ActorRef | None = None
    by: ActorRef | None = None
    current: LeaseView | None = None
    try:
        async with row_scope(Task, ended.task_id) as session:
            if tenant.bound_workspace(session) is None:
                return  # the task is gone, and its record and stream with it
            holder = await actor_of(session, ended.lease.actor_id)
            by = await actor_of(session, ended.ended_by)
            current = await lease_view(session, ended.task_id)
    except Exception:
        logfire.exception("task {task}: a lease's ending could not be read", task=str(ended.task_id))
    await record_after(
        ended.task_id,
        MachineControlEnded,
        actor=holder,
        reason=str(ended.reason),
        held_s=held_s,
        ended_by=by,
        lease_since=ended.lease.since,
    )
    await publish_state(ended.task_id, current)
    # The ending ends a person's hold on the screen at its provider too (the door's hand-back),
    # before the gate lets the agent's next call try the browser. The gate is
    # answered however the hand-back ends - raising, or cancelled inside its 5 s bound - so the
    # agent never waits out `control_wait_s` for it.
    try:
        await _hand_back(ended.machine_id)
    finally:
        # The ending answers the agent's CONTROL gate - only the gate it ended: this
        # lease's takeover, or a handoff opened before it ended (`min(at, until)`: a lease that ran
        # out is over at its `until`, however late it is noticed). Any reason accepts it with who
        # drove and for how long; the kill switch's (`disabled`) declines it. Its own
        # transaction on the task's row, never the Machine's; it never raises - the gate's TTL is
        # the backstop.
        from prismoid.operations import gates

        await gates.resolve_control(
            ended.task_id,
            holder=holder.name if holder is not None else "Someone",
            held_s=held_s,
            reason=ended.reason,
            lease_since=ended.lease.since,
            ended_at=min(ended.at, ended.lease.until),
        )


async def _hand_back(machine_id: uuid.UUID) -> None:
    """Every lease ending passes `after_end`, in the API or the worker, so the provider's hold on
    the screen ends whichever process ended the lease. Only a
    warm Machine's: any call on a sleeping computer would wake it, and its sleep handed the screen
    back already. Never raises - the provider's own grace (`control_grace_s`) is the backstop."""
    try:
        async with row_scope(Machine, machine_id) as session:
            if tenant.bound_workspace(session) is None:
                return
            row = await Machine.current(session, machine_id)
            if row is None or row.state not in FRAME_STATES or not row.frame_ref:
                return
            if row.lease() is not None:
                # A claim landed meanwhile: a hand-back ends whichever takeover is live, so it
                # would end the new holder's. Their Takeover answers the live session with a
                # fresh ticket, and the provider's grace is the backstop.
                return
            driver, ref = row.driver, row.frame_ref
    except Exception:
        logfire.exception("machine {id}: the screen's hand-back could not read the row", id=str(machine_id))
        return
    if frame_screen.has_control(driver):
        await frame_screen.hand_back(driver, ref)


async def tell_egress(machine_id: uuid.UUID, version: int | None) -> None:
    """After the API's commit that moved an idle Machine's egress (`Machine.stepless_egress`), its
    frame is told by the worker - this process never touches a frame - under an id unique
    to the move, `machine-egress-{id}-{version}` (`lifecycle.push_egress` pins the push to it).
    Never from inside a DBOS step, which cannot start a job (`end_lease`'s caller pushes instead).
    Never raises: a lost enqueue leaves the frame on its older list until the next step's
    boundary, or the idle sleep that destroys it, and is logged."""
    if version is None:
        return
    try:
        await get_dispatcher().enqueue(
            "machine_egress",
            f"machine-egress-{machine_id}-{version}",
            machine_id=str(machine_id),
            version=version,
        )
    except Exception:
        logfire.exception(
            "machine {id}: its egress v{v} could not be handed to the worker", id=str(machine_id), v=version
        )


async def publish_state(task_id: uuid.UUID, view: LeaseView | None) -> None:
    """`screen.state` on the task's stream (Lane 2). Best-effort and silent, like
    presence's frames: a failed send must not cost the lease change it reports, nor put a person's
    name into a log line."""
    frame = ScreenState(
        task_id=TaskId(task_id),
        at=utcnow(),
        holder=(
            ActorRef(type="user", id=ActorId(view.holder_id), name=view.holder_name)
            if view is not None
            else None
        ),
        private=view.private if view is not None else False,
        until=view.until if view is not None else None,
    )
    with contextlib.suppress(Exception):
        await live.notify(frame)


# ── the screen's upstream: the relay reads the row; it never calls a lifecycle verb ──


class NoScreen(Exception):
    """The Machine has no screen to open right now. `reason`: `asleep` (not warm: waking,
    sleeping or asleep), `gone` (reset, or the row is gone), or `no_browser` (no screen at all,
    `has_screen`: a base Machine, or a driver with no screen door)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class HeldLease:
    """The lease as the relay caches it: the row's six columns, and its holder for
    `masked{holder}` ("Ada is signing in privately")."""

    lease: Lease
    holder: ActorRef


@dataclass(frozen=True, slots=True)
class Opened:
    """A Machine's watch upstream as the relay gets it: the stream, the epoch it belongs to, and
    whether its driver's door serves the lease holder on a separate control stream (a driver's
    VNC) - where it does, the watch carries no input."""

    stream: ScreenStream
    epoch: int
    control: bool = False


async def _warm(machine_id: uuid.UUID) -> tuple[str, str, int]:
    """(driver, frame_ref, epoch) of a warm browser Machine with a screen; `NoScreen` otherwise.
    The row is read under its own workspace (`row_scope`), never locked."""
    async with row_scope(Machine, machine_id) as session:
        if tenant.bound_workspace(session) is None:
            raise NoScreen("gone")
        row = await Machine.current(session, machine_id)
        if row is None or row.state == MachineState.GONE:
            raise NoScreen("gone")
        if not has_screen(row.profile, row.driver):
            raise NoScreen("no_browser")
        if row.state not in FRAME_STATES or not row.frame_ref:
            raise NoScreen("asleep")
        return row.driver, row.frame_ref, row.epoch


async def open_upstream(machine_id: uuid.UUID) -> Opened:
    """A warm browser Machine's screen stream, the epoch it belongs to, and whether its holder
    drives through a separate control stream. The frame is reached by its reference through
    `ai/frames/screen.py`: no `bind`, no lifecycle verb - this package is the only one that
    touches a Machine's frame, and the relay only reads it. The epoch tells the relay a
    wake from a reconnect: a new one means nothing of the old frame is shown again.

    Not `ai/frames/screen.open_upstream(driver, ref, kind, ...)`, the driver's door this one
    reaches through `_open_stream`."""
    driver, ref, epoch = await _warm(machine_id)
    stream = await _open_stream(driver, ref)
    return Opened(stream=stream, epoch=epoch, control=frame_screen.has_control(driver))


async def open_control(machine_id: uuid.UUID) -> ScreenStream:
    """The lease holder's control stream of a warm Machine whose door has one (a driver's VNC
    through a takeover). `NoScreen` as `open_upstream`, or `no_control` for a
    door without the kind; `ScreenClosed` when the door refused it."""
    driver, ref, _ = await _warm(machine_id)
    if not frame_screen.has_control(driver):
        raise NoScreen("no_control")
    return await _open_stream(driver, ref, "control")


async def _open_stream(driver: str, frame_ref: str, kind: frame_screen.ScreenKind = "watch") -> ScreenStream:
    """Open a Machine's screen through its driver's door (ADR-065; the
    caller asked `has_screen`). `driver` is the row's own column - the driver that made the frame -
    never this process's setting. The API's display minter goes in as an argument: a door admitted
    by a `dt_` mints one per open; the signer never leaves this layer (A5)."""
    return await frame_screen.open_upstream(driver, frame_ref, kind, display_ticket=tickets.mint_display)


async def lease_of(machine_id: uuid.UUID) -> HeldLease | None:
    """The lease as the row holds it now, with its holder; None when there is none or no row. A
    read, never a lock: expiry is the reader's to compute (`is_live`), so the relay's gate never
    trusts a stale `until`. The mask never hangs on a name: a holder whose actor cannot
    be read is still the holder, unnamed."""
    async with row_scope(Machine, machine_id) as session:
        if tenant.bound_workspace(session) is None:
            return None
        row = await Machine.current(session, machine_id)
        lease = row.lease() if row is not None else None
        if lease is None:
            return None
        holder = await actor_of(session, lease.actor_id)
        if holder is None:
            holder = ActorRef(type="user", id=ActorId(lease.actor_id), name="")
        return HeldLease(lease=lease, holder=holder)
