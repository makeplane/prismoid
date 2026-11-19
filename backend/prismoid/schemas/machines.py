# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Machine - its values, and its pure decisions.

A Machine is a task's remembered computer: one row, many frames over its life (one per wake,
numbered by its epoch). Nothing in this module does I/O or reads a clock: `operations/machines`
feeds it rows and Postgres `now()`, the way `schemas/routines.py` does fire math.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal

from prismoid.schemas.machine_meter import (
    Resources,
    Segment,
    parse_prices,
    parse_resources,
    parse_rfc3339,
    segment_seconds,
)
from prismoid.schemas.machine_profile import MachineProfile, widen
from prismoid.schemas.screen import LeaseView


class MachineState(StrEnum):
    WAKING = "waking"  # a frame is being created and restored for a step
    RUNNING = "running"  # a holder (a step, or a fork's seed) has it
    PARKED = "parked"  # its step waits on a gate, inside the park hold
    IDLE = "idle"  # warm, nobody holds it; the idle clock runs
    # quiescing, uploading its checkpoint, destroying the frame; or a seed being written
    SLEEPING = "sleeping"
    # checkpoint stored; no frame - or, on a native driver, a suspended one its `frame_ref` still
    # names ("natively asleep")
    ASLEEP = "asleep"
    GONE = "gone"  # no frame, no checkpoint; the next step builds fresh


WARM_STATES: Final = frozenset(
    {MachineState.WAKING, MachineState.RUNNING, MachineState.PARKED, MachineState.IDLE, MachineState.SLEEPING}
)
# A row in one of these claims a live frame the reconciler must find.
FRAME_STATES: Final = frozenset({MachineState.RUNNING, MachineState.PARKED, MachineState.IDLE})


class SleepReason(StrEnum):
    IDLE = "idle"
    PARK_HOLD = "park_hold"
    CAP = "cap"
    MANUAL = "manual"
    UPGRADE = "upgrade"


class RebuildReason(StrEnum):
    EXPIRED = "expired"
    LOST = "lost"
    OVERSIZE = "oversize"


class FrameDriverName(StrEnum):
    """Who makes a Machine's frames: the worker's `frame_driver` setting, or a task's own
    (`tasks.machine_driver`). `conf` sits beside `schemas` and spells these as plain strings."""

    DOCKER = "docker"
    SANDBAR = "sandbar"
    DEVICE = "device"


class FrameKind(StrEnum):
    MACHINE = "machine"
    SEAT = "seat"
    VERIFY = "verify"
    QA = "qa"
    FIGURES = "figures"
    SELFTEST = "selftest"


# Frame labels - every frame wears them from 1b on, so the reconciler can judge it.
LABEL_KIND: Final = "plane.kind"
LABEL_WORKSPACE: Final = "plane.workspace"
LABEL_MACHINE: Final = "plane.machine"
LABEL_EPOCH: Final = "plane.epoch"
LABEL_TASK: Final = "plane.task"
LABEL_STEP: Final = "plane.step"
LABEL_ATTEMPT: Final = "plane.attempt"
LABEL_INSTANCE: Final = "plane.instance"  # the dev instance (settings.worker.instance); "" = default


def step_holder(step_id: str, attempt: int) -> str:
    return f"step:{step_id}:{attempt}"


def seed_holder(child_machine_id: uuid.UUID) -> str:
    return f"seed:{child_machine_id}"


def persistent(sleep_kind: str) -> bool:
    """A frame that outlives every sleep, wake, race and failed attempt:
    a driver that never archives (`sleep_kind = "none"`) keeps the task's work in the frame itself,
    and names a Machine's frame the same at every wake. The invariant: a persistent frame is
    destroyed only when no row exists for it (`forget`, the reconciler), by a Reset's `cleanup`
    (retried until the helper answers) or just before a wake that names no frame makes one
    (`_rebuild`), and a row that names none (`gone`, a stuck wake from `gone`) is never given one.

    It assumes a persistent driver names a Machine's frame the same at every wake (the helper's
    `device-{task_id}`): a wake that leaves behind a frame with another name (`_wake`'s destroy of
    the native ref) does not destroy it, so a driver that renamed its frames would leak them."""
    return sleep_kind == "none"


def held_by_seed(held_by: str | None) -> bool:
    """Is this hold a fork's seed's (`seed_holder`'s shape)? A seed freezes the frame it holds."""
    return held_by is not None and held_by.startswith("seed:")


