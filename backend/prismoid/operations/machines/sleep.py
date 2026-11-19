# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Sleep, Sleep now, reset, expiry and forgetting (ADR-063), and
the kill switch's browser stop.

Every transition is a short `FOR UPDATE` + compare-and-set on (state, epoch); the slow work
(quiescing, archiving, uploading, destroying) runs between two transactions, guarded by the
`sleeping` state. A sleep is one CLAIM, and a claim has an identity - the row's `state_since` while
it holds the row (`SleepClaim.at`) - so a step whose claim is no longer the row's (a crashed step
re-run after another claim, a sleep the reconciler handed back) changes nothing but its own debris.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any, Literal

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai import frames
from prismoid.ai.frames.driver import FrameUnreachable, KeepsQuarantine, NativeSleep, StopsServices
from prismoid.conf import settings
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.errors import Conflict, NoMachine
from prismoid.operations import cipher
from prismoid.operations.dispatch import get_dispatcher
from prismoid.operations.machines import store
from prismoid.operations.machines.common import (
    actor_of,
    bound,
    db_now,
    lease_cleared,
    locked_machine,
    record,
    record_lost,
    refuse_while_driven,
)
from prismoid.operations.machines.meter import meter_segment, summary
from prismoid.operations.steering import require_project_editor
from prismoid.schemas.device import DEVICE_DRIVER
from prismoid.schemas.events import MachineReset, MachineSlept
from prismoid.schemas.machines import (
    MachineProfile,
    MachineState,
    MachineSummary,
    SleepReason,
    browser_key,
    checkpoint_key,
    machine_prefix,
    persistent,
    sleep_job_id,
)
from prismoid.schemas.screen import Lease

_BUSY = "It's working; pause the task first."


@dataclass(frozen=True, slots=True)
class SleepClaim:
    """One claim on a Machine, for one sleep. `at` is its identity: the row's `state_since` while
    this claim holds it. `after_s` is how long the Machine sat unused (the event's), and `since` its
    clock before the claim, which a failed sleep puts back."""

    at: dt.datetime
    after_s: int
    since: dt.datetime

    def wire(self) -> dict[str, Any]:
        """Plain values, for a job's kwargs and a step's recorded output."""
        return {"claim_at": self.at.isoformat(), "after_s": self.after_s, "since": self.since.isoformat()}

    @classmethod
    def from_wire(cls, *, claim_at: str, after_s: int, since: str) -> SleepClaim:
        return cls(
            at=dt.datetime.fromisoformat(claim_at), after_s=after_s, since=dt.datetime.fromisoformat(since)
        )


def claim_row(row: Machine, now: dt.datetime) -> SleepClaim:
    """The one claim rule, in the caller's transaction: an `idle` or `parked` row
    goes `sleeping`. Idle counts from its last activity, parked from when it parked. Package-visible
    so `lifecycle` claims a sleep in the same commit that hands a Machine back -
    one rule, never a copy."""
    start = row.last_active_at if row.state == MachineState.IDLE else row.state_since
    after_s = max(0, int((now - start).total_seconds()))
    claim = SleepClaim(at=now, after_s=after_s, since=row.state_since)
    row.move(MachineState.SLEEPING, now)
    return claim


def _holds(row: Machine, epoch: int, claim: SleepClaim) -> bool:
    """The row is still this claim's: `sleeping`, at this epoch, since this claim's own moment."""
    return row.epoch == epoch and row.state == MachineState.SLEEPING and row.state_since == claim.at


def _disowned(row: Machine | None, epoch: int) -> bool:
    """For a step that lost its claim: True when nothing owns this epoch's frame any more (the row
    is deleted, asleep or gone, or at a new epoch), so what the step made is debris to clean up.
    False when another claim or a holder has the row - and the frame and this epoch's keys are
    theirs, never the stale step's to touch."""
    return row is None or row.epoch != epoch or row.state in (MachineState.ASLEEP, MachineState.GONE)


