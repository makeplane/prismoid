# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A step's life in the Machine (ADR-063): acquire → the step boundary → release.

`acquire` returns the frame a step runs in: the task's Machine for a serial step (created, bound
warm, or woken), a throwaway frame for a seat or when the rollback lever is off, or the old frame
of a park from before 1b. `release` (`handback`) hands it back. Every transition is a short `FOR UPDATE` +
compare-and-set on (state, epoch, held_by); booting and restoring run outside any transaction,
guarded by `waking`. A holder that is the same step at an older attempt is dead - attempts of one
step never overlap, and a DBOS recovery is a fresh attempt - so the newer attempt takes over.
Acquire never sleeps a Machine itself: where it needs one
asleep - a cap victim, a base Machine meeting a browser step, an airtight frame that must be born
with a list - it CLAIMS the sleep (sleep's one claim rule: the row goes `sleeping`) in the same
commit that evicts or hands back the Machine, enqueues the claimed `machine_sleep` job under a
claim-scoped id, and waits for the row to change. A `sleeping` row is bound by no step and never
skipped by its job. A crash between that commit and the enqueue leaves the row `sleeping` with no
job - the reconciler's stuck rule resolves it, the same accepted residual as Sleep now.

The caller's session: `acquire` first commits what the caller has open, so every transition here
is its own transaction and reads its own `now()`; and a transition that changes nothing ends with a
commit too, never a rollback - a rollback expires the caller's `task` and `step`, and an expired row
read outside the greenlet is an error, not a refresh. The hand-backs (`handback`) take no
session at all: they run on their own, and they record no usage - each returns the
warm segment it closed for its caller to bill. An attempt that
fails after claiming the Machine gives it back as it found it (a park it found keeps its hold), so
a step that already ended never blocks the next one. A database error inside a transition leaves
`waking` to the reconciler's stuck rule, the way `sleep` leaves `sleeping`.

Here: `acquire`'s loop, the claim under the row's lock (`_claim`) and the early boot. The claim's
plan is carried out by `wake` (bind warm, resume or rebuild) and `ready` (the step boundary); the
values they pass live in `acquired`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from collections.abc import Sequence

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai import frames
from prismoid.conf import settings
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.errors import ModelIncapable
from prismoid.operations.dispatch import get_dispatcher, machine_boot_job_key
from prismoid.operations.machines.acquired import (
    DRIVING,
    Acquired,
    AcquiredKind,
    BindPlan,
    Evict,
    Retry,
    WakePlan,
    driver_of,
)
from prismoid.operations.machines.common import db_now, record, workspace_lock
from prismoid.operations.machines.egress_push import push
from prismoid.operations.machines.ready import enqueue_sleep
from prismoid.operations.machines.sleep import claim_row
from prismoid.operations.machines.wake import bind, rebuild, wake
from prismoid.operations.machines.wake_wait import Backoff, Phase
from prismoid.schemas.device import DEVICE_DRIVER
from prismoid.schemas.events import MachineCreated
from prismoid.schemas.machines import (
    FrameKind,
    MachineProfile,
    MachineState,
    SleepReason,
    boot_holder,
    frame_labels,
    held_by_boot,
    holder_step,
    persistent,
    step_holder,
)
from prismoid.schemas.machines import widen as widen_profile
from prismoid.schemas.plan import StepSpec
from prismoid.schemas.screen import is_live

_SLEEPING_FOR_US = "waiting for a Machine to go to sleep"
# The early boot is still starting the task's Machine: the step waits for it, asking every
# second rather than backing off - it is the frame the step would otherwise have started itself.
_STARTING = "the Machine is starting"
# Where a Machine is warm and a claim may take it.
_WARM = (MachineState.IDLE, MachineState.RUNNING, MachineState.PARKED)
# Where a claim wakes it at a new epoch, if the workspace's warm cap has room.
_COLD = (MachineState.ASLEEP, MachineState.GONE)


def seat_profile(task: Task, spec: StepSpec) -> str:
    """The image a step boots on this task: its agent's, the browser's, or its format skill's (`code`);
    the one answer every step path asks, so a seat never changes between an attempt and its resume."""
    from prismoid.ai.agents.base import frame_profile_for

    output = spec.contract.output if spec.contract is not None else None
    return frame_profile_for(
        str(spec.agent),
        browser=spec.browser,
        device=driver_of(task) == DEVICE_DRIVER,
        makes=output.type if output is not None else None,
    )


async def acquire(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    *,
    profile: str,
    allow_domains: Sequence[str],
    seat: bool = False,
    resuming: bool = False,
    legacy_ref: str | None = None,
    on_phase: Phase | None = None,
) -> Acquired:
    if legacy_ref is not None:  # a park from before the Machine resumes on its own frame
        frame = frames.new_frame(str(task.id), profile=profile, attempt=step.attempt, name=legacy_ref)
        restarted = await frame.bind()  # RuntimeError when it is gone, exactly as before 1b
        try:
            await push(frame, sorted(allow_domains), hold=None)  # a legacy frame has no Machine row
        except Exception as refused:  # as before 1b: a reattached frame that cannot be allowed is torn down
            with contextlib.suppress(Exception):
                await frame.destroy()
            raise ModelIncapable(str(refused), infra=True) from refused
        return Acquired(
            frame,
            kind=AcquiredKind.LEGACY,
            label="Preparing the Machine",
            woke_from_park=restarted,  # a restart took the parked page with it
            task_id=task.id,
            profile=profile,
        )
    # The rollback lever hands NEW work a throwaway frame. A resume that reaches here is one
    # the Machine holds (the caller sends any other as `legacy_ref`): its run lives in the Machine,
    # so it binds it even with the lever pulled, and its release gives the Machine back.
    if seat or (not settings.machines.enabled and not resuming):
        return await _throwaway(task, step, profile=profile, allow_domains=allow_domains, seat=seat)

    await session.commit()  # the caller's work is its own; every transition below is ours
    holder = step_holder(str(step.id), step.attempt)
    backoff = Backoff(settings.machines.wait_s, on_phase)
    evicting: Evict | None = None
    while True:
        outcome = await _claim(session, task, step, holder, profile, evicting)
        if isinstance(outcome, BindPlan | WakePlan):
            try:
                if isinstance(outcome, BindPlan):
                    return await bind(
                        session,
                        task,
                        step,
                        outcome,
                        holder,
                        allow_domains=allow_domains,
                        resuming=resuming,
                        on_phase=on_phase,
                    )
                return await wake(
                    session,
                    task,
                    step,
                    outcome,
                    holder,
                    allow_domains=allow_domains,
                    resuming=resuming,
                    on_phase=on_phase,
                )
            except Retry as again:
                waiting = str(again)
        elif isinstance(outcome, Evict):
            # Never sleep inline in a step: the claim is committed; the job does the slow part.
            if outcome.reason == SleepReason.CAP:
                evicting = outcome  # one slot at a time: wait for this one, never evict a second
            await enqueue_sleep(outcome)
            waiting = _SLEEPING_FOR_US
        else:
            waiting = outcome
        if backoff.left() <= 0:
            raise ModelIncapable(waiting, infra=True)
        starting = waiting == _STARTING
        await backoff.wait("Starting the Machine" if starting else "Waiting for a Machine", reset=starting)


async def _room(session: AsyncSession, workspace_id: uuid.UUID, evicting: Evict | None) -> Evict | str | None:
    """The per-workspace warm cap, under the workspace's advisory lock: None when there is room,
    else the Machine to put to sleep for it, else why the step waits. `warm_count` and `lru_idle`
    fence on the session's bound workspace; `workspace_id` names the lock. The
    victim's sleep is claimed here, in the caller's transaction, so its own step can
    never re-bind it before the job runs. It counts as warm until its sleep lands, so while our
    claim still holds it the step waits for that slot rather than evicting a second Machine."""
    await workspace_lock(session, workspace_id)
    if await Machine.warm_count(session) < settings.machines.workspace_warm_cap:
        return None
    if evicting is not None:
        # Unlocked (M1): never wait on a row lock while holding the workspace's - a claim takes row, then workspace.
        pending = await Machine.current(session, evicting.machine_id)
        if (
            pending is not None
            and pending.epoch == evicting.epoch
            and pending.state == MachineState.SLEEPING
            and pending.state_since == evicting.claim.at
        ):
            return _SLEEPING_FOR_US  # our claim still holds it: its slot is on its way
    victim = await Machine.lru_idle(session)  # locked, re-read, and idle at this epoch
    if victim is None or victim.state != MachineState.IDLE:
        return "no Machine free in your workspace"
    return Evict(victim.id, victim.epoch, SleepReason.CAP, claim_row(victim, await db_now(session)))


async def _claim(
    session: AsyncSession, task: Task, step: PlanStep, holder: str, profile: str, evicting: Evict | None
) -> BindPlan | WakePlan | Evict | str:
    """The step's claim on the task's Machine, under the row's lock, in one transaction that always
    ends in a commit: the plan to bind it warm or wake it at a new epoch (its hold taken), a sleep
    claimed for an upgrade or the warm cap (`Evict`), or why the step waits."""
    now = await db_now(session)
    row = await Machine.locked_for_task(session, task.id)
    if row is None:
        return await _create(session, task, holder, profile, evicting)

    held = holder_step(row.held_by)
    mine = held is not None and held[0] == str(step.id) and held[1] <= step.attempt
    outcome: BindPlan | WakePlan | Evict | str | None = _waits(row, mine=mine)
    if outcome is None:
        wanted = widen_profile(row.profile, profile)
        if row.state in _WARM:
            outcome = _take_warm(row, wanted, holder, now)
        else:  # asleep, gone, or waking under a dead attempt of this step: wake at a new epoch
            if row.state in _COLD:
                outcome = await _room(session, task.workspace_id, evicting)
            if outcome is None:
                outcome = _take_cold(row, task, mine=mine, holder=holder, wanted=wanted, now=now)
    await session.commit()
    return outcome


def _waits(row: Machine, *, mine: bool) -> str | None:
    """Why a claim cannot take the row now, or None when it may."""
    if row.state == MachineState.SLEEPING:
        return "the Machine is going to sleep"
    # An early boot holds only while it wakes (its hand-back lets go): a resting row still naming one
    # is a boot that died and the stuck rule put to rest, and the step takes it over.
    booting = held_by_boot(row.held_by)
    if booting and row.state == MachineState.WAKING:
        return _STARTING
    if row.held_by is not None and not mine and not booting:
        return "the Machine is busy"
    return None


def _take_warm(row: Machine, wanted: str, holder: str, now: dt.datetime) -> BindPlan | Evict | str:
    """A warm row: taken under `holder` to bind, or - a base Machine meeting a browser step -
    handed back with its sleep claimed to re-image it by sleep + wake, unless someone drives it."""
    if wanted != row.profile:  # a base Machine meets a browser step: re-image it by sleep + wake
        lease = row.lease()
        if lease is not None and is_live(lease, now):
            return DRIVING
        row.held_by, row.held_since = None, None
        if row.state != MachineState.IDLE:
            row.move(MachineState.IDLE, now)
        return Evict(row.id, row.epoch, SleepReason.UPGRADE, claim_row(row, now))  # handed back claimed
    plan = BindPlan(
        row.id,
        row.epoch,
        row.frame_ref,
        row.profile,
        row.egress_version,
        list(row.egress_hosts),
        was_parked=row.state == MachineState.PARKED,
        driver=row.driver,
    )
    row.held_by, row.held_since = holder, now
    row.last_active_at = now  # a step's use is activity: retention counts from it
    row.move(MachineState.RUNNING, now)
    return plan


def _native_ref(row: Machine) -> str | None:
    """The computer a cold row still names, to resume or keep: natively asleep (asleep, its
    suspended frame still named), or a dead attempt's `waking` row on a persistent driver."""
    named = row.state == MachineState.ASLEEP or (
        row.state == MachineState.WAKING
        and row.frame_ref is not None
        and persistent(frames.sleep_kind(row.driver))
    )
    return row.frame_ref if named else None


def _take_cold(
    row: Machine, task: Task, *, mine: bool, holder: str, wanted: str, now: dt.datetime
) -> WakePlan:
    """A cold row moves to `waking` at a new epoch under `holder`: the plan to start its frame."""
    from_park = mine and row.state in _COLD
    # A dead attempt's `waking` row still carries why it was gone (M3); `wake` clears it on success.
    gone_reason = row.gone_reason if row.state in (MachineState.GONE, MachineState.WAKING) else None
    # Natively asleep: resumed when nothing about
    # it must change - the profile it was made with, on the task's driver (`driver_of`: the worker's
    # unless the task names its own - the kill switch). A kept device frame binds the same way,
    # and a dead attempt's `waking` row on a persistent driver still names it: the same again.
    native_ref = _native_ref(row)
    native_driver = row.driver if native_ref is not None else None
    native_profile = row.profile if native_ref is not None else None
    resume = native_ref is not None and wanted == row.profile and row.driver == driver_of(task)
    row.epoch += 1
    row.held_by, row.held_since = holder, now
    row.last_active_at = now  # a wake after 13 dormant days must not expire hours later
    # A persistent frame (the device) is the task's work: the row keeps naming it, and its profile,
    # until the wake lands (`wake`), so a worker that dies mid-wake leaves nothing to destroy.
    kept = native_ref is not None and persistent(frames.sleep_kind(row.driver))
    if not kept:
        row.profile = wanted
    row.frame_ref = native_ref if resume or kept else None  # a resumed computer stays the row's
    row.move(MachineState.WAKING, now)
    return WakePlan(
        row.id,
        row.epoch,
        wanted,
        row.checkpoint_object,
        row.checkpoint_sha256,
        row.browser_object,
        row.egress_version,
        list(row.egress_hosts),
        gone_reason,
        created=False,
        from_park=from_park,
        native_ref=native_ref,
        native_driver=native_driver,
        resume=resume,
        native_profile=native_profile,
    )


async def _create(
    session: AsyncSession, task: Task, holder: str, profile: str, evicting: Evict | None
) -> WakePlan | Evict | str:
    """The task's first need: a row born `waking` at epoch 1, if the cap has room."""
    busy = await _room(session, task.workspace_id, evicting)
    if busy is None and await Machine.for_task(session, task.id) is not None:
        busy = "the Machine is being made"  # made while this acquire waited for the workspace's lock
    if busy is not None:
        await session.commit()
        return busy
    return await _born(session, task, holder, profile)


async def _born(session: AsyncSession, task: Task, holder: str, profile: str) -> WakePlan:
    """The task's Machine row, born `waking` at epoch 1 under `holder` - a step's or the early
    boot's - with its `machine.created`, committed."""
    now = await db_now(session)
    row = Machine(
        workspace_id=task.workspace_id,
        task_id=task.id,
        driver=driver_of(task),
        profile=profile,
        state=MachineState.WAKING,
        state_since=now,
        last_active_at=now,
        epoch=1,
        held_by=holder,
        held_since=now,
        egress_version=0,
        egress_hosts=[],
    )
    session.add(row)
    await session.flush()
    await record(session, task.id, MachineCreated, profile=profile)
    wake = WakePlan(row.id, 1, profile, None, None, None, 0, [], None, created=True, from_park=False)
    await session.commit()
    return wake


# ── the early boot ──────────────────────────────────────────────────────────────────────────


def boots_early(task: Task) -> bool:
    """Does this task's first Machine start when its turn is picked up? Only on a driver in
    `frames.EARLY_BOOT_DRIVERS`, where every profile is one template's computer: the boot cannot
    guess a frame the first step would have to re-make (none of this edition's drivers). It reads
    the task's own driver (`driver_of`), as the frame does: a task on the device never boots,
    whatever the worker's driver, because a boot would make its frame in a person's folder ahead of
    the first step's checks."""
    cfg = settings.machines
    return cfg.enabled and cfg.boot_on_accept and driver_of(task) in frames.EARLY_BOOT_DRIVERS


def boot_profile() -> str:
    """The widest profile the task may need: a browser Machine binds every step (`widen`), where a
    base one meeting the computer agent would be slept and woken again. With computer use off no
    step gets a browser, so the boot asks for none."""
    return MachineProfile.BROWSER if settings.computer_use.enabled else MachineProfile.BASE


async def boot_early(task_id: uuid.UUID, turn: int = 1) -> None:
    """Enqueue the boot, after the caller's commit, under the task's own id - one boot per turn."""
    await get_dispatcher().enqueue("machine_boot", machine_boot_job_key(task_id, turn), task_id=str(task_id))


async def boot(task_id: uuid.UUID) -> str:
    """Start the task's first Machine before any step needs it, and leave it `idle` for
    the first step to bind warm. The row is born `waking` under the boot's own hold, so a step that
    arrives first waits for it (`_STARTING`) instead of starting a second; the frame is born with
    no list (an idle Machine reaches nothing) and the first step's boundary pushes its own.

    It never takes a Machine from anyone: a task that already has one (a step got there first),
    a full workspace (it never evicts for a guess), or a task that ended is left alone. A boot
    that fails gives the row back `gone` with nothing held, and the first step starts it as before.
    A re-run of this boot (DBOS recovery) finds its own `waking` row and creates again under the
    same frame name, which a provider's create may be keyed by. Returns what it did, for the log."""
    holder = boot_holder(task_id)
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        if task is None or task.is_terminal():
            return "ended"
        row = await Machine.locked_for_task(session, task.id)
        if row is not None:
            mine = row.state == MachineState.WAKING and row.held_by == holder and row.epoch == 1
            plan = WakePlan(
                row.id, 1, row.profile, None, None, None, 0, [], None, created=True, from_park=False
            )
            await session.commit()
            if not mine:
                return "made"
        else:
            await workspace_lock(session, task.workspace_id)
            if await Machine.warm_count(session) >= settings.machines.workspace_warm_cap:
                await session.commit()
                return "full"
            if await Machine.for_task(session, task.id) is not None:
                await session.commit()
                return "made"
            plan = await _born(session, task, holder, boot_profile())
        try:
            frame, _, _ = await rebuild(
                session,
                task,
                None,
                plan,
                holder,
                allow_domains=(),
                label="Starting the Machine",
                on_phase=None,
            )
        except ModelIncapable as failed:  # `_give_up` put the row to rest, holding nothing
            logfire.warn(
                "machine {id}: the early boot failed: {err}", id=str(plan.machine_id), err=str(failed)
            )
            return "failed"
        now = await db_now(session)
        row = await Machine.locked_for_task(session, task.id)
        if (
            row is None
            or row.epoch != plan.epoch
            or row.state != MachineState.WAKING
            or row.held_by != holder
        ):
            await session.commit()
            with contextlib.suppress(Exception):
                await frame.destroy()  # this epoch's frame, and the row no longer wants it
            return "changed"
        row.frame_ref = frame.ref
        row.driver = frame.driver
        row.gone_reason = None
        row.held_by, row.held_since = None, None
        row.move(MachineState.IDLE, now)
        row.last_active_at = now  # the idle clock starts here: an unclaimed boot sleeps like any idle Machine
        row.metered_until = now  # a new frame's segment starts
        row.egress_version = max(row.egress_version, frame.egress_version)
        row.egress_hosts = list(frame.egress_hosts)
        await session.commit()
    return "booted"


async def _throwaway(
    task: Task, step: PlanStep, *, profile: str, allow_domains: Sequence[str], seat: bool
) -> Acquired:
    frame = frames.new_frame(
        f"{task.id}-{step.id}-{step.attempt}",
        driver=task.machine_driver,
        profile=profile,
        attempt=step.attempt,
    )
    labels = frame_labels(
        FrameKind.SEAT, workspace_id=task.workspace_id, task_id=task.id, step_id=step.id, attempt=step.attempt
    )
    await asyncio.to_thread(frames.ensure_ready, profile, task.machine_driver)
    await frame.create(allow_domains=sorted(allow_domains), labels=labels)
    return Acquired(
        frame,
        kind=AcquiredKind.SEAT if seat else AcquiredKind.THROWAWAY,
        label="Starting a sandbox",
        task_id=task.id,
        profile=profile,
    )