def boot_holder(task_id: uuid.UUID) -> str:
    """The early boot's hold: a task's first Machine, started when its turn is picked up."""
    return f"boot:{task_id}"


def held_by_boot(held_by: str | None) -> bool:
    return held_by is not None and held_by.startswith("boot:")


def holder_step(held_by: str | None) -> tuple[str, int] | None:
    """`step:<id>:<attempt>` → (id, attempt); anything else (a seed, a boot, nobody) → None."""
    if not held_by or not held_by.startswith("step:"):
        return None
    _, step_id, attempt = held_by.split(":", 2)
    return step_id, int(attempt)


def machine_prefix(machine_id: uuid.UUID) -> str:
    return f"machines/{machine_id}/"


def checkpoint_key(machine_id: uuid.UUID, epoch: int) -> str:
    return f"machines/{machine_id}/{epoch}.tar.gz"


def browser_key(machine_id: uuid.UUID, epoch: int) -> str:
    return f"machines/{machine_id}/{epoch}.browser.enc"


def seed_key(machine_id: uuid.UUID) -> str:
    return f"machines/{machine_id}/seed.tar.gz"


def frame_name(machine_id: uuid.UUID, epoch: int) -> str:
    return f"machine-{machine_id}-{epoch}"


def frame_labels(
    kind: FrameKind,
    *,
    workspace_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    step_id: uuid.UUID | None = None,
    attempt: int | None = None,
) -> dict[str, str]:
    """What a throwaway frame wears, so the reconciler can judge it: its kind, and the
    ids it has. A frame whose step attempt is running is kept; any other is kept while young."""
    labels = {LABEL_KIND: str(kind)}
    for label, value in (
        (LABEL_WORKSPACE, workspace_id),
        (LABEL_TASK, task_id),
        (LABEL_STEP, step_id),
        (LABEL_ATTEMPT, attempt),
    ):
        if value is not None:
            labels[label] = str(value)
    return labels


SLEEP_JOB_PREFIX: Final = "machine-sleep-"


def sleep_job_id(machine_id: uuid.UUID, epoch: int, scope: dt.datetime) -> str:
    """A `machine_sleep` job's id, scoped to one claim or one sweep tick (`scope`): a sleep that
    failed back to warm never uses up the id the next one needs - DBOS never re-runs a finished id."""
    return f"{SLEEP_JOB_PREFIX}{machine_id}-{epoch}~{scope.isoformat()}"


def sleep_job_machine(workflow_id: str) -> uuid.UUID | None:
    """The Machine a `machine_sleep` job id names; None for any other id."""
    if not workflow_id.startswith(SLEEP_JOB_PREFIX):
        return None
    try:
        return uuid.UUID(workflow_id[len(SLEEP_JOB_PREFIX) :][:36])
    except ValueError:
        return None


# ── views: what the pure decisions read ───────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class MachineView:
    """A row as the pure decisions see it. `operations/machines` builds it from the ORM row;
    the row itself never reaches this module (ORM rows never cross into values). The lease's two
    times are None when nobody holds a control lease (ADR-065)."""

    id: uuid.UUID
    state: MachineState
    state_since: dt.datetime
    epoch: int
    held_by: str | None
    last_active_at: dt.datetime
    has_checkpoint: bool  # checkpoint_object is set
    has_objects: bool  # any stored object: checkpoint, browser archive, or a seed
    profile: str = MachineProfile.BASE
    control_since: dt.datetime | None = None
    control_until: dt.datetime | None = None
    frame_ref: str | None = None  # the frame the row names; a native computer is owned by it (ADR-063)
    driver: str = ""  # the driver that frame is on: a ref on two drivers is two frames
    workspace_id: uuid.UUID | None = None
    control_open: bool = False  # its task has an open CONTROL gate (the sweep reads it; `awaits_done`)


def awaits_done(row: MachineView) -> bool:
    """A device Machine whose task has an open CONTROL gate: the person
    is doing what the agent asked in its window on their own computer, and only their Done or the
    gate's own TTL (the away limit, `device_gates.control_wait_s`) ends the wait. The park hold never
    sleeps it - a sleep would stop what the page left running. A cloud Machine never awaits Done:
    there the gate times out before the park hold (`Settings._control_wait_under_park_hold`)."""
    return row.driver == FrameDriverName.DEVICE and row.control_open


