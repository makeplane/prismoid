# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The sweep and the reconciler (ADR-063).

The sweep is read-only and simple: it reads the rows an arm could act on and Postgres `now()`,
asks the pure `sweep_actions`, and dispatches one job per Machine. A sleep's job id carries the
sweep's tick (`machine-sleep-{id}-{epoch}~{tick}`), so a sleep that failed its upload is offered
again next tick instead of being deduped forever; two ticks' sleeps cannot both run, because the
first claims the row. An expiry's id carries the tick too, and `expire` compares-and-sets.

The reconciler lists each driver BEFORE it reads the rows (each on its own: a driver that does
not answer is skipped, never judged), asks the pure `reconcile`, destroys only what Postgres
disowns, and applies row fixes by compare-and-set - each under its own row's lock, waited on at
most `LOCK_WAIT_S` (a busy row is the next pass's). It replaces the boot reap-all, which assumed
one worker and would kill a live peer's frames. A `sleeping` row whose job is still queued or in
flight is never judged stuck. The daily orphan arm deletes objects under `machines/` that no row
references.

All three read platform-wide (a `platform=True` session): they keep every workspace's
Machines. Each write is one row's, in that row's workspace (`row_scope`). None records an event while
it holds a Machine's row lock (a `record` takes the task's row, which a step may hold) - so a fix
that takes a frame away closes its warm segment under the lock and bills it after the commit, in a
transaction of its own (a back-fill, bounded by the watermark), and records the lost unsaved
work the same way.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from dataclasses import dataclass

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.ai import frames
from prismoid.conf import settings
from prismoid.db.models.gate import Gate
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.session import row_scope, session_scope
from prismoid.operations.dispatch import get_dispatcher
from prismoid.operations.machines.common import (
    RowBusy,
    db_now,
    lease_cleared,
    lock_briefly,
    locked_machine,
    record_lost,
)
from prismoid.operations.machines.meter import settle
from prismoid.schemas.device import DEVICE_DRIVER
from prismoid.schemas.machine_caps import past_the_asleep_cap
from prismoid.schemas.machines import (
    LABEL_KIND,
    FrameInfo,
    FrameKind,
    MachineState,
    MachineView,
    RebuildReason,
    ReconcileLimits,
    ReconcilePlan,
    RowFix,
    Segment,
    SleepReason,
    SweepArm,
    SweepLimits,
    machine_label,
    persistent,
    reconcile,
    segment_seconds,
    sleep_job_id,
    step_holder,
    sweep_actions,
)
from prismoid.schemas.screen import ControlEnd, Lease

# How long a row must sit unchanged before the reconciler may judge it: a row that changed this
# recently may own a frame newer than the listing, or a hold whose attempt `running_attempts`
# has not seen yet.
FRESH_S = 60
# Past the wall clock, a throwaway (or verify) frame is its step's leftover: the listing's margin.
THROWAWAY_MARGIN_S = 600
# Every Machine's object lives under here (`schemas.machines.machine_prefix`); the orphan arm
# never sweeps anything else.
MACHINES_PREFIX = "machines/"


def view_of(row: Machine, *, control_open: bool = False) -> MachineView:
    lease = row.lease()
    return MachineView(
        id=row.id,
        state=MachineState(row.state),
        state_since=row.state_since,
        epoch=row.epoch,
        held_by=row.held_by,
        last_active_at=row.last_active_at,
        has_checkpoint=row.has_checkpoint,
        has_objects=row.has_objects,
        profile=row.profile,
        control_since=lease.since if lease is not None else None,
        control_until=lease.until if lease is not None else None,
        frame_ref=row.frame_ref,
        driver=row.driver,
        control_open=control_open,
        workspace_id=row.workspace_id,
    )