async def claim_sleep(machine_id: uuid.UUID, epoch: int) -> SleepClaim | None:
    """Claim the Machine for a sleep and commit: `idle` or `parked` at this epoch → `sleeping`.
    None when it is not ours to sleep (gone, moved on, busy, or already sleeping or asleep) - or
    when a control lease is on the row: someone drives it, or drove it and the
    lease-end job ends that first, restarting the idle clock. The job runs this as its own step, so a
    worker that dies after it resumes at the slow part."""
    async with row_scope(Machine, machine_id) as session:
        row = await locked_machine(session, machine_id)
        if row is None or row.epoch != epoch or row.state not in (MachineState.IDLE, MachineState.PARKED):
            return None
        if row.lease() is not None:
            return None
        claim = claim_row(row, await db_now(session))
        await session.commit()
        return claim


async def _after_failed_checkpoint(
    machine_id: uuid.UUID, epoch: int, claim: SleepClaim, frame: frames.FrameDriver | None
) -> str:
    """Hand the claim back after a checkpoint failed, and `sleep`'s answer for it."""
    fate = await _unclaim(machine_id, epoch, claim)
    if fate == "unclaimed":
        if frame is not None:
            with contextlib.suppress(Exception):  # a frame that died under us cannot thaw
                await frame.thaw()
        return "failed"
    if fate == "taken":
        return "skipped"  # another claim or a holder has the row and its frame
    if frame is not None:
        await _destroy(frame, machine_id)  # the row moved on (the task was deleted): debris
    return "gone"


async def sleep(
    machine_id: uuid.UUID,
    epoch: int,
    reason: SleepReason,
    *,
    actor_id: uuid.UUID | None = None,
    claim: SleepClaim | None = None,
) -> str:
    """Store the Machine's checkpoint and destroy its frame - or, on a native driver, suspend it
    (ADR-063).
    A device frame is never archived: its leftovers are stopped and it is kept.

    Without `claim` it claims first (`claim_sleep`), for a direct caller. With one (Sleep now's
    request, or the job's claim step), it runs only while that claim still holds the row -
    checked when it starts, when it gives the row back and when it finishes - so a step whose
    claim is no longer the row's changes nothing.

    Returns `slept`, `dropped` (over the cap: gone), `skipped` (not ours to sleep: never claimed,
    or the claim was taken over, in which case the frame and this epoch's keys are left to the
    claim that has them), `failed` (the frame or the upload failed: back to warm, the next sweep
    retries) or `gone` (the row moved on - the task was deleted, or the row reconciled - and our
    frame and the objects no row references are cleaned up). A frame or store
    failure never raises. What can still raise: a database error in one of the short transactions
    (the row stays `sleeping`, and the reconciler's stuck rule resolves it), and the object deletes
    after the final commit (harmless: the orphan arm reclaims what they miss)."""
    if claim is None:
        claim = await claim_sleep(machine_id, epoch)
        if claim is None:
            return "skipped"
    held = await _claimed(machine_id, epoch, claim)
    if held is None:
        return "skipped"

    frame: frames.FrameDriver | None = None
    try:
        frame = (
            frames.new_frame(str(held.task_id), driver=held.driver, profile=held.profile, name=held.ref)
            if held.ref
            else None
        )
        quiet = await _quiesce(frame, machine_id, epoch, held.profile)
    except Exception:
        logfire.exception("machine {id}: the checkpoint failed; back to warm", id=str(machine_id))
        return await _after_failed_checkpoint(machine_id, epoch, claim, frame)

    done = await _hand_back_asleep(machine_id, epoch, claim, held, quiet, reason=reason, actor_id=actor_id)
    if isinstance(done, str):
        return done
    # After the commit, never before: a claim learns it still holds the row before it destroys the
    # frame, so a stale one never kills a frame another claim is archiving. The row no longer names
    # it, so a destroy that fails here is the reconciler's.
    if quiet.frame is not None and not quiet.kept:
        await _destroy(quiet.frame, machine_id)
    if quiet.vanished:
        await record_lost(held.task_id)  # after the commit, outside the row's lock; never raises
    if done.cleared is not None:
        await lease_cleared(held.task_id, machine_id, done.cleared, done.now)  # never raises
    await store.delete_keys(k for k in done.stale if k not in quiet.new_keys)
    return "dropped" if quiet.dropped is not None else "slept"


