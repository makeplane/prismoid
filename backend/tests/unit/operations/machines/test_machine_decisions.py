# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Machine's pure decisions: the sweep's arms, the
reconciler's rule, the metering math, the retry note. No I/O - rows and `now` are arguments."""

import datetime as dt
import uuid
from dataclasses import replace
from decimal import Decimal

from prismoid.schemas.machines import (
    LABEL_ATTEMPT,
    LABEL_EPOCH,
    LABEL_KIND,
    LABEL_MACHINE,
    LABEL_STEP,
    LABEL_TASK,
    LABEL_WORKSPACE,
    RETRY_LIST_CAP,
    FrameInfo,
    FrameKind,
    MachineState,
    MachineView,
    ReconcileLimits,
    Resources,
    RowFix,
    SweepAction,
    SweepArm,
    frame_labels,
    parse_prices,
    parse_resources,
    parse_rfc3339,
    reconcile,
    retry_note,
    seed_holder,
    segment_cost_usd,
    segment_seconds,
    sleep_job_id,
    sleep_job_machine,
    step_holder,
    sweep_actions,
)
from tests.support.rigs.machine_decisions_rig import LIMITS, NOW, RLIMITS, ago, view


def test_idle_sleeps_only_after_the_idle_clock_from_last_activity() -> None:
    fresh = view(MachineState.IDLE, since=5, active=599)
    stale = view(MachineState.IDLE, since=5, active=601)  # a seed hold reset state_since, not last_active_at
    actions = sweep_actions([fresh, stale], NOW, LIMITS)
    assert actions == [SweepAction(SweepArm.IDLE, stale.id, stale.epoch)]


def test_parked_sleeps_after_the_hold_counted_from_the_park() -> None:
    held = view(MachineState.PARKED, since=1799, active=5000)
    over = view(MachineState.PARKED, since=1801, active=5000)
    assert sweep_actions([held, over], NOW, LIMITS) == [SweepAction(SweepArm.PARK_HOLD, over.id, over.epoch)]


def test_retention_expires_stored_objects_after_fourteen_days_of_inactivity() -> None:
    keep = view(MachineState.ASLEEP, since=10, active=14 * 86400 - 60, has_objects=True)
    old = view(MachineState.ASLEEP, since=10, active=14 * 86400 + 60, has_objects=True)
    old_gone_with_orphans = view(MachineState.GONE, since=10, active=15 * 86400, has_objects=True)
    old_gone_clean = view(MachineState.GONE, since=10, active=15 * 86400, has_objects=False)
    arms = sweep_actions([keep, old, old_gone_with_orphans, old_gone_clean], NOW, LIMITS)
    assert [(a.arm, a.machine_id) for a in arms] == [
        (SweepArm.RETENTION, old.id),
        (SweepArm.RETENTION, old_gone_with_orphans.id),
    ]


def test_stuck_transitions_go_to_the_reconciler() -> None:
    waking = view(MachineState.WAKING, since=601)
    sleeping = view(MachineState.SLEEPING, since=601)
    young = view(MachineState.SLEEPING, since=30)
    running = view(MachineState.RUNNING, since=99999)  # a long step is not stuck
    arms = sweep_actions([waking, sleeping, young, running], NOW, LIMITS)
    assert {a.machine_id for a in arms if a.arm == SweepArm.STUCK} == {waking.id, sleeping.id}


def test_a_live_lease_keeps_an_idle_or_a_parked_machine_awake() -> None:
    idle = view(MachineState.IDLE, active=10_000, lease_until=30)
    parked = view(MachineState.PARKED, since=10_000, active=10_000, lease_until=0.000001)
    assert sweep_actions([idle, parked], NOW, LIMITS) == []


def test_a_run_out_lease_is_ended_by_its_since_and_the_same_tick_never_sleeps_it() -> None:
    """The ending moves `last_active_at`, so the idle clock restarts after the lease-end job: the
    tick that hands it over never puts the Machine to sleep under the person who just drove it."""
    idle = view(MachineState.IDLE, active=10_000, lease_until=-1, lease_since=300)
    parked = view(MachineState.PARKED, since=10_000, active=10_000, lease_until=-1, lease_since=300)
    assert sweep_actions([idle, parked], NOW, LIMITS) == [
        SweepAction(SweepArm.LEASE_END, idle.id, idle.epoch, since=ago(300)),
        SweepAction(SweepArm.LEASE_END, parked.id, parked.epoch, since=ago(300)),
    ]


def test_until_equal_to_now_is_expired() -> None:
    edge = view(MachineState.IDLE, lease_until=0)
    live = view(MachineState.IDLE, lease_until=0.000001)
    arms = sweep_actions([edge, live], NOW, LIMITS)
    assert [(a.arm, a.machine_id) for a in arms] == [(SweepArm.LEASE_END, edge.id)]


def test_a_running_machine_whose_lease_ran_out_is_ended_too() -> None:
    running = view(MachineState.RUNNING, since=99_999, lease_until=-5)
    assert [a.arm for a in sweep_actions([running], NOW, LIMITS)] == [SweepArm.LEASE_END]