async def sweep_once(tick: dt.datetime) -> int:
    """One pass of the sweep: the idle, park-hold and retention
    arms, the lease-end arm, and - only while computer use is off - the browser-stop arm, each
    dispatched as one job per Machine under a deterministic id. With the switch off every lease
    ends: a live one `disabled`, a run-out one by its own clock. Returns how many actions the arms
    chose."""
    cfg = settings.machines
    async with session_scope(platform=True) as session:  # every workspace's Machines, read only
        now = await db_now(session)
        rows = await Machine.sweepable(session)
        # A device Machine waiting on its creator's Done is never park-hold slept (`awaits_done`).
        asked = await Gate.control_open_for(
            session, {r.task_id for r in rows if r.driver == DEVICE_DRIVER}, now
        )
        views = [view_of(r, control_open=r.task_id in asked) for r in rows]
    limits = SweepLimits(
        idle_s=cfg.idle_s,
        park_hold_s=cfg.park_hold_s,
        retention_days=cfg.retention_days,
        stuck_s=cfg.stuck_s,
    )
    on = settings.computer_use.enabled  # the kill switch
    by_id = {v.id: v for v in views}
    dispatcher = get_dispatcher()
    actions = sweep_actions(views, now, limits, computer_use_on=on)
    retiring = {a.machine_id for a in actions}
    actions += past_the_asleep_cap(views, cfg.workspace_asleep_cap, retiring=retiring)
    for action in actions:
        if action.arm in (SweepArm.IDLE, SweepArm.PARK_HOLD):
            reason = SleepReason.IDLE if action.arm == SweepArm.IDLE else SleepReason.PARK_HOLD
            await dispatcher.enqueue(
                "machine_sleep",
                sleep_job_id(action.machine_id, action.epoch, tick),
                machine_id=str(action.machine_id),
                epoch=action.epoch,
                reason=str(reason),
            )
        elif action.arm == SweepArm.RETENTION:
            # Tick-scoped too: an expire that errored is offered again next
            # tick instead of wedging retention; `expire`'s compare-and-set keeps it to one expiry.
            await dispatcher.enqueue(
                "machine_expire",
                f"machine-expire-{action.machine_id}-{action.epoch}~{tick.isoformat()}",
                machine_id=str(action.machine_id),
                epoch=action.epoch,
            )
        elif action.arm == SweepArm.RELEASE:
            # Tick-scoped as retention is; `release_asleep` compares-and-sets on the epoch.
            await dispatcher.enqueue(
                "machine_release",
                f"machine-release-{action.machine_id}-{action.epoch}~{tick.isoformat()}",
                machine_id=str(action.machine_id),
                epoch=action.epoch,
            )
        elif action.arm == SweepArm.LEASE_END and action.since is not None:
            # Tick-scoped for the same reason: an ending that errored is offered again next tick (and
            # the sweep keeps a leased Machine awake until it lands); `end_lease` compares-and-sets on
            # the lease's `since`, so it ends once. A LIVE lease is here only because the switch is
            # off: it ends `disabled`. A run-out one keeps "" - ended by its own clock (expired,
            # grace or cap).
            view = by_id.get(action.machine_id)
            live = view is not None and view.control_until is not None and view.control_until > now
            await dispatcher.enqueue(
                "machine_lease_end",
                f"machine-lease-{action.machine_id}-{action.since.isoformat()}~{tick.isoformat()}",
                machine_id=str(action.machine_id),
                since=action.since.isoformat(),
                reason=str(ControlEnd.DISABLED) if live and not on else "",
            )
        elif action.arm == SweepArm.BROWSER_STOP:
            # Once per Machine per epoch, NEVER tick-scoped: `sidecar.ctl` spawns the daemon (and
            # Chromium) when its socket is absent, so a shutdown offered every tick would start and
            # kill Chromium every 30 s. Offered only while the Machine is idle or parked, so the id
            # is never spent on a running step's browser (it is offered once the step lets go).
            view = by_id.get(action.machine_id)
            if view is not None and view.state in (MachineState.IDLE, MachineState.PARKED):
                await dispatcher.enqueue(
                    "machine_browser_stop",
                    f"machine-browser-stop-{action.machine_id}-{action.epoch}",
                    machine_id=str(action.machine_id),
                    epoch=action.epoch,
                )
        # STUCK rows are the reconciler's: it runs every minute and resolves them from the listing.
    return len(actions)


