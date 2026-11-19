# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The reconciler, one input per arm: each frame kind's verdict (kept, destroyed, skipped, left be)
and each row rule (re-stamp, stuck, lost, dead hold, a silent driver) pinned to the exact plan
`reconcile` returns. No I/O - frames, rows and `now` are arguments."""

from __future__ import annotations

import uuid
from dataclasses import replace
from typing import TypedDict

import pytest

from prismoid.schemas.machines import (
    LABEL_ATTEMPT,
    LABEL_EPOCH,
    LABEL_KIND,
    LABEL_MACHINE,
    LABEL_STEP,
    FrameInfo,
    FrameKind,
    MachineState,
    MachineView,
    ReconcilePlan,
    RowFix,
    reconcile,
    seed_holder,
    step_holder,
)
from tests.support.rigs.machine_decisions_rig import NOW, RLIMITS, ago, view

A = uuid.UUID(int=1)
B = uuid.UUID(int=2)
RUNNING = step_holder("s1", 1)


def mframe(mid: uuid.UUID, epoch: int, *, ref: str = "", driver: str = "", age: float = 60) -> FrameInfo:
    labels = {LABEL_KIND: FrameKind.MACHINE, LABEL_MACHINE: str(mid), LABEL_EPOCH: str(epoch)}
    return FrameInfo(ref=ref or f"machine-{mid}-{epoch}", labels=labels, created_at=ago(age), driver=driver)


def tframe(kind: FrameKind, attempt: str, *, age: float) -> FrameInfo:
    labels = {LABEL_KIND: kind, LABEL_STEP: "s1", LABEL_ATTEMPT: attempt}
    return FrameInfo(ref=f"{kind}-{attempt}-{age}", labels=labels, created_at=ago(age))


def raw(ref: str, labels: dict[str, str], *, age: float) -> FrameInfo:
    return FrameInfo(ref=ref, labels=labels, created_at=ago(age))


def row(
    state: MachineState,
    *,
    since: float,
    held_by: str | None = None,
    has_checkpoint: bool = False,
    frame_ref: str | None = None,
    driver: str = "",
) -> MachineView:
    """Machine A at epoch 3, naming `frame_ref` on `driver`."""
    base = view(state, since=since, epoch=3, held_by=held_by, has_checkpoint=has_checkpoint, mid=A)
    return replace(base, frame_ref=frame_ref, driver=driver)


class Listing(TypedDict, total=False):
    sleeping_jobs: frozenset[uuid.UUID] | None
    seeding_jobs: frozenset[uuid.UUID] | None
    answered: frozenset[str] | None
    asked: frozenset[str] | None
    kept: frozenset[str]


# ── frame verdicts: (frames, rows, running attempts, destroyed refs, skipped refs) ─────────────

OLD = RLIMITS.throwaway_max_s + 1
FRAMES: dict[str, tuple[list[FrameInfo], list[MachineView], frozenset[str], tuple[str, ...], tuple[str, ...]]] = {
    "machine labels that do not parse are skipped": (
        [raw("m-bad", {LABEL_KIND: FrameKind.MACHINE, LABEL_MACHINE: "nope", LABEL_EPOCH: "1"}, age=9999)],
        [], frozenset(), (), ("m-bad",),
    ),
    "a machine frame no row claims is destroyed": ([mframe(A, 3)], [], frozenset(), (f"machine-{A}-3",), ()),
    "a machine frame its warm row owns is kept": (
        [mframe(A, 3)], [row(MachineState.IDLE, since=120)], frozenset(), (), (),
    ),
    "a stuck wake's frame is destroyed": (
        [mframe(A, 3)], [row(MachineState.WAKING, since=601)], frozenset(), (f"machine-{A}-3",), (),
    ),
    "a frame of an earlier epoch is destroyed": (
        [mframe(A, 2)], [row(MachineState.IDLE, since=120)], frozenset(), (f"machine-{A}-2",), (),
    ),
    "a throwaway attempt that does not parse is skipped": (
        [tframe(FrameKind.SEAT, "x", age=OLD)], [], frozenset(), (), (f"seat-x-{OLD}",),
    ),
    "an old throwaway whose step runs is kept": ([tframe(FrameKind.QA, "1", age=OLD)], [], frozenset({RUNNING}), (), ()),
    "an old throwaway whose step ended is destroyed": (
        [tframe(FrameKind.FIGURES, "2", age=OLD)], [], frozenset({RUNNING}), (f"figures-2-{OLD}",), (),
    ),
    "a young throwaway is kept": ([tframe(FrameKind.SELFTEST, "2", age=60)], [], frozenset(), (), ()),
    "an old verify frame is destroyed": (
        [raw("v-old", {LABEL_KIND: FrameKind.VERIFY}, age=RLIMITS.verify_max_s + 1)], [], frozenset(), ("v-old",), (),
    ),
    "a young verify frame is kept": ([raw("v-new", {LABEL_KIND: FrameKind.VERIFY}, age=60)], [], frozenset(), (), ()),
    "an old unlabelled legacy frame is destroyed": (
        [raw("frame-t1", {}, age=RLIMITS.legacy_grace_s + 1)], [], frozenset(), ("frame-t1",), (),
    ),
    "a young unlabelled legacy frame is kept": ([raw("frame-t2", {}, age=60)], [], frozenset(), (), ()),
    "an unlabelled stranger is never ours": ([raw("qa-postgres", {}, age=99999)], [], frozenset(), (), ()),
    "an unknown kind is left be": ([raw("odd", {LABEL_KIND: "other"}, age=99999)], [], frozenset(), (), ()),
}  # fmt: skip


@pytest.mark.parametrize("case", FRAMES.values(), ids=FRAMES.keys())
def test_each_frame_kind_gets_its_verdict(
    case: tuple[list[FrameInfo], list[MachineView], frozenset[str], tuple[str, ...], tuple[str, ...]],
) -> None:
    frames, rows, running, destroyed, skipped = case
    plan = reconcile(frames, rows, running, NOW, RLIMITS)
    assert (plan.destroy, plan.skipped) == (destroyed, skipped)


def test_a_persistent_drivers_unowned_frame_is_left_be_while_its_row_exists() -> None:
    frames = [mframe(A, 2, driver="device"), mframe(B, 2, driver="device")]
    plan = reconcile(
        frames, [row(MachineState.IDLE, since=120)], frozenset(), NOW, RLIMITS, kept=frozenset({"device"})
    )
    assert plan.destroy == (f"machine-{B}-2",)


def test_a_named_instance_never_adopts_legacy_frames() -> None:
    old = raw("frame-t1", {}, age=RLIMITS.legacy_grace_s + 1)
    assert reconcile([old], [], frozenset(), NOW, replace(RLIMITS, adopt_legacy=False)).destroy == ()


# ── row rules: one row in, the exact fix out ──────────────────────────────────────────────────


def _fix(
    state: MachineState,
    to: MachineState,
    *,
    since: float,
    held_by: str | None = None,
    keep_frame: bool = False,
    release_hold: bool = False,
    thaw: bool = False,
) -> RowFix:
    return RowFix(A, 3, state, ago(since), held_by, to, keep_frame, release_hold, thaw)


def test_a_row_its_frame_was_found_on_another_driver_is_re_stamped_and_nothing_else() -> None:
    stamped = row(MachineState.IDLE, since=120, frame_ref="m1", driver="docker")
    plan = reconcile([mframe(A, 3, ref="m1", driver="sandbar")], [stamped], frozenset(), NOW, RLIMITS)
    assert plan == ReconcilePlan(
        disowned=(),
        fixes=(RowFix(A, 3, MachineState.IDLE, ago(120), None, MachineState.IDLE, True, driver="sandbar"),),
    )


ROWS: dict[str, tuple[MachineView, list[FrameInfo], Listing, RowFix | None]] = {
    "a stuck sleep with its frame live goes back to idle, thawed": (
        row(MachineState.SLEEPING, since=601), [mframe(A, 3)], {"sleeping_jobs": frozenset()},
        _fix(MachineState.SLEEPING, MachineState.IDLE, since=601, keep_frame=True, thaw=True),
    ),
    "a held stuck sleep with its frame live goes back to parked": (
        row(MachineState.SLEEPING, since=601, held_by=RUNNING), [mframe(A, 3)], {"sleeping_jobs": frozenset()},
        _fix(MachineState.SLEEPING, MachineState.PARKED, since=601, held_by=RUNNING, keep_frame=True, thaw=True),
    ),
    "a stuck sleep whose job is still queued is left to it": (
        row(MachineState.SLEEPING, since=601), [], {"sleeping_jobs": frozenset({A})}, None,
    ),
    "a stuck sleep with no frame falls back to its checkpoint": (
        row(MachineState.SLEEPING, since=601, has_checkpoint=True), [], {"sleeping_jobs": frozenset()},
        _fix(MachineState.SLEEPING, MachineState.ASLEEP, since=601),
    ),
    "a stuck wake on a persistent frame goes back to sleep naming it": (
        row(MachineState.WAKING, since=601, frame_ref="dev-1", driver="device"), [], {"kept": frozenset({"device"})},
        _fix(MachineState.WAKING, MachineState.ASLEEP, since=601, keep_frame=True),
    ),
    "a stuck wake with no checkpoint falls back to gone": (
        row(MachineState.WAKING, since=601), [], {}, _fix(MachineState.WAKING, MachineState.GONE, since=601),
    ),
    "a warm row whose frame is missing falls back": (
        row(MachineState.IDLE, since=31, has_checkpoint=True), [], {},
        _fix(MachineState.IDLE, MachineState.ASLEEP, since=31),
    ),
    "a warm row that changed after the listing is left be": (row(MachineState.RUNNING, since=29), [], {}, None),
    "a running dead hold on a live frame goes idle, thawed only for a non-step holder": (
        row(MachineState.RUNNING, since=31, held_by=RUNNING), [mframe(A, 3)], {},
        _fix(MachineState.RUNNING, MachineState.IDLE, since=31, held_by=RUNNING, keep_frame=True, release_hold=True),
    ),
    "a dead seed hold on a running row is released and thawed": (
        row(MachineState.RUNNING, since=31, held_by=seed_holder(B)), [mframe(A, 3)], {"seeding_jobs": frozenset()},
        _fix(
            MachineState.RUNNING, MachineState.IDLE, since=31, held_by=seed_holder(B),
            keep_frame=True, release_hold=True, thaw=True,
        ),
    ),
    "a live seed hold is kept": (
        row(MachineState.RUNNING, since=31, held_by=seed_holder(B)), [mframe(A, 3)],
        {"seeding_jobs": frozenset({B})}, None,
    ),
    "a seed hold past the longest run is dead though its job is listed": (
        row(MachineState.PARKED, since=OLD, held_by=seed_holder(B)), [mframe(A, 3)],
        {"seeding_jobs": frozenset({B})},
        _fix(
            MachineState.PARKED, MachineState.IDLE, since=OLD, held_by=seed_holder(B),
            keep_frame=True, release_hold=True,
        ),
    ),
    "an asleep row's dead hold is released and stays asleep": (
        row(MachineState.ASLEEP, since=31, held_by=RUNNING), [], {},
        _fix(MachineState.ASLEEP, MachineState.ASLEEP, since=31, held_by=RUNNING, keep_frame=True, release_hold=True),
    ),
    "while an asked driver is silent a frame-bearing row is not judged lost": (
        row(MachineState.IDLE, since=31, frame_ref="m1", driver="docker"), [],
        {"answered": frozenset({"docker"}), "asked": frozenset({"docker", "sandbar"})}, None,
    ),
    "while an asked driver is silent a dead hold is still released": (
        row(MachineState.ASLEEP, since=31, held_by=RUNNING, frame_ref="m1", driver="docker"), [],
        {"answered": frozenset({"docker"}), "asked": frozenset({"docker", "sandbar"})},
        _fix(MachineState.ASLEEP, MachineState.ASLEEP, since=31, held_by=RUNNING, keep_frame=True, release_hold=True),
    ),
    "a row naming no frame is judged lost whatever driver is silent": (
        row(MachineState.IDLE, since=31, driver="docker"), [],
        {"answered": frozenset({"docker"}), "asked": frozenset({"docker", "sandbar"})},
        _fix(MachineState.IDLE, MachineState.GONE, since=31),
    ),
    "a frame-bearing row naming a driver nobody asked counts as silent": (
        row(MachineState.IDLE, since=31, frame_ref="m1", driver="remote"), [],
        {"answered": frozenset({"docker"}), "asked": frozenset({"docker"})}, None,
    ),
    "every asked driver answered: a lost frame falls back": (
        row(MachineState.IDLE, since=31, frame_ref="m1", driver="docker"), [],
        {"answered": frozenset({"docker"})}, _fix(MachineState.IDLE, MachineState.GONE, since=31),
    ),
}  # fmt: skip


@pytest.mark.parametrize("case", ROWS.values(), ids=ROWS.keys())
def test_each_row_rule_gets_its_fix(
    case: tuple[MachineView, list[FrameInfo], Listing, RowFix | None],
) -> None:
    machine, frames, listing, fix = case
    plan = reconcile(frames, [machine], frozenset(), NOW, RLIMITS, **listing)
    assert plan.fixes == ((fix,) if fix else ())