@dataclass(frozen=True, slots=True)
class _Held:
    """What a sleep read off the row while its claim held it."""

    task_id: uuid.UUID
    profile: str
    ref: str | None
    driver: str | None
    old_work: str | None
    old_browser: str | None


async def _claimed(machine_id: uuid.UUID, epoch: int, claim: SleepClaim) -> _Held | None:
    """The row's facts while `claim` still holds it, else None. Commits nothing; the row's lock is
    let go when the scope ends, before any slow work."""
    async with row_scope(Machine, machine_id) as session:
        row = await locked_machine(session, machine_id)
        if row is None or not _holds(row, epoch, claim):
            return None
        return _Held(
            task_id=row.task_id,
            profile=row.profile,
            ref=row.frame_ref,
            driver=row.driver,
            old_work=row.checkpoint_object,
            old_browser=row.browser_object,
        )


@dataclass(frozen=True, slots=True)
class _Quiet:
    """What the slow part left: the frame (None when it vanished or there was none), what was
    stored, and how the frame sleeps."""

    frame: frames.FrameDriver | None
    work: store.Stored | None = None
    browser: store.Stored | None = None
    dropped: int | None = None  # bytes over the cap: nothing was kept
    vanished: bool = False  # the frame went before it was saved: its unsaved work is lost
    kept: bool = False  # a native frame suspended with its memory: the row keeps naming it (ADR-063)
    bare: bool = False  # a frame never archived: kept, asleep with no checkpoint

    @property
    def new_keys(self) -> list[str]:
        return [s.key for s in (self.work, self.browser) if s is not None]


async def _quiesce(
    frame: frames.FrameDriver | None, machine_id: uuid.UUID, epoch: int, profile: str
) -> _Quiet:
    """The slow part, holding no lock: checkpoint and suspend, or stop a persistent frame's
    services. A frame that is no longer bound falls back to the checkpoint already stored. Raises
    what the frame or the store raises; the caller hands the claim back."""
    if frame is None:
        return _Quiet(frame=None)
    if not await bound(frame):
        return _Quiet(frame=None, vanished=True)
    if persistent(frame.sleep_kind):
        # The third sleep: `/work` is the user's own folder, so
        # nothing is frozen or archived. What the task left running stops; the frame is kept.
        if isinstance(frame, StopsServices):
            await frame.stop_services()
        return _Quiet(frame=frame, kept=True, bare=True)
    work, browser, dropped = await _checkpoint(frame, machine_id, epoch, profile)
    return _Quiet(frame=frame, work=work, browser=browser, dropped=dropped, kept=await _suspend(frame, work))


@dataclass(frozen=True, slots=True)
class _Asleep:
    """A committed sleep: the keys it stopped referencing, a lease the move cleared, and when."""

    stale: list[str | None]
    cleared: Lease | None
    now: dt.datetime