async def reconcile_once(
    sleeping_jobs: frozenset[uuid.UUID] | None = None, seeding_jobs: frozenset[uuid.UUID] | None = None
) -> ReconcilePlan:
    """One pass of the reconciler, at worker boot and every minute.

    `sleeping_jobs` names the Machines with a `machine_sleep` job still queued or in flight, and
    `seeding_jobs` the forks with a `machine_seed` one (the scheduled job asks DBOS). None means
    nobody could tell - the boot's pass runs before `DBOS.launch()` - and then every `sleeping` row
    and every seed counts as having its job: never destroy or release on uncertainty.

    It lists every driver a row names a frame on, plus this worker's: a
    deployment that switched drivers keeps judging the old one's Machines. A listing is per driver:
    a driver that raises, or that this worker does not
    know, is skipped with a warning - its frames are never destroyed and the rows naming one are
    left for the next pass. While a driver is silent only the fixes that drop or disown a frame
    pause; fixes that keep the frame (a dead-hold release, a stuck sleep back to warm) and
    re-stamps still run."""
    cfg = settings.machines
    async with session_scope(platform=True) as session:  # which providers hold our frames, read only
        drivers = await Machine.frame_drivers(session) | {settings.worker.frame_driver}
    listed, answered = await _list(drivers)  # BEFORE the rows are read (see `reconcile`)
    named = {
        parsed[0]
        for f in listed
        if f.labels.get(LABEL_KIND) == FrameKind.MACHINE and (parsed := machine_label(f)) is not None
    }
    async with session_scope(platform=True) as session:  # every workspace's Machines, read only
        now = await db_now(session)
        views = [view_of(r) for r in await Machine.for_reconcile(session, named)]
        running = frozenset(
            step_holder(str(step_id), attempt)
            for step_id, attempt in await PlanStep.running_attempts(session)
        )
    limits = ReconcileLimits(
        throwaway_max_s=settings.worker.agent_wall_clock_s + THROWAWAY_MARGIN_S,
        verify_max_s=settings.worker.agent_wall_clock_s + THROWAWAY_MARGIN_S,
        legacy_grace_s=cfg.legacy_grace_s,
        stuck_s=cfg.stuck_s,
        fresh_s=FRESH_S,
        adopt_legacy=settings.worker.instance == "",
    )
    plan = reconcile(
        listed,
        views,
        running,
        now,
        limits,
        sleeping_jobs=sleeping_jobs,
        seeding_jobs=seeding_jobs,
        answered=answered,
        asked=frozenset(drivers),
        kept=_kept(answered),
    )
    for ref in plan.skipped:  # never destroyed, never the end of the pass
        logfire.warn("reconciler: frame {ref} has labels that do not parse; left alone", ref=ref)
    destroyed = 0
    for found in plan.disowned:  # each disowned frame, once, through the driver that listed it
        try:
            await frames.destroy_ref(found.ref, driver=found.driver or None)
            destroyed += 1
        except Exception as failed:  # the next pass lists it again
            logfire.warn(
                "reconciler: frame {ref} could not be destroyed: {err}", ref=found.ref, err=str(failed)
            )
    for fix in plan.fixes:
        try:
            await _apply(fix)
        except Exception:  # one row's failure never costs the others their fix
            logfire.exception("reconciler: machine {id} could not be fixed", id=str(fix.machine_id))
    if destroyed or plan.fixes:
        logfire.info(
            "reconciler: {destroyed} frame(s) destroyed, {fixed} row(s) fixed",
            destroyed=destroyed,
            fixed=len(plan.fixes),
        )
    return plan


def _kept(drivers: frozenset[str]) -> frozenset[str]:
    """The drivers whose Machine frames are `persistent` (the device): never destroyed while a row
    owns them. Only drivers that answered are asked - one this worker does not know never listed."""
    return frozenset(d for d in drivers if persistent(frames.sleep_kind(d)))


async def _list(drivers: set[str]) -> tuple[list[FrameInfo], frozenset[str]]:
    """Each driver's listing on its own: what the drivers that answered run, and which answered. A
    driver that fails in any way (Docker's `DockerException` is neither a `RuntimeError` nor an
    `OSError`), or whose name this worker does not know, is left out loudly: a warm Machine on a
    driver that cannot answer cannot sleep either, so failing the whole pass on it would stop every
    healthy driver's hygiene for as long as it stays down."""
    listed: list[FrameInfo] = []
    answered: set[str] = set()
    for driver in sorted(drivers):
        try:
            listed += await frames.inventory([driver])
        except Exception as failed:  # the next pass asks it again
            logfire.warn(
                "reconciler: driver {driver} did not answer its listing ({err}); its frames and the "
                "Machines naming one wait for the next pass",
                driver=driver,
                err=str(failed),
            )
            continue
        answered.add(driver)
    return listed, frozenset(answered)


@dataclass(frozen=True, slots=True)
class _After:
    """What a kept frame is told once its fix has committed: its allowlist, a thaw."""

    task_id: str
    profile: str
    ref: str
    egress: tuple[int, list[str]] | None  # the version and list a released hold left it, or None
    thaw: bool
    driver: str


