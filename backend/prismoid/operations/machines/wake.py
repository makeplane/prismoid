# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A claimed Machine's frame (ADR-063): bound warm, resumed, or rebuilt from its checkpoint.

`bind` takes a warm Machine's frame; `wake` starts the frame of a Machine the claim moved to
`waking` - resumed natively when nothing about it must change, else rebuilt (`rebuild`) on the
task's driver with `/work` and the browser profile restored. Booting and restoring run outside any
transaction, guarded by `waking`; each phase moves `state_since` on its own short session
(`_progressed`). A wake that fails puts the row back to rest as the claim found it (`_abandon`),
destroying only the half-made frame - never a computer its provider has not said is gone (ADR-063)."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import Sequence
from typing import NoReturn

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai import frames
from prismoid.ai.frames.driver import NamesItsDevice, NativeSleep
from prismoid.conf import settings
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.errors import ModelIncapable, PrismoidError
from prismoid.operations.machines import store
from prismoid.operations.machines.acquired import Acquired, BindPlan, Retry, WakePlan, recording_tag
from prismoid.operations.machines.common import (
    RowBusy,
    actor_of,
    bound,
    db_now,
    lease_cleared,
    lock_briefly,
    locked_machine,
    record,
    record_lost,
)
from prismoid.operations.machines.device import clear_gone, tell_grants
from prismoid.operations.machines.meter import meter_segment
from prismoid.operations.machines.ready import let_go, ready
from prismoid.operations.machines.wake_wait import Phase, create_with_room, wake_native
from prismoid.schemas.events import MachineRebuilt, MachineWoke
from prismoid.schemas.machines import (
    LABEL_EPOCH,
    LABEL_KIND,
    LABEL_MACHINE,
    LABEL_TASK,
    LABEL_WORKSPACE,
    FrameKind,
    MachineProfile,
    MachineState,
    RebuildReason,
    frame_name,
    persistent,
)
from prismoid.schemas.screen import Lease

# Said with every wake failure that leaves the computer as it was (ADR-063).
_STILL_ASLEEP = "the Machine is still asleep, and the next attempt wakes it"


async def bind(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    plan: BindPlan,
    holder: str,
    *,
    allow_domains: Sequence[str],
    resuming: bool,
    on_phase: Phase | None,
) -> Acquired:
    """A warm Machine's frame, bound for the step; a frame that vanished between steps wakes from
    the checkpoint instead (`_rewake`)."""
    frame = frames.new_frame(
        str(task.id),
        driver=plan.driver,
        profile=plan.profile,
        attempt=step.attempt,
        name=plan.ref,
        recording=recording_tag(step),
    )
    try:
        if plan.ref is None:
            raise RuntimeError("no frame recorded")
        await tell_grants(session, task, frame)
        restarted = await frame.bind()
    except RuntimeError:
        # The frame vanished between steps. Wake from the checkpoint instead.
        woken = await _rewake(session, task, plan, holder)
        return await wake(
            session,
            task,
            step,
            woken,
            holder,
            allow_domains=allow_domains,
            resuming=resuming,
            on_phase=on_phase,
        )
    except BaseException as down:  # the provider did not answer: one infra attempt, holding nothing
        await let_go(
            session,
            task,
            plan.machine_id,
            plan.epoch,
            holder,
            err=down,
            what="the Machine could not be reached",
            park=plan.was_parked,
        )
    # The row's egress is the truth: a driver found by `bind()` has synced no version.
    frame.egress_version, frame.egress_hosts = plan.egress_version, list(plan.egress_hosts)
    return await ready(
        session,
        task,
        step,
        frame,
        machine_id=plan.machine_id,
        epoch=plan.epoch,
        holder=holder,
        allow_domains=allow_domains,
        resuming=resuming,
        woke=False,
        # A restart (bind started the frame) took a parked step's page with it: its resume is told.
        woke_from_park=plan.was_parked and resuming and restarted,
        park=plan.was_parked,
        label="Preparing the Machine",
        profile=plan.profile,
    )