# ── the sweep ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SweepLimits:
    idle_s: int
    park_hold_s: int
    retention_days: int
    stuck_s: int


class SweepArm(StrEnum):
    IDLE = "idle"
    PARK_HOLD = "park_hold"
    RETENTION = "retention"
    STUCK = "stuck"
    LEASE_END = "lease_end"  # a control lease whose clock ran out (or any, with computer use off)
    BROWSER_STOP = "browser_stop"  # computer use is off: stop a warm browser Machine's Chromium
    RELEASE = "release"  # a workspace past its asleep cap: the oldest gives up its native computer


@dataclass(frozen=True, slots=True)
class SweepAction:
    arm: SweepArm
    machine_id: uuid.UUID
    epoch: int
    since: dt.datetime | None = None  # LEASE_END only: the lease's identity, which the job compares


def sweep_actions(
    rows: Sequence[MachineView],
    now: dt.datetime,
    limits: SweepLimits,
    *,
    computer_use_on: bool = True,
) -> list[SweepAction]:
    """Which Machines the sweep dispatches work for, in row order.

    The idle clock counts from `last_active_at` (a fork's seed hold moves `state_since` but not
    the activity); the park hold and the stuck test count from `state_since`.

    The control lease (ADR-065). A lease whose clock has run out - `until <= now`,
    so the edge itself is expired - is ended (`LEASE_END`, named by its `since`, which the job
    compares-and-sets on); with computer use off, every lease is. A Machine with ANY lease on its
    row never gets the idle or the park-hold sleep: a live lease is someone driving, and an ending
    moves `last_active_at`, so the idle clock restarts once the lease-end job has run, one tick
    later. With computer use off, every warm `browser` Machine also stops its Chromium
    (`BROWSER_STOP`, the kill switch's job, once per epoch) - unless this pass puts it to sleep
    (`IDLE` or `PARK_HOLD`): the sleep's checkpoint shuts the daemon down itself, and a stop racing
    it could respawn Chromium over the profile the archive is reading. A device
    Machine waiting on its creator's Done (`awaits_done`) never gets the park-hold sleep."""
    retention = dt.timedelta(days=limits.retention_days)
    out: list[SweepAction] = []
    for row in rows:
        since, until = row.control_since, row.control_until
        leased = since is not None
        if since is not None and until is not None and (until <= now or not computer_use_on):
            out.append(SweepAction(SweepArm.LEASE_END, row.id, row.epoch, since=since))
        in_state = (now - row.state_since).total_seconds()
        sleep: SweepArm | None = None
        if (
            row.state == MachineState.IDLE
            and not leased
            and (now - row.last_active_at).total_seconds() > limits.idle_s
        ):
            sleep = SweepArm.IDLE
        elif (
            row.state == MachineState.PARKED
            and not leased
            and not awaits_done(row)
            and in_state > limits.park_hold_s
        ):
            sleep = SweepArm.PARK_HOLD
        if (
            sleep is None
            and not computer_use_on
            and row.profile == MachineProfile.BROWSER
            and row.state in FRAME_STATES
        ):
            out.append(SweepAction(SweepArm.BROWSER_STOP, row.id, row.epoch))
        if sleep is not None:
            out.append(SweepAction(sleep, row.id, row.epoch))
        elif (
            row.state in (MachineState.ASLEEP, MachineState.GONE)
            and row.has_objects
            and now - row.last_active_at > retention
        ):
            out.append(SweepAction(SweepArm.RETENTION, row.id, row.epoch))
        elif row.state in (MachineState.WAKING, MachineState.SLEEPING) and in_state > limits.stuck_s:
            out.append(SweepAction(SweepArm.STUCK, row.id, row.epoch))
    return out


# ── the reconciler ────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class FrameInfo:
    """One frame the provider lists (`FrameDriver.inventory()`), running or not. A frame a Docker or
    host restart stopped is listed like any other: its processes are gone, its files are not, and a
    bind starts it again (`FrameDriver.bind`), so the reconciler judges it as it would a running
    frame. One whose provider has lost it (sandbar's record that outlived its container) is the next
    bind's or sleep's to find gone; once its row moves on, the reconciler destroys it as disowned."""

    ref: str
    labels: Mapping[str, str]
    created_at: dt.datetime
    driver: str = ""  # the driver that listed it (`frames.inventory`); "" means this worker's