def test_the_kill_switch_ends_live_leases_and_stops_every_warm_browser() -> None:
    leased = view(MachineState.PARKED, profile="browser", lease_until=300)
    idle_browser = view(MachineState.IDLE, profile="browser")
    running_browser = view(MachineState.RUNNING, profile="browser")
    base = view(MachineState.IDLE, profile="base")
    asleep_browser = view(MachineState.ASLEEP, profile="browser")
    waking_browser = view(MachineState.WAKING, profile="browser")
    arms = sweep_actions(
        [leased, idle_browser, running_browser, base, asleep_browser, waking_browser],
        NOW,
        LIMITS,
        computer_use_on=False,
    )
    assert [(a.arm, a.machine_id) for a in arms] == [
        (SweepArm.LEASE_END, leased.id),
        (SweepArm.BROWSER_STOP, leased.id),
        (SweepArm.BROWSER_STOP, idle_browser.id),
        (SweepArm.BROWSER_STOP, running_browser.id),
    ]
    assert arms[0].since == leased.control_since and arms[1].since is None


def test_the_kill_switch_never_stops_a_browser_the_same_pass_puts_to_sleep() -> None:
    """The sleep's checkpoint shuts the daemon down itself, so a browser stop
    racing it in the same pass could respawn Chromium over the profile the archive is reading. A
    row that gets IDLE or PARK_HOLD gets no BROWSER_STOP; one that stays warm still does."""
    sleepy = view(MachineState.IDLE, profile="browser", active=10_000)
    held_too_long = view(MachineState.PARKED, profile="browser", since=10_000, active=10_000)
    awake = view(MachineState.IDLE, profile="browser")
    arms = sweep_actions([sleepy, held_too_long, awake], NOW, LIMITS, computer_use_on=False)
    assert [(a.arm, a.machine_id) for a in arms] == [
        (SweepArm.IDLE, sleepy.id),
        (SweepArm.PARK_HOLD, held_too_long.id),
        (SweepArm.BROWSER_STOP, awake.id),
    ]


def test_with_computer_use_on_no_browser_is_stopped() -> None:
    assert sweep_actions([view(MachineState.IDLE, profile="browser")], NOW, LIMITS) == []


def machine_frame(mid: uuid.UUID, epoch: int, *, age: float = 60) -> FrameInfo:
    return FrameInfo(
        ref=f"machine-{mid}-{epoch}",
        labels={LABEL_KIND: FrameKind.MACHINE, LABEL_MACHINE: str(mid), LABEL_EPOCH: str(epoch)},
        created_at=ago(age),
    )


def test_a_machine_frame_lives_only_while_its_row_owns_its_epoch() -> None:
    live = view(MachineState.IDLE, since=120, epoch=4)
    moved_on = view(MachineState.RUNNING, since=120, epoch=5)
    asleep = view(MachineState.ASLEEP, since=120, epoch=2, has_checkpoint=True)
    frames = [
        machine_frame(live.id, 4),
        machine_frame(moved_on.id, 4),  # a stale epoch
        machine_frame(moved_on.id, 5),
        machine_frame(asleep.id, 2),  # its row says asleep
        machine_frame(uuid.uuid4(), 1),  # no row at all (the task was deleted)
    ]
    plan = reconcile(frames, [live, moved_on, asleep], frozenset(), NOW, RLIMITS)
    assert set(plan.destroy) == {
        f"machine-{moved_on.id}-4",
        f"machine-{asleep.id}-2",
        frames[4].ref,
    }


def test_a_second_worker_booting_keeps_a_live_peers_machine() -> None:
    # The rolling-deploy case the old reap-all got wrong (runner/worker.py:110-114).
    running = view(MachineState.RUNNING, since=5, epoch=7, held_by="step:s1:1")
    plan = reconcile([machine_frame(running.id, 7, age=5)], [running], frozenset(), NOW, RLIMITS)
    assert plan.destroy == () and plan.fixes == ()


def test_throwaway_frames_live_while_their_step_runs_or_while_young() -> None:
    def seat(step: str, attempt: int, age: float) -> FrameInfo:
        return FrameInfo(
            ref=f"frame-t-{step}-{attempt}",
            labels={LABEL_KIND: FrameKind.SEAT, LABEL_STEP: step, LABEL_ATTEMPT: str(attempt)},
            created_at=ago(age),
        )

    running_old = seat("s1", 1, 9000)
    dead_old = seat("s2", 1, 9000)
    dead_young = seat("s3", 1, 60)
    verify_old = FrameInfo(ref="verify-abc", labels={LABEL_KIND: FrameKind.VERIFY}, created_at=ago(1300))
    plan = reconcile(
        [running_old, dead_old, dead_young, verify_old], [], frozenset({step_holder("s1", 1)}), NOW, RLIMITS
    )
    assert set(plan.destroy) == {dead_old.ref, verify_old.ref}


def test_unlabelled_frames_from_before_1b_get_an_hours_grace() -> None:
    young = FrameInfo(ref="frame-a-b-1", labels={}, created_at=ago(600))
    old = FrameInfo(ref="frame-c-d-1", labels={}, created_at=ago(4000))
    stranger = FrameInfo(ref="postgres", labels={}, created_at=ago(99999))  # not ours: never touched
    plan = reconcile([young, old, stranger], [], frozenset(), NOW, RLIMITS)
    assert plan.destroy == (old.ref,)


def test_a_named_dev_instance_never_adopts_unlabelled_frames() -> None:
    # Dev instances: pre-1b frames belong to whichever worktree made them; only the default
    # instance (settings.worker.instance == "") sweeps them.
    old = FrameInfo(ref="frame-c-d-1", labels={}, created_at=ago(99999))
    limits = ReconcileLimits(
        throwaway_max_s=2400,
        verify_max_s=1200,
        legacy_grace_s=3600,
        stuck_s=600,
        fresh_s=30,
        adopt_legacy=False,
    )
    assert reconcile([old], [], frozenset(), NOW, limits).destroy == ()


