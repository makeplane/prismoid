# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Machine's jobs (ADR-063). Each is one effect around one operation (the sleep is
two: its claim, then its checkpoint): the operations compare-and-set on (state, epoch), and the
deterministic workflow id dedupes a double dispatch - `machine-sleep-{id}-{epoch}~{claim or tick}`
(scoped to one claim, so a failed sleep never uses up the id a retry needs),
`machine-reset-{id}-{epoch}`, `machine-seed-{child}`, `machine-expire-{id}-{epoch}~{tick}` (a
failed expiry is offered again next tick), `machine-lease-{id}-{since}~{tick}` (a control lease the
sweep found run out; tick-scoped like the expiry, and `end_lease` compares-and-sets on `since`),
`machine-forget-{id}`, `machine-boot-{task}` (the early boot), `machine-browser-stop-{id}-{epoch}`
(the kill switch stops a warm browser
Machine's Chromium; once per epoch, never per tick - see the job), `machine-egress-{id}-{version}`
(a control lease moved an idle Machine's egress: one push per version).

Three are scheduled: the sweep every `sweep_interval_s`, the reconciler every
`reconcile_interval_s` (and once at worker boot), and the orphan arm daily. They are the Rule-5
exception the sweeper documents (`jobs/sweeper.py`, `sweep_orphans`): a sweep must read the
CURRENT world on every pass, so its reads are its body, never journaled into steps a replay would
act on (the one exception: the reconciler's listings of active sleeps and seeds, which DBOS
records itself). Each is idempotent by construction - deterministic job ids, and every write a
compare-and-set on its row - and best-effort: a failed pass is logged and costs that pass, never
the schedule."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

import logfire

from prismoid.conf import settings
from prismoid.jobs import durable
from prismoid.schemas.machines import SleepReason, sleep_job_machine


@durable.job("machine_sleep")
async def machine_sleep(
    machine_id: str,
    epoch: int,
    reason: str,
    actor_id: str | None = None,
    claimed: bool = False,
    claim_at: str | None = None,
    after_s: int | None = None,
    since: str | None = None,
) -> str:
    """Two steps, so a worker that dies between them resumes at the slow part, claimed: the claim
    (skipped when Sleep now already claimed the row in its request and passed the claim here), then
    the checkpoint, which runs only while that claim - by its identity, `claim_at` - holds the row.
    One step for both would re-run the claim on recovery, find the row `sleeping` and skip,
    stranding it."""
    if claimed:
        if claim_at is None or after_s is None or since is None:
            return "skipped"  # a claim without its identity cannot prove the row is still its
        claim: dict[str, Any] | None = {"claim_at": claim_at, "after_s": after_s, "since": since}
    else:
        claim = await _claim(machine_id, epoch)
    if claim is None:
        return "skipped"
    return await _sleep(machine_id, epoch, reason, actor_id, claim)


@durable.effect()
async def _claim(machine_id: str, epoch: int) -> dict[str, Any] | None:
    """The claim's plain values (its step output is recorded), or None: not ours to sleep."""
    from prismoid.operations import machines

    claim = await machines.claim_sleep(UUID(machine_id), epoch)
    return claim.wire() if claim is not None else None


@durable.effect()
async def _sleep(
    machine_id: str, epoch: int, reason: str, actor_id: str | None, claim: dict[str, Any]
) -> str:
    from prismoid.operations import machines

    return await machines.retire_or_sleep(
        UUID(machine_id),
        epoch,
        SleepReason(reason),
        actor_id=UUID(actor_id) if actor_id else None,
        claim=machines.SleepClaim.from_wire(**claim),
    )


@durable.job("machine_reset")
async def machine_reset(
    machine_id: str, frame_ref: str | None, keys: list[str], driver: str | None = None
) -> None:
    """`driver` holds the old frame (the driver that made it). A job enqueued before it existed
    carries none, and cleans up on this worker's driver, as it always did."""
    await _reset(frame_ref, keys, driver, machine_id)


def _reset_retries(err: BaseException) -> bool:
    from prismoid.operations import machines

    return machines.reset_retryable(err)


# A persistent frame's cleanup raises while its driver does not answer (the device's helper is
# away): the step asks again until it does, and nothing else retries - a cloud cleanup stays the
# one best-effort attempt it was. Retries spent, the frame waits for the next wake's own destroy.
@durable.effect(attempts=20, interval_s=3.0, backoff=1.5, retry_if=_reset_retries)
async def _reset(
    frame_ref: str | None, keys: list[str], driver: str | None = None, machine_id: str | None = None
) -> None:
    from prismoid.operations import machines

    await machines.cleanup(frame_ref, keys, driver, UUID(machine_id) if machine_id else None)


@durable.job("machine_expire")
async def machine_expire(machine_id: str, epoch: int) -> str:
    return await _expire(machine_id, epoch)


@durable.effect()
async def _expire(machine_id: str, epoch: int) -> str:
    from prismoid.operations import machines

    return await machines.expire(UUID(machine_id), epoch)


@durable.job("machine_release")
async def machine_release(machine_id: str, epoch: int) -> str:
    return await _release(machine_id, epoch)


@durable.effect()
async def _release(machine_id: str, epoch: int) -> str:
    from prismoid.operations import machines

    return await machines.release_asleep(UUID(machine_id), epoch)


@durable.job("machine_lease_end")
async def machine_lease_end(machine_id: str, since: str, reason: str = "") -> bool:
    """End a control lease the sweep found run out - or, with `reason`, one it
    must end whatever its clock says (the kill switch: `disabled`). `end_lease` compares-and-
    sets on the lease's `since`, so a lease renewed, claimed afresh or already ended is left alone,
    and the next tick's duplicate changes nothing. When the ending narrowed an idle Machine's
    egress, the frame is told by a second effect here: a step cannot start the `machine_egress`
    job."""
    ended = await _lease_end(machine_id, since, reason)
    if ended["egress"] is not None:
        await _egress(machine_id, ended["egress"])
    return bool(ended["ended"])


@durable.effect()
async def _lease_end(machine_id: str, since: str, reason: str) -> dict[str, Any]:
    """The ending, as plain values (its step output is recorded): whether it ended, and the
    egress version it moved an idle Machine to (None: nothing to tell its frame)."""
    from prismoid.operations import machines
    from prismoid.schemas.screen import ControlEnd

    ended = await machines.screen.end_lease(
        UUID(machine_id),
        since=dt.datetime.fromisoformat(since),
        reason=ControlEnd(reason) if reason else None,
    )
    return {"ended": ended is not None, "egress": ended.egress if ended is not None else None}


@durable.job("machine_egress")
async def machine_egress(machine_id: str, version: int) -> bool:
    """Tell an idle Machine's frame the list a control lease moved it to: the
    last step's while someone drives, nothing once they let go. One effect, pinned to `version`, so
    a duplicate or a late run never overwrites a newer list."""
    return await _egress(machine_id, version)


@durable.effect()
async def _egress(machine_id: str, version: int) -> bool:
    from prismoid.operations import machines

    return await machines.push_egress(UUID(machine_id), version)


@durable.job("machine_forget")
async def machine_forget(machine_id: str, frame_ref: str | None, driver: str | None = None) -> None:
    """A deleted task's Machine: its frame, on the driver that holds it, and every
    object under its prefix. The API enqueues it after the delete commits, because the worker owns
    all machine work; GDPR erasure will enqueue the same job."""
    await _forget(machine_id, frame_ref, driver)


@durable.effect()
async def _forget(machine_id: str, frame_ref: str | None, driver: str | None = None) -> None:
    from prismoid.operations import machines

    await machines.forget(machines.Forgotten(UUID(machine_id), frame_ref, driver))


@durable.job("machine_browser_stop")
async def machine_browser_stop(machine_id: str, epoch: int) -> str:
    """The kill switch's browser arm: stop a warm browser Machine's Chromium
    so its cookies flush. Once per Machine per epoch - `machine-browser-stop-{id}-{epoch}`, no tick:
    a shutdown that reaches an absent daemon spawns it first (`sidecar.ctl`), so repeating it every
    tick would start and kill Chromium every 30 s. The sweep offers it only for an idle or parked
    Machine, so the id is never spent on a running one."""
    return await _browser_stop(machine_id, epoch)


# The id is spent whatever the workflow's fate, so a transient failure (the row's read, the
# provider's bind) is retried here rather than by the next tick. A retry repeats only what came
# before the shutdown, which never raises.
@durable.effect(attempts=3, interval_s=2.0, backoff=2.0)
async def _browser_stop(machine_id: str, epoch: int) -> str:
    from prismoid.operations import machines

    return await machines.stop_browser(UUID(machine_id), epoch)


@durable.job("machine_boot")
async def machine_boot(task_id: str) -> str:
    """The task's first Machine, prewarmed once a committed plan may need it: `machine-boot-{task}`,
    so a task boots once. One effect: the operation compares-and-sets on the row it was born with."""
    return await _boot(task_id)


@durable.effect()
async def _boot(task_id: str) -> str:
    from prismoid.operations import machines

    return await machines.boot(UUID(task_id))


@durable.job("machine_seed")
async def machine_seed(parent_machine_id: str, child_machine_id: str) -> str:
    """A fork's Machine, filled from its parent's `/work`. The id is the child TASK's,
    `machine-seed-{child}`, so a replayed fork seeds once."""
    return await _seed(parent_machine_id, child_machine_id)


def _seed_retries(err: BaseException) -> bool:
    from prismoid.operations import machines

    return machines.seed_retryable(err)


# A parent not ready yet (`waking`, `sleeping`, held by another fork's seed) raises `SeedNotReady`,
# and a transient failure propagates: the step retries both until they settle - and nothing else
# (a bug fails at once). A seed that never lands leaves the child `sleeping` for the reconciler.
@durable.effect(attempts=20, interval_s=3.0, backoff=1.5, retry_if=_seed_retries)
async def _seed(parent_machine_id: str, child_machine_id: str) -> str:
    from prismoid.operations import machines

    return await machines.run_seed(UUID(parent_machine_id), UUID(child_machine_id))


# DBOS takes seconds FIRST in a six-field cron. Both intervals are read at import, and
# `MachineSettings` refuses one that does not divide a minute.
@durable.schedule(f"*/{settings.machines.sweep_interval_s} * * * * *")
@durable.job("machine_sweep")
async def machine_sweep(scheduled: dt.datetime, actual: dt.datetime) -> None:
    """Dispatch the sleeps, expiries and lease endings that are due, and - only
    while computer use is off - the browser stops. Their ids carry `scheduled`, except the
    browser stop's (once per epoch)."""
    from prismoid.operations import machines

    try:
        await machines.sweep_once(scheduled)
    except Exception:
        logfire.exception("machine sweep failed; the next tick tries again")


@durable.schedule(f"*/{settings.machines.reconcile_interval_s} * * * * *")
@durable.job("machine_reconcile")
async def machine_reconcile(scheduled: dt.datetime, actual: dt.datetime) -> None:
    """Destroy what Postgres disowns and fix rows that claim a frame the provider lacks."""
    from prismoid.operations import machines

    if replayed(scheduled, dt.datetime.now(dt.UTC)):
        return
    try:
        await machines.reconcile_once(sleeping_jobs=await sleeping_jobs(), seeding_jobs=await seeding_jobs())
    except Exception:
        logfire.exception("machine reconcile failed; the next pass tries again")


def replayed(scheduled: dt.datetime, now: dt.datetime) -> bool:
    """A pass DBOS recovers after a crash replays the job listings it recorded before it. Stale by
    now, they would judge a sleep or a seed that began since as dead - and release a seed's hold
    under its live copy - so the pass is skipped: a newer tick lists afresh."""
    return now - scheduled > dt.timedelta(seconds=2 * settings.machines.reconcile_interval_s)


async def sleeping_jobs() -> frozenset[UUID] | None:
    """The Machines whose `machine_sleep` job is still queued, delayed or in flight - their
    `sleeping` row is not stuck, the job (or DBOS recovery) finishes it. None when DBOS cannot say:
    the reconciler then treats every sleeping row
    as having its job."""
    listed = await _active("machine_sleep")
    if listed is None:
        return None
    return frozenset(m for run in listed if (m := sleep_job_machine(run.key)) is not None)


async def seeding_jobs() -> frozenset[UUID] | None:
    """The forks (child Machines) whose `machine_seed` job is still queued, delayed or in flight -
    the child waits `sleeping` on it, and the seed's hold on its parent (`seed:<child>`) is live:
    the job resumes or releases it. The child is in the job's own input. None
    when DBOS cannot say - nor when a listed run's input does not name its child: the reconciler
    then releases no seed's hold."""
    listed = await _active("machine_seed", load_input=True)
    if listed is None:
        return None
    try:
        # A run with no input (None) raises the TypeError below, as a malformed one does.
        children = [run.kwargs["child_machine_id"] for run in listed]  # pyright: ignore[reportOptionalSubscript]
        return frozenset(UUID(str(child)) for child in children)
    except (KeyError, TypeError, ValueError):
        logfire.warn("machine reconcile: an active seed names no child; no seed's hold is judged")
        return None


async def _active(name: str, *, load_input: bool = False) -> list[durable.Run] | None:
    """A job's queued, delayed and in-flight runs; None when the engine cannot say (before
    `durable.launch()`, or a failing system database) - never "none". In a workflow the listing is
    recorded as a step."""
    try:
        # Delayed too: DBOS 2.26 holds an enqueue with a delay there, and counts it active.
        return await durable.runs(name, states=durable.ACTIVE, load_input=load_input)
    except Exception:
        logfire.warn(
            "machine reconcile: the active {name} jobs could not be listed; none is judged", name=name
        )
        return None


@durable.schedule("17 3 * * *")  # daily, 03:17 UTC
@durable.job("machine_orphans")
async def machine_orphans(scheduled: dt.datetime, actual: dt.datetime) -> None:
    """Delete objects under `machines/` that no row references (the daily arm)."""
    from prismoid.operations import machines

    try:
        await machines.orphans_once()
    except Exception:
        logfire.exception("machine orphan arm failed; tomorrow's tries again")