@dataclass(frozen=True, slots=True)
class ReconcileLimits:
    # agent_wall_clock_s + 600: the longest any step's work may run - a throwaway frame's life, and
    # how long a wake or a seed's hold may show no progress under a job that still reads as running
    throwaway_max_s: int
    verify_max_s: int  # the verify timeout + 600
    legacy_grace_s: int
    stuck_s: int
    fresh_s: int  # a row that changed this recently may own a frame newer than the listing
    adopt_legacy: bool  # sweep unlabelled pre-1b frames (the default dev instance only)


@dataclass(frozen=True, slots=True)
class RowFix:
    """One row the reconciler corrects, by compare-and-set on the row exactly as the pass read it:
    its `epoch`, the state it was `seen` in and that state's `since`, and its `held_by`. A row that
    moved after the read - a wake that made progress or finished, a step that bound it, a hold
    taken over - is the next pass's to judge, never fixed on a stale judgment.

    An ordinary fix (a lost frame, a stuck wake, a stuck sleep whose frame is gone) moves the row
    to rest, clears its `frame_ref` and keeps its hold. A `keep_frame` fix leaves the frame where
    it is and moves to `to_state` only if that differs: a released dead hold (`release_hold`),
    or a stuck sleep put back to warm. `thaw` thaws a frame that a dead seed or a dead sleep may
    have frozen.

    A `driver` fix re-stamps the row's driver to the one whose listing found its frame
    and changes nothing else. Its compare-and-set needs no more than the others':
    `frame_ref` and `driver` are otherwise only written with a move of the state, so an unchanged
    `since` is an unchanged frame, and two passes that both re-stamp write the same driver."""

    machine_id: uuid.UUID
    epoch: int
    seen: MachineState
    since: dt.datetime
    held_by: str | None
    to_state: MachineState
    keep_frame: bool = False
    release_hold: bool = False
    thaw: bool = False
    driver: str | None = None  # re-stamp: the driver whose listing found the row's frame

    @classmethod
    def of(
        cls,
        row: MachineView,
        to_state: MachineState,
        *,
        keep_frame: bool = False,
        release_hold: bool = False,
        thaw: bool = False,
        driver: str | None = None,
    ) -> RowFix:
        """The fix of `row` as this pass read it."""
        return cls(
            row.id,
            row.epoch,
            row.state,
            row.state_since,
            row.held_by,
            to_state,
            keep_frame,
            release_hold,
            thaw,
            driver,
        )


@dataclass(frozen=True, slots=True)
class ReconcilePlan:
    disowned: tuple[FrameInfo, ...]  # each listed frame to destroy, on the driver that listed it
    fixes: tuple[RowFix, ...]
    skipped: tuple[str, ...] = ()  # frames whose labels do not parse: never destroyed, logged

    @property
    def destroy(self) -> tuple[str, ...]:
        """The disowned frames' refs (a ref on two drivers is two frames, so it can repeat)."""
        return tuple(f.ref for f in self.disowned)


# The name every unnamed pre-1b frame wore (`frame-{task_id}`, so `frame-{task}-{step}-{attempt}`,
# `frame-verify-…`, `frame-qa-…`, `frame-figures-…`, `frame-selftest-…`), and the only one the
# removed boot reaps ever matched. An unlabelled container with any other name is not ours: the
# daemon lists every container on the host, a `qa-postgres` included.
LEGACY_PREFIXES: Final = ("frame-",)