def test_a_row_claiming_a_frame_the_provider_lacks_falls_back_to_its_checkpoint() -> None:
    with_ckpt = view(MachineState.IDLE, since=300, epoch=3, has_checkpoint=True)
    without = view(MachineState.RUNNING, since=300, epoch=6)
    just_woke = view(
        MachineState.RUNNING, since=5, epoch=2
    )  # inside fresh_s: its frame may postdate the listing
    plan = reconcile([], [with_ckpt, without, just_woke], frozenset(), NOW, RLIMITS)
    assert set(plan.fixes) == {
        RowFix.of(with_ckpt, MachineState.ASLEEP),
        RowFix.of(without, MachineState.GONE),
    }


def test_a_stopped_frame_is_judged_like_a_running_one() -> None:
    """A Docker or host restart left the container stopped - with its files: a bind starts it
    (`FrameDriver.bind`), so the inventory lists it like any other (a `FrameInfo` carries no run
    state) and its row and the container stay as they would for a running frame - a young one
    included, created and not yet started. A stopped frame its row disowns goes like any other."""
    idle = view(MachineState.IDLE, since=300, epoch=3, has_checkpoint=True)
    running = view(MachineState.RUNNING, since=300, epoch=4, held_by=step_holder("s1", 1))
    woke = view(
        MachineState.RUNNING, since=0.2, epoch=5, held_by=step_holder("s2", 1)
    )  # created, not started
    frames = [
        machine_frame(idle.id, 3),
        machine_frame(running.id, 4),
        machine_frame(woke.id, 5, age=0.5),
    ]
    live = frozenset({step_holder("s1", 1), step_holder("s2", 1)})
    plan = reconcile(frames, [idle, running, woke], live, NOW, RLIMITS)
    assert plan.destroy == () and plan.fixes == ()
    moved_on = view(MachineState.ASLEEP, since=300, epoch=6, has_checkpoint=True)
    old = machine_frame(moved_on.id, 5)
    assert reconcile([old], [moved_on], frozenset(), NOW, RLIMITS).destroy == (old.ref,)


def test_a_stuck_waking_row_falls_back_and_its_frame_goes() -> None:
    # Its checkpoint is intact: the half-woken frame holds nothing the row does not.
    stuck = view(MachineState.WAKING, since=700, epoch=9, has_checkpoint=True)
    plan = reconcile([machine_frame(stuck.id, 9)], [stuck], frozenset(), NOW, RLIMITS)
    assert plan.destroy == (f"machine-{stuck.id}-9",)
    assert plan.fixes == (RowFix.of(stuck, MachineState.ASLEEP),)


def test_a_slow_wake_is_its_running_attempts_however_long_it_takes() -> None:
    """A wake restoring a large checkpoint can pass `stuck_s`. While its attempt runs it is that
    attempt's to finish or give up: destroying its frame only made the next attempt (infra rungs are
    uncapped) restore the same checkpoint into the same end, for ever."""
    holder = step_holder("s1", 2)
    slow = view(MachineState.WAKING, since=700, epoch=4, held_by=holder, has_checkpoint=True)
    plan = reconcile([machine_frame(slow.id, 4, age=650)], [slow], frozenset({holder}), NOW, RLIMITS)
    assert plan.destroy == () and plan.fixes == ()
    dead = view(MachineState.WAKING, since=700, epoch=4, held_by=step_holder("s1", 1), has_checkpoint=True)
    gone = reconcile([machine_frame(dead.id, 4, age=650)], [dead], frozenset({holder}), NOW, RLIMITS)
    assert gone.destroy == (f"machine-{dead.id}-4",), "a wake whose attempt ended is stuck"
    assert gone.fixes == (RowFix.of(dead, MachineState.ASLEEP),)


def test_an_orphaned_wake_is_stuck_once_it_has_shown_no_progress_for_a_whole_run() -> None:
    """A wake moves `state_since` at each phase it finishes. One whose attempt still reads as
    running yet has shown nothing for longer than any step may run (`throwaway_max_s`) is an
    orphan - a worker that never came back - and must not hold its frame and a cap slot for ever."""
    holder = step_holder("s1", 2)
    orphan = view(MachineState.WAKING, since=2401, epoch=4, held_by=holder, has_checkpoint=True)
    plan = reconcile([machine_frame(orphan.id, 4, age=3000)], [orphan], frozenset({holder}), NOW, RLIMITS)
    assert plan.destroy == (f"machine-{orphan.id}-4",)
    assert plan.fixes == (RowFix.of(orphan, MachineState.ASLEEP),)


def test_a_caller_that_cannot_tell_gets_no_stuck_sleep() -> None:
    # Fail-safe (fix round 2): without `sleeping_jobs`, every sleeping row has its job.
    row = view(MachineState.SLEEPING, since=7000, epoch=9, has_checkpoint=True)
    plan = reconcile([machine_frame(row.id, 9)], [row], frozenset(), NOW, RLIMITS)
    assert plan.destroy == () and plan.fixes == ()
    assert reconcile([], [row], frozenset(), NOW, RLIMITS, sleeping_jobs=None).fixes == ()


def test_a_sleeping_row_whose_job_is_still_queued_is_not_stuck() -> None:
    # A Sleep now behind a queue backlog, a slow upload, a worker down mid-sleep: the job (or DBOS
    # recovery) finishes it. Its frame may hold the only copy of /work.
    row = view(MachineState.SLEEPING, since=7000, epoch=9, has_checkpoint=True)
    plan = reconcile(
        [machine_frame(row.id, 9)], [row], frozenset(), NOW, RLIMITS, sleeping_jobs=frozenset({row.id})
    )
    assert plan.destroy == () and plan.fixes == ()