async def _apply(fix: RowFix) -> None:
    """One fix, compare-and-set under the row's lock. A row busy past `LOCK_WAIT_S` is skipped:
    the next pass sees it again. Records no event under the lock (a `record` takes the task's row,
    which a step may hold while it waits on this one). A fix that takes the frame away bills the
    segment a crash skipped (`metered_until → now`) only after its commit, in its own
    transaction, and - when the row claimed a live frame (warm, or a stuck sleep's) - says the
    unsaved work since the last checkpoint was lost, the same way; a stuck wake's frame held
    nothing unsaved. Its move to `asleep` or `gone` clears a control lease in the same transaction,
    and the ending is said after the commit too - `lost` when it was live. A fix
    that keeps the frame keeps the meter running - the next edge bills it - and keeps the lease. A
    lease's ending restarts the idle clock in the same transaction (`Machine.end_lease`)."""
    after: _After | None = None
    taken = TakenFrame()
    async with row_scope(Machine, fix.machine_id) as session:
        try:
            row = await lock_briefly(session, lambda: locked_machine(session, fix.machine_id))
        except RowBusy:
            return
        if row is None or _moved_since_read(row, fix):
            return  # it moved since the pass read it: the next pass judges it again
        if fix.driver is not None:
            await _restamp(session, row, fix.driver)
            return
        now = await db_now(session)
        if fix.keep_frame:
            after = fix_keeping_frame(row, fix, now)
        else:
            taken = fix_taking_frame(row, fix, now)
        await session.commit()
    if taken.segment is not None:
        await settle(taken.segment)  # never raises: a bill lost here is under-billed, never twice
    if taken.lost is not None:
        await record_lost(taken.lost)  # never raises
    if taken.cleared is not None:
        task_id, lease, at = taken.cleared
        await lease_cleared(task_id, fix.machine_id, lease, at)  # never raises
    if after is not None:
        await _tell(after)


@dataclass(frozen=True, slots=True)
class TakenFrame:
    """What a fix that took the frame away still owes after its commit."""

    segment: Segment | None = None  # the warm time a crash skipped billing
    lost: uuid.UUID | None = None  # the task whose Machine lost unsaved work
    cleared: tuple[uuid.UUID, Lease, dt.datetime] | None = None  # a control lease the move ended


def _moved_since_read(row: Machine, fix: RowFix) -> bool:
    return (
        row.epoch != fix.epoch
        or row.state != fix.seen
        or row.state_since != fix.since
        or row.held_by != fix.held_by
    )


async def _restamp(session: AsyncSession, row: Machine, driver: str) -> None:
    """A stale stamp: the frame runs where the listing found it. Nothing else moves - the frame,
    the hold, the meter stay - and the next pass judges the row on the driver it is now stamped
    with. A bookkeeping fix, logged, never an event."""
    was, row.driver = row.driver, driver
    ref = row.frame_ref
    machine_id = row.id
    await session.commit()
    logfire.warn(
        "reconciler: machine {machine}'s frame {ref} runs on {driver}, not {was}: re-stamped",
        machine=str(machine_id),
        ref=ref,
        driver=driver,
        was=was,
    )


def fix_keeping_frame(row: Machine, fix: RowFix, now: dt.datetime) -> _After | None:
    """A dead hold let go, a stuck sleep put back to warm, or a stuck wake on a persistent frame
    put back to sleep naming it. The frame stays - no meter, no destroy. What the kept frame is
    told after the commit, if anything."""
    moved = row.state != fix.to_state
    if moved:
        row.move(fix.to_state, now)  # idle or parked: a lease stays with its frame
        if fix.release_hold:
            row.last_active_at = now  # the idle clock starts at the release
    # An idle Machine's background processes reach nothing - a dead park's list, `once`
    # hosts included, goes with it (as `release` and `release_parked` narrow it) - unless
    # someone drives its browser, who keeps it.
    version: int | None = None
    if fix.release_hold:
        version = row.let_go(now)
    elif moved:
        version = row.stepless_egress(now)
    egress = (version, list(row.egress_hosts)) if version is not None else None
    if row.frame_ref and (egress is not None or fix.thaw):
        return _After(str(row.task_id), row.profile, row.frame_ref, egress, fix.thaw, row.driver)
    return None