async def _hand_back_asleep(
    machine_id: uuid.UUID,
    epoch: int,
    claim: SleepClaim,
    held: _Held,
    quiet: _Quiet,
    *,
    reason: SleepReason,
    actor_id: uuid.UUID | None,
) -> _Asleep | str:
    """Write the slept row and its event, and commit - while `claim` still holds the row. When it
    no longer does, the outcome instead: `skipped` (another claim has the row, its frame and this
    epoch's keys) or `gone` (nothing owns this epoch any more: our frame and new keys are debris,
    cleaned up here)."""
    async with row_scope(Machine, machine_id) as session:
        row = await locked_machine(session, machine_id)
        if row is None or not _holds(row, epoch, claim):
            disowned = _disowned(row, epoch)
            referenced = {row.checkpoint_object, row.browser_object} if row is not None else set()
            await session.rollback()  # let go of the row (if any) before the slow part
            if not disowned:
                return "skipped"  # the claim that took the row owns its frame and this epoch's keys
            if quiet.frame is not None:
                await _destroy(quiet.frame, machine_id)
            await store.delete_keys(k for k in quiet.new_keys if k not in referenced)
            return "gone"
        now = await db_now(session)
        stale = _store_objects(row, held, quiet, now)
        # The idle tail: the frame goes, so its last segment is billed and the meter stops -
        # here, under the row's lock, as this transaction's own event is (no step holds the task's
        # row while its Machine sleeps).
        await meter_segment(session, row, now)
        row.metered_until = None
        if not quiet.kept:
            row.frame_ref = None  # a natively asleep row keeps naming its suspended frame (ADR-063)
        # A lease still on the row ran out before the lease-end job got to it (a live one never
        # reaches a sleep): the move clears it, and its ending is said after the commit.
        cleared = row.move(
            MachineState.ASLEEP if row.checkpoint_object or quiet.bare else MachineState.GONE, now
        )
        await record(
            session,
            row.task_id,
            MachineSlept,
            actor=await actor_of(session, actor_id),
            reason=str(reason),
            after_s=claim.after_s,
            dropped_bytes=quiet.dropped,
        )
        await session.commit()
    return _Asleep(stale=stale, cleared=cleared, now=now)


def _store_objects(row: Machine, held: _Held, quiet: _Quiet, now: dt.datetime) -> list[str | None]:
    """Point the row at what this sleep stored, or forget what it can no longer use (in the
    caller's transaction); returns the keys no longer referenced, for deleting after the commit."""
    fate = checkpoint_fate(
        dropped=quiet.dropped is not None,
        stored=quiet.work is not None,
        had_checkpoint=row.has_checkpoint,
        bare=quiet.bare,
        vanished=quiet.vanished,
    )
    if fate.clear:
        # A vanished frame's loss is said after the commit, so the next wake does not say it too.
        row.gone_reason = fate.gone_reason
        return _clear_objects(row)
    if quiet.work is None:
        return []
    work, browser = quiet.work, quiet.browser
    row.checkpoint_object, row.checkpoint_sha256 = work.key, work.sha256
    row.checkpoint_bytes, row.checkpoint_at = work.size, now
    # A browser archive that failed is a lost login, never an older one: the last
    # epoch's cookies may hold a session the user has since logged out of.
    row.browser_object = browser.key if browser else None
    row.browser_bytes = browser.size if browser else None
    row.browser_cipher_key_id = cipher.key_id() if browser else None
    return [held.old_work, held.old_browser]


@dataclass(frozen=True, slots=True)
class CheckpointFate:
    """What a finished sleep does with the row's stored objects: `clear` forgets them all, with the
    row's `gone_reason`; otherwise the row keeps its objects, or takes the newly stored ones."""

    clear: bool
    gone_reason: Literal["oversize", "lost"] | None = None


def checkpoint_fate(
    *, dropped: bool, stored: bool, had_checkpoint: bool, bare: bool, vanished: bool
) -> CheckpointFate:
    """Over the cap, everything goes ("oversize"). With nothing stored now, nothing stored before
    and no persistent frame to keep, the Machine is gone: "lost" - unless its frame vanished, whose
    loss is said once, after the commit, with no reason on the row. Otherwise nothing is cleared."""
    if dropped:
        return CheckpointFate(clear=True, gone_reason="oversize")
    if not stored and not had_checkpoint and not bare:
        return CheckpointFate(clear=True, gone_reason=None if vanished else "lost")
    return CheckpointFate(clear=False)


async def retire_or_sleep(
    machine_id: uuid.UUID, epoch: int, reason: SleepReason, *, claim: SleepClaim | None = None, **kw: Any
) -> str:
    """The sleep job's entry: `unused` for a boot nobody used, else `sleep`, as it answers."""
    if claim is None:
        claim = await claim_sleep(machine_id, epoch)
        if claim is None:
            return "skipped"
    if await _retire_unused(machine_id, epoch, claim):
        return "unused"
    return await sleep(machine_id, epoch, reason, claim=claim, **kw)