async def _rewake(session: AsyncSession, task: Task, plan: BindPlan, holder: str) -> WakePlan:
    """The bound row's frame is gone: wake it at a new epoch, from its checkpoint if it
    has one, else fresh. Either way the unsaved work since the last checkpoint went with the frame:
    `rebuilt{lost}` is recorded after this transition commits, outside the row's lock, and the
    wake does not say it again. It stays warm throughout, so the cap is not asked again. A control
    lease on the row ends with the frame it was driving (`lost` when it was live,
    as when the reconciler finds the frame gone), in this transaction; its ending is said after
    the commit."""
    now = await db_now(session)
    row = await Machine.locked_for_task(session, task.id)
    if (
        row is None
        or row.id != plan.machine_id
        or row.epoch != plan.epoch
        or row.held_by != holder
        or row.state != MachineState.RUNNING
    ):
        await session.commit()
        raise Retry("the Machine changed while this step was binding it")
    # The frame is gone: its unbilled tail is billed now, as the reconciler would have when it found
    # it, and the meter stops until the wake starts the new frame's. Under the row's lock, in
    # this step's own transaction - the order every Machine event here takes (row, then task).
    await meter_segment(session, row, now)
    row.metered_until = None
    row.epoch += 1
    row.frame_ref = None
    row.move(MachineState.WAKING, now)
    lease = row.end_lease(now)  # the new frame is not the browser anyone was driving
    woken = WakePlan(
        row.id,
        row.epoch,
        row.profile,
        row.checkpoint_object,
        row.checkpoint_sha256,
        row.browser_object,
        row.egress_version,
        list(row.egress_hosts),
        None,  # the loss is recorded below, once
        created=False,
        from_park=plan.was_parked,
    )
    await session.commit()
    await record_lost(task.id)  # never raises
    if lease is not None:
        await lease_cleared(task.id, woken.machine_id, lease, now)  # never raises
    return woken


async def wake(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    plan: WakePlan,
    holder: str,
    *,
    allow_domains: Sequence[str],
    resuming: bool,
    on_phase: Phase | None,
) -> Acquired:
    """The claimed `waking` Machine's frame: resumed or rebuilt, named on the row under the claim's
    compare-and-set (`_woke`), then the step's last mile (`ready`)."""
    label = "Starting the Machine" if plan.created else "Waking the Machine"
    frame, rebuilt, browser_restored = await _wake_frame(
        session, task, step, plan, holder, allow_domains=allow_domains, label=label, on_phase=on_phase
    )
    woke_from_park = plan.from_park and resuming  # a fresh attempt is not a park coming back
    await _woke(
        session,
        task,
        plan,
        holder,
        frame,
        rebuilt=rebuilt,
        browser_restored=browser_restored,
        woke_from_park=woke_from_park,
    )
    await _destroy_replaced(plan, frame)
    return await ready(
        session,
        task,
        step,
        frame,
        machine_id=plan.machine_id,
        epoch=plan.epoch,
        holder=holder,
        allow_domains=allow_domains,
        resuming=resuming,
        woke=not plan.created,
        woke_from_park=woke_from_park,
        park=plan.from_park,
        label=label,
        profile=plan.profile,
    )


async def _wake_frame(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    plan: WakePlan,
    holder: str,
    *,
    allow_domains: Sequence[str],
    label: str,
    on_phase: Phase | None,
) -> tuple[frames.FrameDriver, str | None, bool]:
    """The woken frame, what it was rebuilt from and whether the browser profile came back: the
    native computer resumed when the claim planned it, else the archive path."""
    frame: frames.FrameDriver | None = None
    rebuilt: str | None = plan.gone_reason
    browser_restored = True
    if plan.resume:
        frame = await _resume(session, task, step, plan, holder, label=label, on_phase=on_phase)
    if frame is None:  # an archive Machine, a native one that cannot be resumed, or a forgotten device frame
        frame, rebuilt, browser_restored = await rebuild(
            session,
            task,
            step,
            plan,
            holder,
            allow_domains=allow_domains,
            label=label,
            on_phase=on_phase,
            say_label=not plan.resume,  # the phase was said once already
            # A native computer this wake replaces without asking its provider (another profile,
            # another driver) is not gone: a rebuild that fails names it again (ADR-063). One `_resume`
            # gave up on was gone, or was never a native computer.
            keep_native=plan.native_ref is not None and not plan.resume,
        )
    return frame, rebuilt, browser_restored