def reconcile(
    frames: Sequence[FrameInfo],
    rows: Sequence[MachineView],
    running_attempts: frozenset[str],
    now: dt.datetime,
    limits: ReconcileLimits,
    *,
    sleeping_jobs: frozenset[uuid.UUID] | None = None,
    seeding_jobs: frozenset[uuid.UUID] | None = None,
    answered: frozenset[str] | None = None,
    asked: frozenset[str] | None = None,
    kept: frozenset[str] = frozenset(),
) -> ReconcilePlan:
    """Destroy only what Postgres disowns; fix rows that claim a frame the provider lacks.

    `frames` must be listed BEFORE `rows` are read: a frame created after the listing is simply
    absent (never destroyed), and a row that changed within `fresh_s` is never "fixed", because
    its frame may postdate the listing. Fixes are applied by compare-and-set on the row as read
    (`RowFix`) in operations, so a transition racing this plan wins.

    `sleeping_jobs` names the Machines with a `machine_sleep` job still queued or in flight: such
    a `sleeping` row is never stuck - its job, or DBOS recovery, finishes it.
    `seeding_jobs` names the forks (child Machines) whose `machine_seed` job
    is: such a seed's hold on its parent is live. None - the default for either - means the caller
    cannot tell, and then every sleeping row and every seed has its job: never destroy or release
    on uncertainty. A frame whose labels do not parse is skipped, never destroyed.

    A listing is per driver: `answered` names the drivers whose listing answered
    this pass, and `frames` holds only theirs, so nothing is ever destroyed on a driver that did
    not answer. A row naming a frame on a driver that did not answer is left as it is - nobody
    looked - and judged again next pass; a row naming no frame is judged as ever. None means every
    driver a row names answered. `asked` names the drivers the pass asked (None: `answered` plus the
    drivers the rows name). While any asked driver is silent, a frame-bearing row gets no fix that
    drops its frame (lost, fallback), whichever driver it names, though a fix that keeps it runs
    (a dead-hold release, a stuck sleep back to warm): a stale stamp may name an answering driver
    while its frame runs on the silent one, and judging it lost would clear the ref and disown the
    frame next pass. A frame-bearing row naming a driver the pass never asked counts as silent too.

    A stamp is corrected by evidence, never destroyed for: a row whose named frame
    is listed, wearing that Machine's labels, by one answering driver other than the row's - and
    not by the row's own - was stamped wrong (a fork the API stamped with its own default, woken
    elsewhere). It is judged as if stamped right, and its one fix this pass is the re-stamp.

    `kept` names the drivers whose Machine frames are `persistent` (the device), and `persistent`
    states their invariant: here, such a frame is destroyed only when no row exists for it, a stuck
    wake naming one goes back to sleep naming it, and a row naming none is never given one."""
    finishing, seeds = _holds(rows, sleeping_jobs, seeding_jobs)
    restamp = _found_elsewhere(frames, rows)
    by_id = {row.id: replace(row, driver=restamp.get(row.id, row.driver)) for row in rows}
    silent = answered is not None and _silent(rows, answered, asked)
    p = _Pass(now, limits, running_attempts, finishing, running_attempts | seeds, kept, silent)
    verdicts = [(frame, _frame_verdict(frame, by_id, p)) for frame in frames]
    live = frozenset(v[0] for _, v in verdicts if isinstance(v, tuple))
    return ReconcilePlan(
        disowned=tuple(frame for frame, v in verdicts if v == "destroy"),
        fixes=tuple(filter(None, (_row_fix(row, restamp, live, p) for row in rows))),
        skipped=tuple(frame.ref for frame, v in verdicts if v == "skip"),
    )


@dataclass(frozen=True, slots=True)
class _Pass:
    now: dt.datetime
    limits: ReconcileLimits
    running_attempts: frozenset[str]
    finishing: frozenset[uuid.UUID]  # sleeping rows a job will finish: a sleep's, or a seed's
    live_holders: frozenset[str]  # the holders that can still let go: running steps, live seeds
    kept: frozenset[str]
    silent: bool  # a driver the pass asked, or a frame-bearing row names, did not answer


def _holds(
    rows: Sequence[MachineView], sleeping: frozenset[uuid.UUID] | None, seeding: frozenset[uuid.UUID] | None
) -> tuple[frozenset[uuid.UUID], frozenset[str]]:
    """The sleeping rows a job will finish, and the live seeds' holders (None: every one has its job)."""
    asleep = [row for row in rows if row.state == MachineState.SLEEPING]
    if seeding is None:  # a fork's child is born sleeping at epoch 0, and only a seed holds
        seeding = frozenset(row.id for row in asleep if row.epoch == 0)
        seeds = frozenset(row.held_by for row in rows if row.held_by and holder_step(row.held_by) is None)
    else:
        seeds = frozenset(seed_holder(child) for child in seeding)
    return (frozenset(row.id for row in asleep) if sleeping is None else sleeping) | seeding, seeds


def _silent(rows: Sequence[MachineView], answered: frozenset[str], asked: frozenset[str] | None) -> bool:
    named = asked if asked is not None else answered | {row.driver for row in rows}
    return not (named | {row.driver for row in rows if row.frame_ref is not None}) <= answered