async def _retire_unused(machine_id: uuid.UUID, epoch: int, claim: SleepClaim) -> bool:
    """An early boot no step ever bound: nothing to keep, and nobody asked for
    this minute of it, so it goes unbilled and holds nothing for 14 days. "Never bound" is a first
    incarnation with nothing stored, on a task none of whose steps ever ran, a step a replan since
    dropped included, and whose browser nobody has driven (what a driver left is on this computer
    alone); a seat's step in its own frame counts as started, so the rule keeps to what it can
    prove. A persistent frame is the task's work, and never goes this way."""
    async with row_scope(Machine, machine_id) as session:
        row = await locked_machine(session, machine_id)
        if row is None or not _holds(row, epoch, claim) or row.epoch != 1 or row.has_objects:
            return False
        if persistent(frames.sleep_kind(row.driver)):
            return False
        if await PlanStep.any_started(session, row.task_id) or await TaskEvent.any_step_ran(
            session, row.task_id
        ):
            return False
        if await TaskEvent.has_type(session, row.task_id, "machine.control_claimed"):
            return False
        ref, driver = row.frame_ref, row.driver
        row.metered_until = None
        row.frame_ref = None
        row.move(MachineState.GONE, await db_now(session))
        await session.commit()
    if ref:
        with contextlib.suppress(Exception):  # the row no longer names it: the reconciler's
            await frames.destroy_ref(ref, driver=driver)
    return True


def _clear_objects(row: Machine) -> list[str | None]:
    """Forget every stored object on the row (in its transaction); returns their keys, for
    deleting after the commit - never before, so a rollback never strands a row on a lost key."""
    keys = [row.checkpoint_object, row.browser_object]
    row.checkpoint_object = row.checkpoint_sha256 = row.browser_object = row.browser_cipher_key_id = None
    row.checkpoint_bytes = row.browser_bytes = None
    row.checkpoint_at = None
    return keys


async def stop_browser(machine_id: uuid.UUID, epoch: int) -> str:
    """The kill switch's browser arm: tell a warm browser Machine's sidecar to stop
    Chromium, so its cookies flush to the profile on `/browser` - the same `shutdown` a sleep
    sends before its archive (`_checkpoint`). The Machine keeps its normal idle life, `/work` is
    untouched, and the profile stays on disk (and in its archive) for when the switch returns: the
    next browser verb then starts Chromium again.

    Only an `idle` or `parked` browser row at this epoch, with a frame, is touched: a running
    step's Chromium is its own, and a frame from another epoch is not this job's. `stopped`, or
    `skipped` (not ours, or the frame is gone - the reconciler's). A shutdown that fails is still
    `stopped`: the daemon may already be down.

    ONE sidecar call, and the job runs once per Machine per epoch (its id carries no tick): when
    the daemon is gone, `sidecar.ctl` spawns it (and Chromium) to answer, and this shutdown stops
    it again - acceptable once per epoch, never every sweep tick."""
    async with row_scope(Machine, machine_id) as session:
        row = await locked_machine(session, machine_id)
        if (
            row is None
            or row.epoch != epoch
            or row.profile != MachineProfile.BROWSER
            or row.state not in (MachineState.IDLE, MachineState.PARKED)
            or not row.frame_ref
        ):
            return "skipped"
        task_id, ref, driver = row.task_id, row.frame_ref, row.driver
    # The row's lock is let go (the scope rolled back) before the frame is touched.
    frame = frames.new_frame(str(task_id), driver=driver, profile=MachineProfile.BROWSER, name=ref)
    if not await bound(frame):
        return "skipped"
    with contextlib.suppress(Exception):
        await frame.browser({"verb": "shutdown"})
    return "stopped"