def fix_taking_frame(row: Machine, fix: RowFix, now: dt.datetime) -> TakenFrame:
    """The frame is gone: stop the meter, drop the frame, move the row, and say what is owed."""
    seconds = segment_seconds(row.metered_until, now)
    segment = Segment(row.task_id, row.profile, seconds, at=now, driver=row.driver) if seconds > 0 else None
    row.metered_until = None  # the frame is gone: the meter stops
    had_frame = row.frame_ref is not None
    row.frame_ref = None
    lost: uuid.UUID | None = None
    if row.state != MachineState.WAKING and had_frame:
        # said after the commit, so never at the next wake too. A row that never had a
        # frame (a fork's child whose seed is still retrying) lost nothing unsaved.
        lost = row.task_id
    if fix.to_state == MachineState.GONE:
        row.gone_reason = None if lost else RebuildReason.LOST
    # `held_by` stays: a running step's attempt fails as infra and its next attempt takes over.
    lease = row.move(fix.to_state, now)
    cleared = (row.task_id, lease, now) if lease is not None else None
    return TakenFrame(segment, lost, cleared)


async def _tell(after: _After) -> None:
    """After the commit, best-effort: the list first, then the thaw (a seed or a sleep may have
    died between its freeze and its thaw), so a thawed process never reaches the old list. A push
    that fails is superseded by the next boundary's, which is newer still."""
    with contextlib.suppress(Exception):
        frame = frames.new_frame(after.task_id, driver=after.driver, profile=after.profile, name=after.ref)
        await frame.bind(start=False)  # a stopped frame has nothing frozen and reaches nothing
        if after.egress is not None:
            version, hosts = after.egress
            with contextlib.suppress(Exception):
                await frame.allow(hosts, version=version)
        if after.thaw:
            await frame.thaw()


async def orphans_once(min_age_s: int = 3600, prefix: str = MACHINES_PREFIX) -> int:
    """The daily arm: delete objects under `prefix` (every Machine's, by default) that no row
    references, once they are older than `min_age_s` (a sleep uploads before its row points at the
    object). A seed whose child's row exists is kept: the child has not claimed it yet.

    The index and the listing are read once, so each delete re-checks its key first (fix round
    2): a row may have come to reference it since (a sleep at the same epoch re-uploaded the key
    and committed), or it may have been written again since the listing. Only a key that is still
    unreferenced AND still old is deleted. `prefix` stays under `machines/`: anything else is not
    a Machine's object, and an empty one would sweep the whole snapshots bucket."""
    if not prefix.startswith(MACHINES_PREFIX):
        raise ValueError(f"the orphan arm only sweeps under {MACHINES_PREFIX!r}, not {prefix!r}")
    bucket = settings.worker.snapshot_bucket
    async with session_scope(platform=True) as session:  # every workspace's Machines, read only
        ids, keys = await Machine.object_index(session)
        now = await db_now(session)
    listed = await asyncio.to_thread(storage.list_objects, prefix, bucket)
    deleted = 0
    for key, modified in listed:
        if key in keys or (now - modified).total_seconds() < min_age_s:
            continue
        if key.endswith("seed.tar.gz") and _owner(key) in ids:
            continue  # a seed the child has not claimed yet
        if not await _still_orphaned(key, bucket, min_age_s):
            continue
        await asyncio.to_thread(storage.delete_key, key, bucket)
        deleted += 1
    if deleted:
        logfire.info("machine orphans: {n} unreferenced object(s) deleted", n=deleted)
    return deleted


async def _still_orphaned(key: str, bucket: str, min_age_s: int) -> bool:
    """Re-check one key just before its delete: no row references it now, and the object is still
    older than `min_age_s` (HEAD after the row check, so an upload that lands before the commit
    that points at it is seen as young). A key already gone needs no delete."""
    async with session_scope(platform=True) as session:  # one key, read only
        if await Machine.references(session, key):
            return False
        now = await db_now(session)
    modified = await asyncio.to_thread(storage.last_modified, key, bucket)
    return modified is not None and (now - modified).total_seconds() >= min_age_s


def _owner(key: str) -> uuid.UUID | None:
    """`machines/<machine_id>/…` → the Machine's id; None for any other shape."""
    parts = key.split("/")
    try:
        return uuid.UUID(parts[1]) if len(parts) > 2 else None
    except ValueError:
        return None