def test_a_stuck_sleep_with_its_frame_live_goes_back_to_warm_and_is_thawed() -> None:
    idle = view(MachineState.SLEEPING, since=700, epoch=9, has_checkpoint=True)
    parked = view(MachineState.SLEEPING, since=700, epoch=2, held_by=step_holder("s1", 1))
    frames = [machine_frame(idle.id, 9), machine_frame(parked.id, 2)]
    running = frozenset({step_holder("s1", 1)})
    plan = reconcile(frames, [idle, parked], running, NOW, RLIMITS, sleeping_jobs=frozenset())
    assert plan.destroy == ()  # the frame may hold the only copy of /work
    assert plan.fixes == (
        RowFix.of(idle, MachineState.IDLE, keep_frame=True, thaw=True),
        RowFix.of(parked, MachineState.PARKED, keep_frame=True, thaw=True),
    )


def test_a_stuck_sleep_whose_frame_is_gone_falls_back_to_its_checkpoint() -> None:
    with_ckpt = view(MachineState.SLEEPING, since=700, epoch=9, has_checkpoint=True)
    without = view(MachineState.SLEEPING, since=700, epoch=3)
    plan = reconcile([], [with_ckpt, without], frozenset(), NOW, RLIMITS, sleeping_jobs=frozenset())
    assert plan.fixes == (
        RowFix.of(with_ckpt, MachineState.ASLEEP),
        RowFix.of(without, MachineState.GONE),
    )


def test_a_young_sleep_is_left_to_its_job() -> None:
    row = view(MachineState.SLEEPING, since=500, epoch=9)
    plan = reconcile([machine_frame(row.id, 9)], [row], frozenset(), NOW, RLIMITS, sleeping_jobs=frozenset())
    assert plan.destroy == () and plan.fixes == ()


def test_a_forks_machine_waits_on_its_seed_job_not_on_the_clock() -> None:
    """A fork's child is born `sleeping` at epoch 0 while its `machine_seed` job fills its seed -
    a job that may retry for hours. Called stuck, the child would go `gone`, the fork's first step
    would wake it fresh, and the seed that lands after would be thrown away."""
    child = view(MachineState.SLEEPING, since=7000, epoch=0)
    for seeding in (frozenset({child.id}), None):  # its job is active; or nobody can tell
        plan = reconcile(
            [], [child], frozenset(), NOW, RLIMITS, sleeping_jobs=frozenset(), seeding_jobs=seeding
        )
        assert plan.fixes == ()
    plan = reconcile(
        [], [child], frozenset(), NOW, RLIMITS, sleeping_jobs=frozenset(), seeding_jobs=frozenset()
    )
    assert plan.fixes == (RowFix.of(child, MachineState.GONE),), "a seed job that ended for good"


def test_an_unlabelled_container_that_is_not_a_frame_is_never_destroyed() -> None:
    # Every unnamed pre-1b frame was `frame-{task_id}`; Docker lists every container on the
    # daemon, and a `qa-postgres` of someone else's is not ours.
    others = [
        FrameInfo(ref=name, labels={}, created_at=ago(99999)) for name in ("qa-x", "verify-db", "selftest")
    ]
    assert reconcile(others, [], frozenset(), NOW, RLIMITS).destroy == ()


def test_a_frame_with_malformed_labels_is_skipped_never_destroyed() -> None:
    bad_machine = FrameInfo(
        ref="machine-bad-1",
        labels={LABEL_KIND: FrameKind.MACHINE, LABEL_MACHINE: "not-a-uuid", LABEL_EPOCH: "1"},
        created_at=ago(99999),
    )
    no_epoch = FrameInfo(
        ref="machine-bad-2",
        labels={LABEL_KIND: FrameKind.MACHINE, LABEL_MACHINE: str(uuid.uuid4())},
        created_at=ago(9),
    )
    bad_attempt = FrameInfo(
        ref="frame-bad-3",
        labels={LABEL_KIND: FrameKind.SEAT, LABEL_STEP: "s1", LABEL_ATTEMPT: "one"},
        created_at=ago(99999),
    )
    plan = reconcile([bad_machine, no_epoch, bad_attempt], [], frozenset(), NOW, RLIMITS)
    assert plan.destroy == ()
    assert plan.skipped == ("machine-bad-1", "machine-bad-2", "frame-bad-3")


def test_frame_labels_carry_the_kind_and_only_the_ids_given() -> None:
    workspace, task, step = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    assert frame_labels(FrameKind.SELFTEST) == {LABEL_KIND: "selftest"}
    assert frame_labels(FrameKind.QA, workspace_id=workspace, task_id=task, step_id=step, attempt=2) == {
        LABEL_KIND: "qa",
        LABEL_WORKSPACE: str(workspace),
        LABEL_TASK: str(task),
        LABEL_STEP: str(step),
        LABEL_ATTEMPT: "2",
    }


def test_a_labelled_throwaway_frame_is_judged_by_its_attempt_or_its_age() -> None:
    step = uuid.uuid4()
    labels = frame_labels(
        FrameKind.QA, workspace_id=uuid.uuid4(), task_id=uuid.uuid4(), step_id=step, attempt=2
    )
    running = FrameInfo(ref="frame-qa-a", labels=labels, created_at=ago(9000))
    figures = FrameInfo(
        ref="frame-figures-b",
        labels=frame_labels(FrameKind.FIGURES, task_id=uuid.uuid4()),
        created_at=ago(9000),
    )
    young = FrameInfo(ref="frame-selftest-c", labels=frame_labels(FrameKind.SELFTEST), created_at=ago(60))
    plan = reconcile([running, figures, young], [], frozenset({step_holder(str(step), 2)}), NOW, RLIMITS)
    assert plan.destroy == (figures.ref,)