async def _woke(
    session: AsyncSession,
    task: Task,
    plan: WakePlan,
    holder: str,
    frame: frames.FrameDriver,
    *,
    rebuilt: str | None,
    browser_restored: bool,
    woke_from_park: bool,
) -> None:
    """The wake lands: under the row's lock, a row still this wake's names the new frame and goes
    `running` with its events, committed. A row that changed fails the attempt as infra, its frame
    destroyed unless it is persistent."""
    now = await db_now(session)
    row = await Machine.locked_for_task(session, task.id)
    if row is None or row.epoch != plan.epoch or row.state != MachineState.WAKING or row.held_by != holder:
        await session.commit()
        if not persistent(frame.sleep_kind):  # a persistent frame is the task's, whichever epoch woke it
            with contextlib.suppress(Exception):
                await frame.destroy()  # this epoch's frame, and the row no longer wants it
        raise ModelIncapable("the Machine changed while it woke", infra=True)
    _name_frame(row, plan, frame, rebuilt, now)
    if not plan.created:
        await record(
            session,
            task.id,
            MachineWoke,
            actor=await actor_of(session, task.turn_actor_id),
            turn=task.turn,
            profile=plan.profile,
            browser_restored=browser_restored,
            from_park=woke_from_park,
        )
    if rebuilt:
        await record(
            session,
            task.id,
            MachineRebuilt,
            reason=str(rebuilt),
            retention_days=settings.machines.retention_days if rebuilt == RebuildReason.EXPIRED else None,
        )
    await session.commit()


def _name_frame(
    row: Machine, plan: WakePlan, frame: frames.FrameDriver, rebuilt: str | None, now: dt.datetime
) -> None:
    """The locked row names the woken frame and goes `running`; a lost checkpoint is forgotten."""
    row.frame_ref = frame.ref
    row.profile = plan.profile  # already so, unless `_claim` kept a persistent row's until now
    # The driver that made this frame (ADR-063): the door, the meter and the next bind follow it.
    row.driver = frame.driver
    if isinstance(frame, NamesItsDevice) and frame.device_name:  # the computer it runs on
        row.device_name = frame.device_name
    row.gone_reason = None
    row.move(MachineState.RUNNING, now)
    row.metered_until = now  # a new frame's segment starts
    if rebuilt == RebuildReason.LOST:
        row.checkpoint_object = row.checkpoint_sha256 = None
        row.checkpoint_bytes = row.checkpoint_at = None


async def _destroy_replaced(plan: WakePlan, frame: frames.FrameDriver) -> None:
    """After the wake's commit: the native computer it did not resume, best-effort."""
    if plan.native_ref is not None and plan.native_ref != frame.ref:
        # The computer this wake did not resume (another profile, another driver, or already gone):
        # the row names the new frame now, so nothing else owns it (ADR-063). A
        # persistent frame holds the task's work: it is left for the task's end of life.
        try:
            if not persistent(frames.sleep_kind(plan.native_driver)):
                await frames.destroy_ref(plan.native_ref, driver=plan.native_driver)
        except Exception as failed:  # the row no longer names it: the reconciler's disowned rule takes it
            logfire.warn(
                "machine {id}: the computer it left could not be destroyed: {err}",
                id=str(plan.machine_id),
                err=str(failed),
            )


async def _resume(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    plan: WakePlan,
    holder: str,
    *,
    label: str,
    on_phase: Phase | None,
) -> frames.FrameDriver | None:
    """A natively asleep Machine wakes with its memory (ADR-063): its suspended
    frame resumed and bound, the row's egress taken as the truth (as `bind` takes it); `ready`'s
    boundary then pushes the step's list - a woken computer reaches nothing new until `allow`. None
    when the provider says the frame is gone: the caller rebuilds from the checkpoint. A Wake the
    provider does not answer is asked again inside `wake_retry_s` (`wake_native`). Any other failure
    - a refusal, the budget spent, a cancellation, the phase publisher - is one infra attempt that
    leaves the computer asleep and the row naming it, never destroyed (Q20: Prismoid never destroys a
    computer its provider has not said is gone; this replaced the Q5 interim's destroy)."""
    if plan.native_ref is None:
        raise PrismoidError(f"machine {plan.machine_id}: a native wake was planned without a native ref")
    frame = frames.new_frame(
        str(task.id),
        driver=plan.native_driver,
        profile=plan.profile,
        attempt=step.attempt,
        name=plan.native_ref,
        recording=recording_tag(step),
    )
    try:
        if on_phase is not None:
            await on_phase(label)
        await tell_grants(session, task, frame)
        if persistent(frame.sleep_kind):
            # A device frame was kept, not archived: the wake binds it.
            # A helper that never heard of it makes a fresh one of the same kind; one that is down
            # is the infra attempt below, the row left asleep naming it.
            if not await bound(frame):
                return None
        elif frame.sleep_kind != "native" or not isinstance(frame, NativeSleep):
            return None  # the driver's own declaration (ADR-063), not the row's: never asked to resume
        elif not await wake_native(frame, plan.machine_id, on_phase):
            # The provider lost it (a host, or the memory window): the checkpoint rebuilds it. Only
            # `resume` and `bind` say so - a RuntimeError from the phase publisher does not.
            return None
    except BaseException as down:
        await _leave_asleep(session, task, plan, holder, down, "the Machine could not wake")
    # The row's egress is the truth (as `bind` takes it): a driver found by `bind()` has synced none.
    frame.egress_version, frame.egress_hosts = plan.egress_version, list(plan.egress_hosts)
    await _progressed(plan, holder)
    return frame