# A frame's verdict: owned by a live row (its machine id and epoch), disowned, unparseable, or left be.
_Verdict = tuple[uuid.UUID, int] | Literal["destroy", "skip"] | None


def _frame_verdict(frame: FrameInfo, by_id: Mapping[uuid.UUID, MachineView], p: _Pass) -> _Verdict:
    kind = frame.labels.get(LABEL_KIND)
    if kind == FrameKind.MACHINE:
        return _machine_verdict(frame, by_id, p)
    age = (p.now - frame.created_at).total_seconds()
    if kind in (FrameKind.SEAT, FrameKind.QA, FrameKind.FIGURES, FrameKind.SELFTEST):
        try:
            attempt = int(frame.labels.get(LABEL_ATTEMPT, "0"))
        except ValueError:
            return "skip"
        holder = step_holder(frame.labels.get(LABEL_STEP, ""), attempt)
        return "destroy" if holder not in p.running_attempts and age > p.limits.throwaway_max_s else None
    if kind == FrameKind.VERIFY:
        return "destroy" if age > p.limits.verify_max_s else None
    legacy = kind is None and p.limits.adopt_legacy and frame.ref.startswith(LEGACY_PREFIXES)
    return "destroy" if legacy and age > p.limits.legacy_grace_s else None


def _machine_verdict(frame: FrameInfo, by_id: Mapping[uuid.UUID, MachineView], p: _Pass) -> _Verdict:
    named = machine_label(frame)
    row = by_id.get(named[0]) if named is not None else None
    if named is None or row is None:
        return "skip" if named is None else "destroy"
    # A stuck wake's frame holds nothing its checkpoint does not; a stuck sleep's may hold the only
    # copy of /work, and goes back to warm with its row. A stopped frame (a restart) is judged as a
    # running one: its files are intact, and the next bind starts it.
    stuck = _stuck(row, p.now, p.limits, p.running_attempts, p.finishing)
    if not (stuck and row.state == MachineState.WAKING) and _owns(row, frame, named[1]):
        return row.id, named[1]
    return None if frame.driver in p.kept else "destroy"  # `persistent`: never adopted here


def _row_fix(
    row: MachineView, restamp: Mapping[uuid.UUID, str], live: frozenset[uuid.UUID], p: _Pass
) -> RowFix | None:
    if row.id in restamp:  # its one fix: the next pass judges it on the driver its frame is on
        return RowFix.of(row, row.state, keep_frame=True, driver=restamp[row.id])
    fallback = MachineState.ASLEEP if row.has_checkpoint else MachineState.GONE
    quiet = (p.now - row.state_since).total_seconds() > p.limits.fresh_s
    if _stuck(row, p.now, p.limits, p.running_attempts, p.finishing):
        fix = _unstick(row, fallback, live=row.id in live, kept=p.kept)
    elif row.state in FRAME_STATES and row.id not in live and quiet:
        fix = RowFix.of(row, fallback)
    elif _dead_hold(row, p.live_holders, p.now, p.limits):
        # Past the rule above, a row in a frame state has its frame live: it goes idle, warm.
        to_state = MachineState.IDLE if row.state in FRAME_STATES else row.state
        thaw = row.state == MachineState.RUNNING and holder_step(row.held_by) is None
        fix = RowFix.of(row, to_state, keep_frame=True, release_hold=True, thaw=thaw)
    else:
        return None
    # Its frame may be on a driver that did not answer: nobody looked, so only a fix that keeps it runs.
    return None if p.silent and row.frame_ref is not None and not fix.keep_frame else fix


def _unstick(row: MachineView, fallback: MachineState, *, live: bool, kept: frozenset[str]) -> RowFix:
    """The fix for a stuck row; `live` says a frame of its epoch was listed."""
    if row.state == MachineState.SLEEPING and live:
        # A sleep that died with no job left to finish it: back to warm, as `_unclaim` would
        # have put it (parked while a step still holds it); the sweep offers the sleep again.
        back = MachineState.PARKED if row.held_by else MachineState.IDLE
        return RowFix.of(row, back, keep_frame=True, thaw=True)
    if row.state == MachineState.WAKING and row.driver in kept and row.frame_ref is not None:
        # A wake that died on a persistent frame: back to sleep naming it, as `_leave_asleep`
        # puts it (its profile was never moved: `_claim` leaves a persistent row's to `_wake`).
        # One that names no frame woke from `gone`, and goes back there: gone means gone.
        return RowFix.of(row, MachineState.ASLEEP, keep_frame=True)
    return RowFix.of(row, fallback)