def test_a_sleep_job_id_names_its_machine() -> None:
    mid = uuid.uuid4()
    key = sleep_job_id(mid, 3, NOW)
    assert key == f"machine-sleep-{mid}-3~{NOW.isoformat()}"
    assert sleep_job_machine(key) == mid
    assert sleep_job_machine("machine-sleep-nonsense") is None
    assert sleep_job_machine(f"machine-expire-{mid}-3") is None


def released(row: MachineView, to: MachineState, *, thaw: bool = False) -> RowFix:
    return RowFix.of(row, to, keep_frame=True, release_hold=True, thaw=thaw)


def test_a_running_dead_hold_on_a_live_frame_goes_idle_and_lets_go() -> None:
    # A release that failed: the attempt is over, its hold would block every later step.
    row = view(MachineState.RUNNING, since=300, epoch=4, held_by=step_holder("s1", 1))
    plan = reconcile([machine_frame(row.id, 4)], [row], frozenset({step_holder("s1", 2)}), NOW, RLIMITS)
    assert plan.destroy == ()
    assert plan.fixes == (released(row, MachineState.IDLE),)


def test_a_parked_dead_hold_goes_idle_and_lets_go() -> None:
    # A park nobody will resume (its task finished: `running_attempts` leaves it out).
    row = view(MachineState.PARKED, since=300, epoch=2, held_by=step_holder("s1", 1))
    plan = reconcile([machine_frame(row.id, 2)], [row], frozenset(), NOW, RLIMITS)
    assert plan.fixes == (released(row, MachineState.IDLE),)


def test_an_asleep_dead_hold_lets_go_and_stays_asleep() -> None:
    # A worker that died mid-wake: the stuck rule put the row to rest and kept its hold.
    asleep = view(MachineState.ASLEEP, since=300, held_by=step_holder("s1", 1), has_checkpoint=True)
    gone = view(MachineState.GONE, since=300, held_by=step_holder("s2", 3))
    plan = reconcile([], [asleep, gone], frozenset(), NOW, RLIMITS)
    assert plan.fixes == (released(asleep, MachineState.ASLEEP), released(gone, MachineState.GONE))


def test_a_live_park_keeps_its_hold() -> None:
    # A parked step is `running` (its task is `waiting`): its hold is alive.
    row = view(MachineState.PARKED, since=99999, epoch=2, held_by=step_holder("s1", 1))
    plan = reconcile([machine_frame(row.id, 2)], [row], frozenset({step_holder("s1", 1)}), NOW, RLIMITS)
    assert plan.destroy == () and plan.fixes == ()


def test_a_seed_hold_is_never_judged_as_a_step_hold() -> None:
    row = view(MachineState.RUNNING, since=300, epoch=2, held_by=seed_holder(uuid.uuid4()))
    plan = reconcile([machine_frame(row.id, 2)], [row], frozenset(), NOW, RLIMITS)
    assert plan.fixes == ()


def test_a_hold_that_changed_within_fresh_s_is_left_alone() -> None:
    # Its attempt may have started after `running_attempts` was read.
    row = view(MachineState.RUNNING, since=5, epoch=2, held_by=step_holder("s1", 1))
    asleep = view(MachineState.ASLEEP, since=5, held_by=step_holder("s2", 1), has_checkpoint=True)
    plan = reconcile([machine_frame(row.id, 2)], [row, asleep], frozenset(), NOW, RLIMITS)
    assert plan.fixes == ()


def test_a_dead_hold_whose_frame_is_gone_gets_the_lost_fix_only() -> None:
    # Hold kept this pass; the next pass (the row now asleep) lets it go.
    row = view(MachineState.RUNNING, since=300, epoch=6, held_by=step_holder("s1", 1), has_checkpoint=True)
    plan = reconcile([], [row], frozenset(), NOW, RLIMITS)
    assert plan.fixes == (RowFix.of(row, MachineState.ASLEEP),)


def test_a_waking_or_sleeping_row_is_never_released_as_a_dead_hold() -> None:
    waking = view(MachineState.WAKING, since=300, held_by=step_holder("s1", 1))
    sleeping = view(MachineState.SLEEPING, since=300, held_by=step_holder("s2", 1))
    plan = reconcile([], [waking, sleeping], frozenset(), NOW, RLIMITS)
    assert plan.fixes == ()


def test_a_seed_hold_lives_exactly_as_long_as_its_seed_job() -> None:
    """A seed cancelled mid-copy never comes back to thaw and release its
    parent. A seed copying a large /work slowly is still queued or in flight - its hold is live
    past `2 * stuck_s` (a clock-only rule thawed a parent under a live copy)."""
    copying, cancelled = uuid.uuid4(), uuid.uuid4()
    alive = view(MachineState.RUNNING, since=1800, epoch=3, held_by=seed_holder(copying))
    dead = view(MachineState.RUNNING, since=1800, epoch=3, held_by=seed_holder(cancelled))
    frames = [machine_frame(alive.id, 3), machine_frame(dead.id, 3)]
    plan = reconcile(frames, [alive, dead], frozenset(), NOW, RLIMITS, seeding_jobs=frozenset({copying}))
    assert plan.destroy == ()
    assert plan.fixes == (released(dead, MachineState.IDLE, thaw=True),)
    # A caller that cannot tell (the listing failed, or DBOS is not up) releases no seed's hold.
    assert reconcile(frames, [alive, dead], frozenset(), NOW, RLIMITS, seeding_jobs=None).fixes == ()