async def rebuild(
    session: AsyncSession,
    task: Task,
    step: PlanStep | None,
    plan: WakePlan,
    holder: str,
    *,
    allow_domains: Sequence[str],
    label: str,
    on_phase: Phase | None,
    say_label: bool = True,
    keep_native: bool = False,
) -> tuple[frames.FrameDriver, str | None, bool]:
    """The archive path: a new frame on the task's driver (`driver_of`), born with the step's list,
    `/work` and the browser profile restored from the row's objects. Returns the frame, what it was
    rebuilt from (`gone_reason`, or `lost` when the checkpoint was) and whether the browser profile
    came back. A failure destroys only the half-made frame; with `keep_native` the row names the
    native computer it would have replaced again (ADR-063). `step` is None for the early boot,
    born with no list and recording nothing: the first step's bind brings both. On a persistent
    driver a wake that names no frame (from `gone`: a Reset, or a frame lost) first destroys any
    frame of this Machine's: the helper would re-create over its folder, undoing a Reset whose
    `cleanup` has not run yet."""
    frame = frames.new_frame(
        str(task.id),
        driver=task.machine_driver,
        profile=plan.profile,
        attempt=step.attempt if step is not None else 1,
        name=frame_name(plan.machine_id, plan.epoch),
        recording=recording_tag(step) if step is not None else None,
    )
    labels = {
        LABEL_KIND: str(FrameKind.MACHINE),
        LABEL_WORKSPACE: str(task.workspace_id),
        LABEL_MACHINE: str(plan.machine_id),
        LABEL_EPOCH: str(plan.epoch),
        LABEL_TASK: str(task.id),
    }
    try:
        if on_phase is not None and say_label:
            await on_phase(label)
        await asyncio.to_thread(frames.ensure_ready, plan.profile, task.machine_driver)
        if persistent(frame.sleep_kind) and plan.native_ref is None and not plan.created:
            await clear_gone(plan.machine_id, frame.driver)  # a helper that is away fails the wake here
        await tell_grants(session, task, frame)
        # Born with the step's list: a sandbar sandbox born with no domains is airtight and can
        # never be widened, so a frame that needs egress must be born with it.
        await create_with_room(
            frame,
            plan.machine_id,
            # The early boot is a guess made ahead of the step: on a full host it gives way at once.
            wait_s=settings.machines.wake_retry_s if step is not None else 0,
            on_phase=on_phase,
            allow_domains=sorted(allow_domains),
            labels=labels,
            egress_version=plan.egress_version + 1,
        )
        await _progressed(plan, holder)
    except BaseException as boot:
        await _give_up(
            session,
            task,
            plan,
            holder,
            frame,
            boot,
            "the Machine could not start",
            park=plan.from_park,
            native=keep_native,
        )
    rebuilt, browser_restored = await _restore(session, task, plan, holder, frame, keep_native=keep_native)
    return frame, rebuilt, browser_restored


async def _restore(
    session: AsyncSession,
    task: Task,
    plan: WakePlan,
    holder: str,
    frame: frames.FrameDriver,
    *,
    keep_native: bool,
) -> tuple[str | None, bool]:
    """`/work` and the browser profile, restored into the new frame from the row's objects: what
    the frame was rebuilt from (`gone_reason`, or `lost` when the checkpoint was) and whether the
    browser profile came back. Storage that cannot be read gives the wake up (`_give_up`)."""
    rebuilt: str | None = plan.gone_reason
    browser_restored = True
    try:
        if plan.checkpoint_object and plan.checkpoint_sha256:
            try:
                await store.load_work(frame, plan.checkpoint_object, plan.checkpoint_sha256)
            except store.CheckpointLost as lost:
                logfire.warn(
                    "machine {id}: checkpoint lost ({why}); starting fresh",
                    id=str(plan.machine_id),
                    why=str(lost),
                )
                rebuilt = RebuildReason.LOST
            await _progressed(plan, holder)
        if plan.profile == MachineProfile.BROWSER and plan.browser_object:
            try:
                await store.load_browser(frame, plan.browser_object)
            except store.CheckpointLost:
                browser_restored = False
    except BaseException as down:
        # S3 unreachable (not a lost object): the checkpoint may be fine - keep it, fail this
        # attempt as infra, and let the ladder retry.
        await _give_up(
            session,
            task,
            plan,
            holder,
            frame,
            down,
            "the Machine's checkpoint could not be read",
            park=plan.from_park,
            native=keep_native,
        )
    return rebuilt, browser_restored