def _found_elsewhere(frames: Sequence[FrameInfo], rows: Sequence[MachineView]) -> dict[uuid.UUID, str]:
    """The rows whose stamp the listing contradicts, and the driver each one's
    frame was found on: exactly one listing driver has the frame the row names, wearing that
    Machine's labels, and it is not the row's. Evidence or nothing - a row whose own driver lists
    the frame too, or that two other drivers list, or that names no frame, keeps its stamp."""
    found: dict[tuple[str, uuid.UUID], set[str]] = {}
    for frame in frames:
        named = machine_label(frame) if frame.labels.get(LABEL_KIND) == FrameKind.MACHINE else None
        if named is not None and frame.driver:
            found.setdefault((frame.ref, named[0]), set()).add(frame.driver)
    out: dict[uuid.UUID, str] = {}
    for row in rows:
        where = found.get((row.frame_ref, row.id), set[str]()) if row.frame_ref and row.driver else set[str]()
        if len(where) == 1 and row.driver not in where:
            out[row.id] = next(iter(where))
    return out


def machine_label(frame: FrameInfo) -> tuple[uuid.UUID, int] | None:
    """A Machine frame's (machine id, epoch) from its labels; None when they do not parse."""
    try:
        return uuid.UUID(frame.labels[LABEL_MACHINE]), int(frame.labels[LABEL_EPOCH])
    except (KeyError, ValueError):
        return None


def _owns(row: MachineView, frame: FrameInfo, epoch: int) -> bool:
    """The row owns this frame: it is warm at the frame's labelled epoch (every driver), or it names
    the frame, on the row's driver, and is warm or asleep - a native provider's computer, which keeps
    its first epoch's labels through every sleep and wake (ADR-063). An archive
    driver's frame name carries its epoch, so for Docker and sandbar the second rule adds nothing."""
    if row.epoch == epoch and row.state in WARM_STATES:
        return True
    return (
        row.frame_ref == frame.ref
        and frame.driver == row.driver
        and (row.state in WARM_STATES or row.state == MachineState.ASLEEP)
    )


def _stuck(
    row: MachineView,
    now: dt.datetime,
    limits: ReconcileLimits,
    running_attempts: frozenset[str],
    finishing: frozenset[uuid.UUID],
) -> bool:
    """A wake or a sleep that nothing will finish, still for longer than `stuck_s`. A wake moves
    `state_since` at each phase it finishes, so its age is its time without progress. It is its
    attempt's while that attempt runs, however long a large checkpoint takes to restore (destroying
    its frame only makes the next attempt restore it again) - up to `throwaway_max_s`, the longest
    any step may run: past it, the attempt that still reads as running is an orphan no worker will
    finish. A sleeping row is its job's while a sleep or, for a fork's child, its seed is queued or
    in flight (`finishing`)."""
    still = (now - row.state_since).total_seconds()
    if still <= limits.stuck_s:
        return False
    if row.state == MachineState.WAKING:
        return row.held_by not in running_attempts or still > limits.throwaway_max_s
    return row.state == MachineState.SLEEPING and row.id not in finishing


def _dead_hold(
    row: MachineView, live_holders: frozenset[str], now: dt.datetime, limits: ReconcileLimits
) -> bool:
    """A hold nobody will ever let go of. Left alone it
    blocks every later step of the task ("the Machine is busy") for good.

    A hold is live while its holder can still let go: a step's while its attempt runs (a parked
    step is `running`, so a live park is never judged dead; a running step of a finished task is
    left out - a park nobody will resume), a fork seed's (`seed:<child>`) while its `machine_seed`
    job is queued or in flight - it resumes or releases its own hold. Any other is dead: a release
    that failed, a worker that died mid-wake (the stuck rule put the row to rest and kept the hold),
    a cancelled park whose Machine was waking, a seed cancelled mid-copy. The row must be unchanged
    for `fresh_s`: a holder that began after the listings were read is not in them yet. A seed
    holds another task's Machine for one copy, so a seed still listed past `throwaway_max_s` is an
    orphan no executor will resume (a rolling deploy's old version), and dead too. `waking` and
    `sleeping` rows are the stuck rule's, never this one's."""
    if row.held_by is None or row.state in (MachineState.WAKING, MachineState.SLEEPING):
        return False
    unchanged = (now - row.state_since).total_seconds()
    if row.held_by in live_holders:
        return holder_step(row.held_by) is None and unchanged > limits.throwaway_max_s
    return unchanged > limits.fresh_s