def test_a_seed_still_listed_past_a_whole_run_is_an_orphan() -> None:
    """A seed holds its parent for one copy. A run still listed after longer than any step may run
    (`throwaway_max_s`) is one no executor will resume (a rolling deploy's old version): its hold
    on another task's Machine is released, listing or not."""
    child = uuid.uuid4()
    orphan = view(MachineState.RUNNING, since=2401, epoch=3, held_by=seed_holder(child))
    for seeding in (frozenset({child}), None):
        plan = reconcile(
            [machine_frame(orphan.id, 3)], [orphan], frozenset(), NOW, RLIMITS, seeding_jobs=seeding
        )
        assert plan.fixes == (released(orphan, MachineState.IDLE, thaw=True),)


def test_a_dead_seed_hold_whose_frame_is_gone_gets_the_lost_fix_then_lets_go() -> None:
    held = seed_holder(uuid.uuid4())
    running = view(MachineState.RUNNING, since=300, epoch=3, held_by=held, has_checkpoint=True)
    assert reconcile([], [running], frozenset(), NOW, RLIMITS, seeding_jobs=frozenset()).fixes == (
        RowFix.of(running, MachineState.ASLEEP),
    )
    # The next pass finds it asleep, still held by the seed nobody will resume.
    asleep = view(MachineState.ASLEEP, since=300, epoch=3, held_by=held, has_checkpoint=True)
    young = view(MachineState.ASLEEP, since=5, epoch=3, held_by=held, has_checkpoint=True)
    plan = reconcile([], [asleep, young], frozenset(), NOW, RLIMITS, seeding_jobs=frozenset())
    assert plan.fixes == (released(asleep, MachineState.ASLEEP),)


def test_segment_seconds_never_goes_negative_and_starts_at_zero() -> None:
    assert segment_seconds(None, NOW) == 0
    assert segment_seconds(ago(90.9), NOW) == 90
    assert segment_seconds(NOW + dt.timedelta(seconds=5), NOW) == 0


def test_cost_is_per_profile_per_minute_with_base_as_the_fallback() -> None:
    prices = parse_prices("base=0.0005,browser=0.00075")
    assert prices == {"base": Decimal("0.0005"), "browser": Decimal("0.00075")}
    assert segment_cost_usd(120, "browser", prices) == Decimal("0.001500")
    assert segment_cost_usd(60, "code", prices) == Decimal("0.000500")  # unknown profile → base
    assert segment_cost_usd(0, "base", prices) == Decimal("0.000000")


def test_parse_resources_reads_the_profile_grammar() -> None:
    parsed = parse_resources("base=2cpu/2048mb/512pids,browser=1.5cpu/3072mb/256pids")
    assert parsed["base"] == Resources(cpus=2.0, mem_mb=2048, pids=512)
    assert parsed["browser"] == Resources(cpus=1.5, mem_mb=3072, pids=256)


def test_parse_rfc3339_takes_nanoseconds_and_a_z() -> None:
    parsed = parse_rfc3339("2026-09-30T12:34:56.123456789Z")
    assert parsed == dt.datetime(2026, 9, 30, 12, 34, 56, 123456, tzinfo=dt.UTC)
    assert parse_rfc3339("2026-09-30T12:34:56Z").microsecond == 0


def test_retry_note_names_the_attempt_and_caps_the_listing() -> None:
    changed = [f"/work/f{i}.txt" for i in range(RETRY_LIST_CAP + 7)]
    note = retry_note(3, changed)
    assert "attempt 3" in note and "same Machine" in note
    assert "/work/f0.txt" in note and f"/work/f{RETRY_LIST_CAP - 1}.txt" in note
    assert f"/work/f{RETRY_LIST_CAP}.txt" not in note
    assert "7 more" in note


def test_retry_note_says_so_when_nothing_changed() -> None:
    assert "changed no files" in retry_note(2, [])


def test_a_natively_asleep_machines_computer_is_never_destroyed() -> None:
    """Spec A2: a row asleep natively owns its provider computer - listed as owned, never destroyed
    - and still owns it once a wake has moved the row past the computer's labelled epoch."""
    asleep = view(MachineState.ASLEEP, since=900, epoch=3, has_checkpoint=True)
    computer = machine_frame(asleep.id, 3)
    computer = replace(computer, driver="remote")
    asleep = replace(asleep, frame_ref=computer.ref, driver="remote")
    woken = view(MachineState.RUNNING, since=900, epoch=5, held_by="step:s:1")
    resumed = replace(machine_frame(woken.id, 2), driver="remote")  # made at epoch 2, slept and resumed since
    woken = replace(woken, frame_ref=resumed.ref, driver="remote")
    plan = reconcile([computer, resumed], [asleep, woken], frozenset({"step:s:1"}), NOW, RLIMITS)
    assert plan.destroy == () and plan.fixes == ()


def test_an_archive_sleepers_frame_and_a_gone_rows_named_frame_still_go() -> None:
    """Naming is ownership only for a warm or an asleep row: an archive sleeper names nothing, and
    a gone row (expired, reset) keeps no computer."""
    asleep = view(MachineState.ASLEEP, since=900, epoch=3, has_checkpoint=True)  # slept by archive
    gone = view(MachineState.GONE, since=900, epoch=4)
    left = machine_frame(gone.id, 4)
    gone = replace(gone, frame_ref=left.ref)
    plan = reconcile([machine_frame(asleep.id, 3), left], [asleep, gone], frozenset(), NOW, RLIMITS)
    assert set(plan.destroy) == {f"machine-{asleep.id}-3", left.ref}