async def _progressed(plan: WakePlan, holder: str) -> None:
    """A wake that finished a phase - its frame up, `/work` restored - moves `state_since`, so the
    reconciler reads a wake's age as its time without progress, never as the size of its
    checkpoint. On its own short session, waiting on the row at most `LOCK_WAIT_S` (the step's
    session must stay clean for `_give_up`): a busy row records nothing, and the next phase tries
    again. A row that is no longer this wake's ends the wake now - the final compare-and-set would
    only throw the restore away."""
    async with row_scope(Machine, plan.machine_id) as session:
        try:
            row = await lock_briefly(session, lambda: locked_machine(session, plan.machine_id))
        except RowBusy:
            return
        if (
            row is None
            or row.epoch != plan.epoch
            or row.state != MachineState.WAKING
            or row.held_by != holder
        ):
            raise ModelIncapable("the Machine changed while it woke", infra=True)
        row.state_since = await db_now(session)
        await session.commit()


async def _give_up(
    session: AsyncSession,
    task: Task,
    plan: WakePlan,
    holder: str,
    frame: frames.FrameDriver,
    err: BaseException,
    what: str,
    *,
    park: bool,
    native: bool = False,
) -> NoReturn:
    """A rebuild failed (or was cancelled) before the row named its new frame: back to rest, the
    half-made frame destroyed - only ever the frame this wake made (`native`: see `_abandon`). A
    `persistent` frame is never destroyed: the device names a Machine's frame the same at every
    wake, so the frame this wake made is the task's own. An error is one infra attempt; a
    cancellation stays a cancellation."""
    await _abandon(session, task, plan, holder, park=park, native=native)
    if not persistent(frame.sleep_kind):
        with contextlib.suppress(Exception):
            await frame.destroy()
    if not isinstance(err, Exception):
        raise err
    raise ModelIncapable(f"{what}: {err}", infra=True) from err


async def _leave_asleep(
    session: AsyncSession, task: Task, plan: WakePlan, holder: str, err: BaseException, what: str
) -> NoReturn:
    """A resume failed (or was cancelled) without the provider saying the computer is gone (ADR-063):
    the row goes back to asleep naming it, and nothing is destroyed - its memory, its logins and its
    unsaved `/work` wait for the next attempt. An error is one infra attempt, its reason the
    person's to read; a cancellation stays a cancellation."""
    await _abandon(session, task, plan, holder, park=plan.from_park, native=True)
    if not isinstance(err, Exception):
        raise err
    raise ModelIncapable(f"{what}: {err} - {_STILL_ASLEEP}", infra=True) from err


async def _abandon(
    session: AsyncSession, task: Task, plan: WakePlan, holder: str, *, park: bool, native: bool = False
) -> None:
    """Back to asleep (or gone), keeping the checkpoint for the next attempt and the reason it is
    gone for the next wake's event. With `native`, a natively asleep row whose computer the provider
    has not said is gone goes back exactly as the claim found it: asleep, naming that computer, with
    the profile it was made with (ADR-063) - the reconciler's ownership rule keeps it. A park the wake
    found keeps its hold; any other attempt holds nothing once it ends, so the next step is never
    blocked behind it. A control lease on the row (none should reach a waking row: a claim needs a
    frame, and a rewake ends one) is cleared with the move, the idle clock restarted, and its ending
    said after the commit."""
    row = await Machine.locked_for_task(session, task.id)
    cleared: tuple[Lease, dt.datetime] | None = None
    if (
        row is not None
        and row.epoch == plan.epoch
        and row.state == MachineState.WAKING
        and row.held_by == holder
    ):
        if not park:
            row.held_by, row.held_since = None, None
        if native and plan.native_ref is not None:
            row.frame_ref = plan.native_ref
            row.profile = plan.native_profile or row.profile
            rest = MachineState.ASLEEP
        else:
            row.frame_ref = None
            if row.checkpoint_object is None:
                row.gone_reason = plan.gone_reason
            rest = MachineState.ASLEEP if row.checkpoint_object else MachineState.GONE
        now = await db_now(session)
        lease = row.move(rest, now)
        if lease is not None:
            cleared = (lease, now)
    await session.commit()
    if cleared is not None:
        await lease_cleared(task.id, plan.machine_id, *cleared)  # never raises