async def _checkpoint(
    frame: frames.FrameDriver, machine_id: uuid.UUID, epoch: int, profile: str
) -> tuple[store.Stored | None, store.Stored | None, int | None]:
    """Quiesce and upload: (the `/work` checkpoint, the browser archive, the bytes dropped over
    the cap). Over the cap nothing is kept; a browser archive that fails is only a lost login."""
    cap = settings.machines.checkpoint_max_bytes
    if profile == MachineProfile.BROWSER:
        with contextlib.suppress(Exception):
            await frame.browser({"verb": "shutdown"})  # Chromium flushes its cookies
        await asyncio.sleep(1.0)
    await frame.freeze()
    try:
        work = await store.save_work(frame, checkpoint_key(machine_id, epoch), cap)
    except store.Oversize:
        return None, None, await _work_bytes(frame)
    browser: store.Stored | None = None
    if profile == MachineProfile.BROWSER:
        try:
            browser = await store.save_browser(frame, browser_key(machine_id, epoch), cap)
        except Exception:
            logfire.warn("machine {id}: the browser archive was not kept", id=str(machine_id))
    return work, browser, None


async def _suspend(frame: frames.FrameDriver, work: store.Stored | None) -> bool:
    """The one place the archive and native sleeps differ (ADR-063). An `archive`
    driver's frame is destroyed after the commit (False). A `native` one with `/work` stored is
    thawed and suspended with its memory (True): the row keeps naming it, and the next wake resumes it. Over
    the cap (nothing stored) it goes the archive way. A failure here is a failed sleep, as any other
    is: the frame stays awake (the caller thaws it)."""
    if frame.sleep_kind != "native" or work is None or not isinstance(frame, NativeSleep):
        return False
    await frame.thaw()
    await frame.suspend()
    return True


async def _work_bytes(frame: frames.FrameDriver) -> int:
    """How big `/work` really is, for the event that says what was not kept."""
    # BSD `du` has no -b: no matter, a checkpoint is the only caller and a device frame takes none.
    out = await frame.exec("du -sb /work 2>/dev/null | cut -f1")
    try:
        return int(out.stdout.split()[0])
    except (IndexError, ValueError):
        return 0


async def _destroy(frame: frames.FrameDriver, machine_id: uuid.UUID) -> None:
    """Best-effort: a frame that will not die is the reconciler's (its row no longer owns it). A
    `persistent` frame is never destroyed here - a newer epoch's wake binds that same frame - and is
    left to the task's end of life."""
    if persistent(frame.sleep_kind):
        return
    try:
        await frame.destroy()
    except Exception:
        logfire.warn("machine {id}: the frame was not destroyed; the reconciler will", id=str(machine_id))


async def _unclaim(
    machine_id: uuid.UUID, epoch: int, claim: SleepClaim
) -> Literal["unclaimed", "disowned", "taken"]:
    """A failed sleep puts the Machine back the way it was - parked if a step still holds it, with
    its clock where it was, so the next sweep retries - but only while its claim holds the row.
    Otherwise `disowned` (the row moved on: the frame is debris) or `taken` (another claim or a
    holder has the row: leave everything alone)."""
    async with row_scope(Machine, machine_id) as session:
        row = await locked_machine(session, machine_id)
        if row is None or not _holds(row, epoch, claim):
            return "disowned" if _disowned(row, epoch) else "taken"
        row.move(MachineState.PARKED if row.held_by else MachineState.IDLE, claim.since)
        await session.commit()
        return "unclaimed"