# ── metering ──────────────────────────────────────────────────────────────────────────────


def segment_cost_usd(seconds: int, profile: str, prices: Mapping[str, Decimal], driver: str = "") -> Decimal:
    """Per profile, per minute: the driver's own `driver.profile` price when the conf names one
    (`device.base=0`, ADR-077), else the profile's, else `base`'s (`code` today)."""
    fallback = prices.get(profile, prices.get(MachineProfile.BASE, Decimal(0)))
    rate = prices.get(f"{driver}.{profile}", fallback) if driver else fallback
    return (Decimal(seconds) / Decimal(60) * rate).quantize(Decimal("0.000001"))


@dataclass(frozen=True, slots=True)
class MachineSummary:
    """What the task page and the CLI show about a Machine. Never a frame reference,
    an object key, a cipher key id or a cloud driver."""

    state: MachineState
    profile: str
    state_since: dt.datetime
    last_active_at: dt.datetime
    checkpoint_at: dt.datetime | None
    checkpoint_bytes: int | None
    browser_saved: bool
    # Every `family == "machine"` usage row of the task, summed: the Machine's segments AND every
    # throwaway frame it paid for - seats, qa, figures and verify frames. The chip shows this.
    warm_seconds: int
    cost_usd: Decimal  # the same rows' cost
    # The live control lease (ADR-065), as the page shows it; None when nobody drives.
    control: LeaseView | None = None
    # The computer a device Machine runs on; None on a cloud driver.
    device_name: str | None = None
    # `device` for a Machine on the user's own computer, which has no screen: its browser is a
    # window there. None on a cloud driver, which is never named.
    driver: str | None = None


# ── the retry note ────────────────────────────────────────────────────────────────────────

RETRY_LIST_CAP: Final = 50


def retry_note(attempt: int, changed: Sequence[str]) -> str:
    """What a retry in the same Machine is told: which attempt it is, and exactly what the failed
    attempt changed (`find /work -newer /task/.step-start`), capped. The old per-attempt frame's
    reason (a retry that inherits half-written files edits what it cannot fully see) is answered
    by showing it what changed."""
    head = (
        f"This is attempt {attempt}, in the same Machine as the attempt that failed. "
        "Its files are still in /work."
    )
    if not changed:
        return f"{head} The failed attempt changed no files."
    shown = list(changed[:RETRY_LIST_CAP])
    lines = "\n".join(f"- {path}" for path in shown)
    more = len(changed) - len(shown)
    tail = f"\n- … and {more} more" if more > 0 else ""
    return f"{head} The failed attempt changed these files:\n{lines}{tail}"


__all__ = [
    "FRAME_STATES",
    "LABEL_ATTEMPT",
    "LABEL_EPOCH",
    "LABEL_INSTANCE",
    "LABEL_KIND",
    "LABEL_MACHINE",
    "LABEL_STEP",
    "LABEL_TASK",
    "LABEL_WORKSPACE",
    "LEGACY_PREFIXES",
    "RETRY_LIST_CAP",
    "SLEEP_JOB_PREFIX",
    "WARM_STATES",
    "FrameInfo",
    "FrameKind",
    "MachineProfile",
    "MachineState",
    "MachineSummary",
    "MachineView",
    "RebuildReason",
    "ReconcileLimits",
    "ReconcilePlan",
    "Resources",
    "RowFix",
    "Segment",
    "SleepReason",
    "SweepAction",
    "SweepArm",
    "SweepLimits",
    "browser_key",
    "checkpoint_key",
    "frame_labels",
    "frame_name",
    "held_by_seed",
    "holder_step",
    "machine_label",
    "machine_prefix",
    "parse_prices",
    "parse_resources",
    "parse_rfc3339",
    "persistent",
    "reconcile",
    "retry_note",
    "seed_holder",
    "seed_key",
    "segment_cost_usd",
    "segment_seconds",
    "sleep_job_id",
    "sleep_job_machine",
    "step_holder",
    "sweep_actions",
    "widen",
]