def _native(state: MachineState, *, epoch: int = 3, **kw: object) -> tuple[MachineView, FrameInfo]:
    """A row on a natively sleeping driver (`remote`) naming its frame (made at epoch 1), and that frame
    as listed."""
    row = view(state, since=900, epoch=epoch, has_checkpoint=True, **kw)  # pyright: ignore[reportArgumentType]
    frame = replace(machine_frame(row.id, 1), driver="remote")
    return replace(row, frame_ref=frame.ref, driver="remote"), frame


def test_a_natively_asleep_computer_is_kept_only_on_its_rows_driver() -> None:
    """A ref on two drivers is two frames: the row's name keeps the one on ITS driver, and the same
    ref listed by another driver is disowned."""
    row, mine = _native(MachineState.ASLEEP)
    other = replace(mine, driver="docker")
    plan = reconcile([mine, other], [row], frozenset(), NOW, RLIMITS)
    assert plan.disowned == (other,) and plan.fixes == ()


def test_a_natively_asleep_row_naming_a_different_frame_disowns_the_old_one() -> None:
    row, old = _native(MachineState.ASLEEP)
    new = replace(machine_frame(row.id, 2), driver="remote")
    row = replace(row, frame_ref=new.ref)
    plan = reconcile([old, new], [row], frozenset(), NOW, RLIMITS)
    assert plan.disowned == (old,) and plan.fixes == ()


def test_pin_a_waking_row_with_a_frame_ref_keeps_its_frame_unless_the_wake_is_stuck() -> None:
    """PIN of the current rules (no behaviour change here): WAKING is a warm state, so the name keeps
    the frame; a STUCK wake's frame goes (it holds nothing its checkpoint does not) and the row falls back."""
    row, frame = _native(MachineState.WAKING, held_by="step:s:1")
    plan = reconcile([frame], [row], frozenset({"step:s:1"}), NOW, RLIMITS)
    assert plan.disowned == ()
    stuck = replace(row, state_since=ago(100_000))
    plan = reconcile([frame], [stuck], frozenset(), NOW, RLIMITS)
    assert plan.disowned == (frame,) and [f.to_state for f in plan.fixes] == [MachineState.ASLEEP]


def test_pin_a_sleeping_row_naming_a_frame_keeps_it() -> None:
    """PIN of the current rules (no behaviour change here): SLEEPING is a warm state, so the name
    keeps the frame while the sleep runs (its job is the sleep that suspends it)."""
    row, frame = _native(MachineState.SLEEPING)
    plan = reconcile([frame], [row], frozenset(), NOW, RLIMITS)
    assert plan.disowned == ()


def test_a_row_whose_driver_did_not_answer_is_left_for_the_next_pass() -> None:
    """A listing is per driver. A row naming a frame on a
    driver that did not answer is neither lost nor let go this pass - nobody looked - and neither is
    any frame-bearing row while a driver is silent (the stale-stamp residual); a row naming no frame
    is judged as before."""
    away = replace(
        view(MachineState.IDLE, since=300, has_checkpoint=True), frame_ref="machine-a-3", driver="sandbar"
    )
    held_away = replace(
        view(MachineState.IDLE, since=300, held_by="step:gone:1"), frame_ref="machine-b-3", driver="sandbar"
    )
    here = replace(
        view(MachineState.IDLE, since=300, has_checkpoint=True), frame_ref="machine-c-3", driver="docker"
    )
    stuck = replace(view(MachineState.WAKING, since=100_000, has_checkpoint=True), driver="sandbar")
    rows = [away, held_away, here, stuck]
    plan = reconcile([], rows, frozenset(), NOW, RLIMITS, answered=frozenset({"docker"}))
    assert set(plan.fixes) == {RowFix.of(stuck, MachineState.ASLEEP)}  # a silent driver: no frame-bearing row
    everyone = reconcile([], rows, frozenset(), NOW, RLIMITS)  # None: every driver answered
    assert {f.machine_id for f in everyone.fixes} == {r.id for r in rows}


def test_a_frame_bearing_row_is_judged_lost_when_every_driver_answered() -> None:
    row = replace(
        view(MachineState.IDLE, since=300, has_checkpoint=True), frame_ref="machine-a-3", driver="docker"
    )
    both = frozenset({"docker", "sandbar"})
    plan = reconcile([], [row], frozenset(), NOW, RLIMITS, answered=both, asked=both)
    assert plan.fixes == (RowFix.of(row, MachineState.ASLEEP),)


def test_a_stale_row_whose_frame_is_on_a_silent_driver_is_untouched_then_re_stamped() -> None:
    """Residual of the final re-review: the row names `docker` (answering) while its frame runs on
    `sandbar` (silent this pass). Pass 1 must not judge it lost; pass 2, with sandbar answering,
    re-stamps it and disowns nothing."""
    row, frame = _stale(MachineState.IDLE, has_checkpoint=True)
    both = frozenset({"docker", "sandbar"})
    first = reconcile([], [row], frozenset(), NOW, RLIMITS, answered=frozenset({"docker"}), asked=both)
    assert first.fixes == () and first.disowned == ()
    second = reconcile([frame], [row], frozenset(), NOW, RLIMITS, answered=both, asked=both)
    assert second.disowned == ()
    assert second.fixes == (RowFix.of(row, MachineState.IDLE, keep_frame=True, driver="sandbar"),)