async def request_sleep(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> Machine:
    """Sleep now: an editor's button. Moves the row to `sleeping` here, so the 202 says so,
    and hands the slow part to the job. A repeat is a no-op. While someone
    drives the browser it is a 409 that names them."""
    task = await Task.get_for(session, task_id, actor)
    await require_project_editor(session, task, actor)
    row = await Machine.locked_for_task(session, task.id)
    if row is None:
        raise NoMachine("this task has no Machine yet")
    now = await db_now(session)
    await refuse_while_driven(session, row, now)
    if row.state in (MachineState.RUNNING, MachineState.WAKING):
        raise Conflict(_BUSY)
    if row.state in (MachineState.SLEEPING, MachineState.ASLEEP, MachineState.GONE):
        return row
    claim = claim_row(row, now)
    machine_id, epoch = row.id, row.epoch
    await session.commit()
    # The id is THIS claim's: a Sleep now that failed (back to warm) must not use up the id the
    # next click needs - DBOS never re-runs a finished workflow id. A double click enqueues once,
    # because only a real transition gets here. The job carries the claim, identity and all.
    await get_dispatcher().enqueue(
        "machine_sleep",
        sleep_job_id(machine_id, epoch, claim.at),
        machine_id=str(machine_id),
        epoch=epoch,
        reason=str(SleepReason.MANUAL),
        actor_id=str(actor.id),
        claimed=True,
        **claim.wire(),
    )
    return row


async def reset(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> Machine:
    """Reset: the row goes `gone` at a new epoch in one transaction; a job destroys the old
    frame and deletes the old objects after commit (the reconciler catches anything it misses).
    While someone drives the browser it is a 409 that names them; a lease whose
    clock ran out is cleared with the move, and its ending said after the commit."""
    task = await Task.get_for(session, task_id, actor)
    await require_project_editor(session, task, actor)
    row = await Machine.locked_for_task(session, task.id)
    if row is None:
        raise NoMachine("this task has no Machine yet")
    now = await db_now(session)
    await refuse_while_driven(session, row, now)
    if row.state in (MachineState.RUNNING, MachineState.WAKING):
        raise Conflict(_BUSY)
    if row.state == MachineState.SLEEPING:
        raise Conflict("It's going to sleep; try again in a moment.")
    if row.state == MachineState.GONE and not row.has_objects:
        return row
    old_ref, old_driver = row.frame_ref, row.driver
    if old_ref:  # a warm Machine's last segment, billed with the reset's own event
        await meter_segment(session, row, now)
    row.metered_until = None
    keys = [k for k in _clear_objects(row) if k]
    row.epoch += 1
    row.frame_ref = None
    row.gone_reason = None
    cleared = row.move(MachineState.GONE, now)  # a parked step keeps `held_by`, and is told on resume
    await record(session, task.id, MachineReset, actor=await actor_of(session, actor.id))
    machine_id, epoch = row.id, row.epoch
    await session.commit()
    if cleared is not None:  # said before the cleanup is enqueued: the ending, then the reset's work
        await lease_cleared(task.id, machine_id, cleared, now)  # never raises
    await get_dispatcher().enqueue(
        "machine_reset",
        f"machine-reset-{machine_id}-{epoch}",
        machine_id=str(machine_id),
        frame_ref=old_ref,
        keys=keys,
        driver=old_driver,
    )
    return row


async def cleanup(
    frame_ref: str | None, keys: list[str], driver: str | None = None, machine_id: uuid.UUID | None = None
) -> None:
    """The reset job's body: the old frame and objects, best-effort (the reconciler and the
    orphan arm catch anything left). `driver` is the one that holds the old frame (ADR-063); a job
    enqueued before it existed carries none, which is this worker's.

    A persistent frame (the device) is never best-effort: no reconciler takes it while its row
    exists, so its destroy raises and the job asks again until the helper answers (`reset_retryable`;
    a frame the helper no longer has is destroyed already). It is destroyed only while the row is
    still `gone`: a wake since destroyed it before making its own (`wake.rebuild`), which wears the same
    name, and a deleted task's `forget` took it."""
    try:
        kept = persistent(frames.sleep_kind(driver))
    except ValueError:  # a driver this worker does not know: its destroy fails below, as it always did
        kept = False
    if frame_ref and kept:
        if machine_id is None or await _still_gone(machine_id):
            await frames.destroy_ref(frame_ref, driver=driver)
    elif frame_ref:
        with contextlib.suppress(Exception):
            await frames.destroy_ref(frame_ref, driver=driver)
    await store.delete_keys(keys)


async def _still_gone(machine_id: uuid.UUID) -> bool:
    """Is the reset Machine still `gone` (not deleted, not woken since)? The row's lock is let go
    (the scope rolls back) before the frame is touched."""
    async with row_scope(Machine, machine_id) as session:
        row = await locked_machine(session, machine_id)
        return row is not None and row.state == MachineState.GONE


def reset_retryable(err: BaseException) -> bool:
    """The `machine_reset` step's `should_retry`: a persistent frame's driver that did not answer.
    Nothing else is retried (a cloud cleanup raises only from the object store, as it always did)."""
    return isinstance(err, FrameUnreachable)


async def expire(machine_id: uuid.UUID, epoch: int) -> str:
    """Retention: delete a sleeping Machine's objects 14 days after its last activity."""
    async with row_scope(Machine, machine_id) as session:
        row = await locked_machine(session, machine_id)
        if row is None or row.epoch != epoch or row.state not in (MachineState.ASLEEP, MachineState.GONE):
            return "skipped"
        if not row.has_objects:
            return "skipped"  # nothing to expire; keep the reason it is gone (oversize, lost)
        keys = _clear_objects(row)
        row.gone_reason = "expired"
        row.move(MachineState.GONE, await db_now(session))
        await session.commit()
    await store.delete_keys(keys)
    return "expired"


async def release_asleep(machine_id: uuid.UUID, epoch: int) -> str:
    """A workspace past its asleep cap: the computer goes and its room on the host with
    it. The row stays asleep on its checkpoint, the archive path's shape, so the next wake rebuilds.
    The row lets go first: a wake that wins the lock keeps its computer, and one that comes after
    finds no computer to resume. A destroy that fails is the reconciler's, the row no longer names it."""
    async with row_scope(Machine, machine_id) as session:
        row = await locked_machine(session, machine_id)
        if row is None or row.epoch != epoch or row.state != MachineState.ASLEEP:
            return "skipped"
        if not row.frame_ref or not row.has_checkpoint:
            return "skipped"
        ref, driver = row.frame_ref, row.driver
        row.frame_ref = None
        await session.commit()
    with contextlib.suppress(Exception):
        await frames.destroy_ref(ref, driver=driver)
    return "released"


@dataclass(frozen=True, slots=True)
class Forgotten:
    machine_id: uuid.UUID
    frame_ref: str | None
    driver: str | None = None


async def forgettable(session: AsyncSession, task_id: uuid.UUID) -> Forgotten | None:
    """Read before a task's rows cascade away (the artifact-digest pattern in `delete_task`)."""
    row = await Machine.for_task(session, task_id)
    return Forgotten(row.id, row.frame_ref, row.driver) if row is not None else None


async def _forget_quarantine(driver: str, frame_ref: str) -> None:
    """A device frame's quarantine goes with its task, best effort, as the frame itself did."""
    with contextlib.suppress(Exception):
        frame = frames.new_frame("", driver=driver, name=frame_ref)
        if isinstance(frame, KeepsQuarantine):
            await frame.forget_quarantine()


async def forget(found: Forgotten) -> None:
    """After the delete commits: the frame and every object under the Machine's prefix. Also the
    hook GDPR erasure must call when it lands."""
    if found.frame_ref:
        with contextlib.suppress(Exception):
            await frames.destroy_ref(found.frame_ref, driver=found.driver)
        if found.driver == DEVICE_DRIVER:  # after the frame's last walk, which may add to it
            await _forget_quarantine(found.driver, found.frame_ref)
    await store.delete_prefix(machine_prefix(found.machine_id))


async def sleep_now(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> MachineSummary:
    """Sleep now, as the API answers it: the transition, then the summary the 202 carries."""
    await request_sleep(session, actor, task_id)
    found = await summary(session, task_id)
    if found is None:  # the task went between the commit and the read
        raise NoMachine("this task has no Machine yet")
    return found


async def reset_now(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> MachineSummary:
    """Reset, as the API answers it: forget the Machine, then the summary the 202 carries."""
    await reset(session, actor, task_id)
    found = await summary(session, task_id)
    if found is None:  # the task went between the commit and the read
        raise NoMachine("this task has no Machine yet")
    return found
