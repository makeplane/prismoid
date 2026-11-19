# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A step's hand-back of its Machine (ADR-063): `release` at the step's end, `widen` during it,
`release_parked` for a cancelled park, and the reads a park's evidence path asks.

None of these takes the step's session: each opens its own short one on the Machine's row and waits
on that row at most `LOCK_WAIT_S`, past which the hold is left to the reconciler. They never take
the task's row, and they record no usage - each returns the warm segment it closed for its caller
to bill."""

from __future__ import annotations

import contextlib
import time
import uuid
from dataclasses import dataclass

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai import frames
from prismoid.db.models import tenant
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations.machines.acquired import Acquired, AcquiredKind, recording_tag
from prismoid.operations.machines.common import RowBusy, db_now, lock_briefly, locked_machine, required
from prismoid.operations.machines.egress_push import Push, resync, tell
from prismoid.operations.machines.meter import later
from prismoid.schemas.machines import MachineState, Segment, holder_step, segment_seconds


@dataclass(frozen=True, slots=True)
class ParkReleased:
    """What a cancelled park's hand-back found: whether a Machine held the park (False: a park
    from before 1b, which the caller tears down by name), and the warm segment it closed, for the
    caller to bill (`release_parked` never records)."""

    held: bool
    segment: Segment | None = None


async def release(acquired: Acquired, *, parked: bool) -> Segment | None:
    """Hand the frame back (`_release`), then let go of what this process holds open on it
    (`FrameDriver.close`: a remote browser session) - on every path, a lost compare-and-set and
    a raised release included, and never raising itself. The frame stays as the hand-back left it."""
    try:
        return await _release(acquired, parked=parked)
    finally:
        try:
            await acquired.frame.close()
        except Exception as exc:  # a close never raises into a release; the breadcrumb is all it leaves
            logfire.warn(
                "frame close failed on {driver}: {error}",
                driver=acquired.frame.driver,
                error=type(exc).__name__,
            )


async def _release_frame(acquired: Acquired, *, parked: bool) -> Segment | None:
    """A throwaway or legacy frame's hand-back: destroyed unless its step parked, and its run since
    `acquire` returned to bill."""
    if not parked:
        await acquired.frame.destroy()
    seconds = int(time.monotonic() - acquired.started)
    if acquired.task_id is None or seconds <= 0:
        return None
    # Timed here: billed at db now, under the driver that made it (a device seat bills as `device`).
    return Segment(acquired.task_id, acquired.profile, seconds, driver=acquired.frame.driver)


async def _release(acquired: Acquired, *, parked: bool) -> Segment | None:
    """Hand the frame back. A throwaway or legacy frame is destroyed unless its step parked
    (then its resume binds it, as before 1b); a Machine goes `parked` or `idle`. Either way the row
    learns the egress the frame has - a live widen during the step moved the version past it, and a
    resume's push must be newer still. A Machine that goes idle keeps that list as its `step_hosts`,
    and its frame is narrowed to nothing after the commit, pinned one version on - unless someone
    drives its browser, who keeps the list the step ran under (`Machine.let_go`).

    It returns the warm segment it closed and records nothing:
    the step's session may hold the task's row uncommitted (the harvest's events), and a usage
    record takes that row - from here, a wait across two connections, on every release. A
    Machine's segment is `metered_until → now`, and the watermark moves to `now` in the same
    compare-and-set, parked and idle alike (the frame stays warm; the next edge bills on from
    there), never backwards (M1). A throwaway frame's is its run since `acquire`, destroyed or
    parked alike: a park bills the run so far and keeps the frame, and its
    resume binds it with a fresh clock and bills only its own run - each portion once, and a
    cancelled park's teardown by name bills nothing more. None when there is nothing to bill: a
    lost compare-and-set, a row that stayed locked, zero seconds.

    It takes no session: the step's own may be poisoned by the very failure that
    ends it, and a release that raises would leave the Machine running and held by a step that is
    done. It opens its own short one on the Machine's row, as `claim_sleep` does, and waits on that
    row at most `LOCK_WAIT_S`: past it the hold is left to the reconciler."""
    if acquired.kind == AcquiredKind.PAPER:
        return None  # no computer ran: nothing to destroy, nothing to bill
    if acquired.kind != AcquiredKind.MACHINE:
        return await _release_frame(acquired, parked=parked)
    machine_id = required(acquired.machine_id, "a Machine hold carries no machine id")
    narrow: tuple[int, list[str]] | None = None
    async with row_scope(Machine, machine_id) as session:
        try:
            row = await lock_briefly(session, lambda: locked_machine(session, machine_id))
        except RowBusy:
            logfire.warn(
                "machine {id}: its row stayed locked - the reconciler releases the hold", id=str(machine_id)
            )
            return None
        if row is None or row.epoch != acquired.epoch or row.held_by != acquired.holder:
            return None
        now = await db_now(session)
        running = row.state == MachineState.RUNNING  # the reconciler may have moved a row whose frame died
        seconds = 0
        if running:  # a warm frame under this hold: bill it up to now, and the meter runs on
            seconds = segment_seconds(row.metered_until, now)
            row.metered_until = later(row.metered_until, now)
        row.egress_version = max(row.egress_version, acquired.frame.egress_version)
        row.egress_hosts = list(acquired.frame.egress_hosts)
        if parked:
            row.last_active_at = now  # the park is activity too: retention never runs from before it
            if running:
                row.move(MachineState.PARKED, now)
        else:
            row.last_active_at = now
            if running:
                row.move(MachineState.IDLE, now)
            # An idle Machine reaches nothing unless someone drives its browser.
            if (version := row.let_go(now)) is not None:
                narrow = (version, list(row.egress_hosts))
        segment = (
            Segment(row.task_id, row.profile, seconds, at=now, driver=row.driver) if seconds > 0 else None
        )
        await session.commit()
    if narrow is not None:
        with contextlib.suppress(Exception):  # best-effort: the next boundary's push is newer still
            await acquired.frame.allow(narrow[1], version=narrow[0])
    return segment


async def widen(acquired: Acquired, domains: list[str]) -> bool:
    """A live widen during the step (`BrowserPorts.allow`): push the list, and on a Machine
    move the row's egress with it, in its own short transaction - the step's
    session sits committed and idle across the run. Without the row, a crash-recovered attempt's
    boundary push would carry a version the frame already passed, be dropped as stale, and leave
    the frame on this attempt's wider list (a `once` grant outliving its step). The hold is checked
    BEFORE the push and kept across it: a zombie attempt never widens the frame of the
    attempt that took its Machine over. A throwaway frame just pushes. A push refused as stale is
    re-synced once (`resync`): the holder's list is the truth. False when it still did not land,
    when the Machine is no longer this attempt's, or when its row stayed locked past `LOCK_WAIT_S`."""
    if acquired.kind != AcquiredKind.MACHINE:
        return await acquired.frame.allow(domains)
    machine_id = required(acquired.machine_id, "a Machine hold carries no machine id")
    async with row_scope(Machine, machine_id) as session:
        try:
            row = await lock_briefly(session, lambda: locked_machine(session, machine_id))
        except RowBusy:
            logfire.warn("machine {id}: its row stayed locked - the widen is refused", id=str(machine_id))
            return False
        if row is None or row.epoch != acquired.epoch or row.held_by != acquired.holder:
            return False
        pushed = await resync(acquired.frame, domains, hold=None)  # the holder's list, under its row's lock
        if pushed:
            row.egress_version = max(row.egress_version, acquired.frame.egress_version)
            row.egress_hosts = list(acquired.frame.egress_hosts)
            await session.commit()
        return pushed


async def release_parked(task_id: uuid.UUID, step_id: uuid.UUID) -> ParkReleased:
    """A cancelled park: the Machine goes back to idle, or, if it already slept, just loses
    its holder. `held` is False when no Machine held this step (a park from before 1b). A Machine
    this step's resume is already running or waking is left to that attempt, which hands it back
    itself. A park that goes idle closes its warm segment and hands it back for the caller
    to bill on its own session - never here: this transaction waits under `lock_briefly`'s timeout,
    and the task's row a usage record takes would turn a lock wait into a 55P03 that aborts the
    hand-back (the release's rule, T11-bill-on-caller).

    It takes ids, not rows, and no session: its own, bound to the task's workspace - so a
    caller whose session failed (and whose rows expired with the rollback) can still give a park
    back. A Machine that goes idle loses the park's egress, `once` hosts included (an
    idle Machine's background processes reach nothing) - unless someone drives its
    browser, who keeps it (`Machine.let_go`): the row records the empty list one version
    past the park's, and the frame is told after the commit, best-effort - a push that fails is
    superseded by the next boundary's, which is newer still. The row is waited on at most
    `LOCK_WAIT_S`: past it the hold is left to the reconciler (`held`: a Machine is
    there, and its segment is billed by whichever edge comes next)."""
    narrow: Push | None = None
    segment: Segment | None = None
    async with row_scope(Task, task_id) as session:
        if tenant.bound_workspace(session) is None:
            return ParkReleased(held=False)  # the task is gone, and its Machine with it
        try:
            row = await lock_briefly(session, lambda: Machine.locked_for_task(session, task_id))
        except RowBusy:
            logfire.warn(
                "task {id}: its Machine's row stayed locked - the reconciler releases the park",
                id=str(task_id),
            )
            return ParkReleased(held=True)
        held = holder_step(row.held_by) if row is not None else None
        if row is None or held is None or held[0] != str(step_id):
            return ParkReleased(held=False)
        if row.state not in (MachineState.RUNNING, MachineState.WAKING):
            now = await db_now(session)
            if row.state == MachineState.PARKED:
                seconds = segment_seconds(row.metered_until, now)  # the park's warm time
                row.metered_until = later(row.metered_until, now)  # warm and idle: the meter runs on
                if seconds > 0:
                    segment = Segment(row.task_id, row.profile, seconds, at=now, driver=row.driver)
                row.last_active_at = now
                row.move(MachineState.IDLE, now)
            if (version := row.let_go(now)) is not None and row.frame_ref:
                narrow = Push(
                    task_id, row.profile, row.frame_ref, list(row.egress_hosts), version, row.driver
                )
            await session.commit()
    if narrow is not None:
        await tell(narrow)
    return ParkReleased(held=True, segment=segment)


async def discard_legacy(task_id: uuid.UUID, step_id: uuid.UUID, attempt: int, *, profile: str) -> None:
    """A park no Machine holds - one from before 1b, or a seat's throwaway frame - is torn down by
    its old name, `frame-{task}-{step}-{attempt}` (only this package calls lifecycle verbs)."""
    await frames.new_frame(f"{task_id}-{step_id}-{attempt}", profile=profile).destroy()


async def holds(session: AsyncSession, task_id: uuid.UUID, step_id: uuid.UUID) -> bool:
    row = await Machine.for_task(session, task_id)
    held = holder_step(row.held_by) if row is not None else None
    return held is not None and held[0] == str(step_id)


async def parked_frame(
    session: AsyncSession, task: Task, step: PlanStep, *, profile: str
) -> frames.FrameDriver | None:
    """The frame a parked step left running - the Machine's, or a pre-1b one by its old name -
    for the cancelled-park evidence path. None when it is gone."""
    row = await Machine.for_task(session, task.id)
    held = holder_step(row.held_by) if row is not None else None
    if row is not None and held is not None and held[0] == str(step.id):
        if not row.frame_ref or row.state in (MachineState.ASLEEP, MachineState.WAKING):
            # An asleep row's ref is not a running frame: binding it would wake it. A waking row's
            # is mid-resume (a cancelled park whose wake died): only the wake path binds it.
            return None
        frame = frames.new_frame(
            str(task.id),
            driver=row.driver,
            profile=row.profile,
            attempt=step.attempt,
            name=row.frame_ref,
            recording=recording_tag(step),
        )
    else:
        frame = frames.new_frame(f"{task.id}-{step.id}-{step.attempt}", profile=profile, attempt=step.attempt)
    try:
        await frame.bind()
    except RuntimeError:
        return None
    return frame