def test_a_silent_driver_pauses_only_the_fixes_that_drop_a_frame() -> None:
    """A dead hold on a row whose frame is listed is released (keep_frame) while a driver is silent,
    so one down driver never blocks steps on every Machine; a row whose frame is listed nowhere is
    still not judged lost."""
    held = replace(view(MachineState.RUNNING, since=900, epoch=3, held_by="step:gone:1"), driver="docker")
    frame = machine_frame(held.id, 3)
    held = replace(held, frame_ref=frame.ref)
    stale, _ = _stale(MachineState.IDLE, has_checkpoint=True)
    both = frozenset({"docker", "sandbar"})
    plan = reconcile(
        [frame], [held, stale], frozenset(), NOW, RLIMITS, answered=frozenset({"docker"}), asked=both
    )
    assert [f.machine_id for f in plan.fixes] == [held.id]
    assert plan.fixes[0].keep_frame and plan.fixes[0].release_hold
    assert plan.disowned == ()


def test_a_frame_bearing_row_naming_a_driver_the_pass_never_asked_is_not_judged_lost() -> None:
    """Silence is judged over the asked drivers plus the ones frame-bearing rows name: this row
    names `sandbar`, which nobody listed, so nobody looked at its frame."""
    row = replace(
        view(MachineState.IDLE, since=300, has_checkpoint=True), frame_ref="machine-a-3", driver="sandbar"
    )
    only = frozenset({"docker"})
    plan = reconcile([], [row], frozenset(), NOW, RLIMITS, answered=only, asked=only)
    assert plan.fixes == () and plan.disowned == ()


def test_a_stuck_sleeps_return_to_warm_still_runs_while_a_driver_is_silent() -> None:
    idle = view(MachineState.SLEEPING, since=700, epoch=9, has_checkpoint=True)
    frame = machine_frame(idle.id, 9)
    idle = replace(idle, frame_ref=frame.ref, driver="docker")
    both = frozenset({"docker", "sandbar"})
    plan = reconcile(
        [frame], [idle], frozenset(), NOW, RLIMITS, sleeping_jobs=frozenset(),
        answered=frozenset({"docker"}), asked=both,
    )  # fmt: skip
    assert plan.fixes == (RowFix.of(idle, MachineState.IDLE, keep_frame=True, thaw=True),)


def _stale(state: MachineState, **kw: object) -> tuple[MachineView, FrameInfo]:
    """A row stamped `docker` naming its frame, and that frame as `sandbar` lists it: a fork the API
    stamped with its own default, woken on a sandbar worker."""
    row = view(state, since=900, epoch=3, **kw)  # pyright: ignore[reportArgumentType]
    frame = replace(machine_frame(row.id, 3), driver="sandbar")
    return replace(row, frame_ref=frame.ref, driver="docker"), frame


def test_a_stale_driver_stamp_is_re_stamped_to_where_its_frame_is_listed() -> None:
    """An answering driver lists the frame a row names, with that Machine's
    labels, while the row names another driver - the row is re-stamped to the listing driver,
    never destroyed and never lost, whether or not its stale driver answered. The re-stamp is the
    row's only fix this pass (a dead hold waits for the next, judged on the right driver)."""
    row, frame = _stale(MachineState.IDLE)
    for answered in (None, frozenset({"sandbar"}), frozenset({"docker", "sandbar"})):
        plan = reconcile([frame], [row], frozenset(), NOW, RLIMITS, answered=answered)
        assert plan.disowned == ()
        assert plan.fixes == (RowFix.of(row, MachineState.IDLE, keep_frame=True, driver="sandbar"),)
    held = replace(row, held_by="step:gone:1")
    plan = reconcile([frame], [held], frozenset(), NOW, RLIMITS)
    assert plan.fixes == (RowFix.of(held, MachineState.IDLE, keep_frame=True, driver="sandbar"),)


def test_a_natively_asleep_computer_on_another_driver_is_re_stamped_not_destroyed() -> None:
    row, computer = _native(MachineState.ASLEEP)
    stale = replace(row, driver="docker")
    plan = reconcile([computer], [stale], frozenset(), NOW, RLIMITS)
    assert plan.disowned == ()
    assert plan.fixes == (RowFix.of(stale, MachineState.ASLEEP, keep_frame=True, driver="remote"),)


def test_a_stamp_stands_without_evidence_against_it() -> None:
    """No re-stamp when the row's own driver lists the frame too, when the frame on the other
    driver wears another Machine's labels, or when the row names no frame (an archive sleeper:
    its old epoch's frame on another driver is disowned as ever)."""
    row, frame = _stale(MachineState.IDLE)
    also_mine = replace(frame, driver="docker")
    plan = reconcile([frame, also_mine], [row], frozenset(), NOW, RLIMITS)
    assert plan.fixes == () and plan.disowned == ()
    someone_elses = replace(frame, labels={**frame.labels, LABEL_MACHINE: str(uuid.uuid4())})
    plan = reconcile([someone_elses], [row], frozenset(), NOW, RLIMITS)
    assert all(f.driver is None for f in plan.fixes) and plan.disowned == (someone_elses,)
    asleep = replace(view(MachineState.ASLEEP, since=900, epoch=3, has_checkpoint=True), driver="docker")
    left = replace(machine_frame(asleep.id, 3), driver="sandbar")
    plan = reconcile([left], [asleep], frozenset(), NOW, RLIMITS)
    assert plan.fixes == () and plan.disowned == (left,)
