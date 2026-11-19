# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The last mile of a step's acquire on a running Machine (ADR-063): the step boundary, the egress
the row learns, and the hand-back of an attempt that failed before its step began.

Every transition is a short `FOR UPDATE` + compare-and-set on the Machine row in the step's session,
ending in a commit; a sleep it claims is enqueued after that commit (`enqueue_sleep`)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import NoReturn

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai import frames
from prismoid.ai.frames.driver import EgressUnavailable
from prismoid.ai.frames.layout import MANIFEST, STEP_MARKER, WORK
from prismoid.db.models.machine import Machine
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.task import Task
from prismoid.errors import ModelIncapable
from prismoid.operations.dispatch import get_dispatcher
from prismoid.operations.machines.acquired import DRIVING, Acquired, AcquiredKind, Evict, Retry
from prismoid.operations.machines.common import db_now
from prismoid.operations.machines.egress_push import Hold, push
from prismoid.operations.machines.sleep import SleepClaim, claim_row
from prismoid.schemas.machines import MachineProfile, MachineState, SleepReason, sleep_job_id
from prismoid.schemas.screen import is_live


async def enqueue_sleep(evict: Evict) -> None:
    """After the claim's commit: the claimed `machine_sleep` job, under an id scoped to THIS claim
    (like Sleep now's), so a sleep that failed back to warm never uses up the id the next needs."""
    await get_dispatcher().enqueue(
        "machine_sleep",
        sleep_job_id(evict.machine_id, evict.epoch, evict.claim.at),
        machine_id=str(evict.machine_id),
        epoch=evict.epoch,
        reason=str(evict.reason),
        claimed=True,
        **evict.claim.wire(),
    )


async def ready(
    session: AsyncSession,
    task: Task,
    step: PlanStep,
    frame: frames.FrameDriver,
    *,
    machine_id: uuid.UUID,
    epoch: int,
    holder: str,
    allow_domains: Sequence[str],
    resuming: bool,
    woke: bool,
    woke_from_park: bool,
    park: bool,
    label: str,
    profile: str,
) -> Acquired:
    """The last mile on a running Machine: the step boundary (a resume continues the same run, so
    it only gets its list), then the row learns the egress the frame now has. `park` is whether the
    claim FOUND the row parked (a bind) or asleep under this step's park (a wake): a failure hands
    that back, and never invents a park for a Machine that was idle (M2)."""
    changed = None
    hold = Hold(machine_id, epoch, holder)
    try:
        if resuming:
            await push(frame, sorted(allow_domains), hold=hold)
        else:  # a woken Machine has no old marker, so there is nothing to list
            changed = await _boundary(
                frame, allow_domains, retry=step.attempt > 1 and not woke, hold=hold, profile=profile
            )
    except EgressUnavailable:
        # A sandbar frame born airtight cannot be widened. Hand the Machine back with its sleep
        # claimed in the same commit (no step may re-bind it before the job runs),
        # and let acquire's loop wake it born with this list - unless someone is driving its
        # browser: then it is handed back UNclaimed, and the loop waits for the lease to end.
        claim = await _unhold(session, task, machine_id, epoch, holder, park=park, sleep=True)
        if isinstance(claim, SleepClaim):
            await enqueue_sleep(Evict(machine_id, epoch, SleepReason.UPGRADE, claim))
        waiting = claim if isinstance(claim, str) else "the Machine is being re-created with its allowlist"
        raise Retry(waiting) from None
    except BaseException as down:  # the frame died under the boundary: one infra attempt
        await let_go(
            session,
            task,
            machine_id,
            epoch,
            holder,
            err=down,
            what="the Machine could not be prepared",
            park=park,
        )
    await _save_egress(session, machine_id, epoch, holder, frame)
    return Acquired(
        frame,
        kind=AcquiredKind.MACHINE,
        label=label,
        machine_id=machine_id,
        epoch=epoch,
        holder=holder,
        woke=woke,
        woke_from_park=woke_from_park,
        changed=changed,
        task_id=task.id,
        profile=profile,
    )


async def let_go(
    session: AsyncSession,
    task: Task,
    machine_id: uuid.UUID,
    epoch: int,
    holder: str,
    *,
    err: BaseException,
    what: str,
    park: bool,
) -> NoReturn:
    """A bound Machine's attempt failed (or was cancelled) before the step began: hand the Machine
    back, then fail. An error is one infra attempt; a cancellation stays a cancellation."""
    await _unhold(session, task, machine_id, epoch, holder, park=park)
    if not isinstance(err, Exception):
        raise err
    raise ModelIncapable(f"{what}: {err}", infra=True) from err


async def _unhold(
    session: AsyncSession,
    task: Task,
    machine_id: uuid.UUID,
    epoch: int,
    holder: str,
    *,
    park: bool,
    sleep: bool = False,
) -> SleepClaim | str | None:
    """Give a just-claimed Machine back as the claim found it (running → idle, or back to `parked`
    with its hold), so a failed attempt holds nothing. With `sleep`, the sleep is claimed in the
    same commit and returned for the caller to enqueue after it - unless a live control lease is on
    the row: then nothing is claimed and `DRIVING` says why. None when the row is not
    ours."""
    row = await Machine.locked_for_task(session, task.id)
    claim: SleepClaim | str | None = None
    if (
        row is not None
        and row.id == machine_id
        and row.epoch == epoch
        and row.held_by == holder
        and row.state == MachineState.RUNNING
    ):
        now = await db_now(session)
        if park:
            row.move(MachineState.PARKED, now)
        else:
            row.held_by, row.held_since = None, None
            row.move(MachineState.IDLE, now)
        if sleep:
            lease = row.lease()
            claim = DRIVING if lease is not None and is_live(lease, now) else claim_row(row, now)
    await session.commit()
    return claim


async def _changed_files(frame: frames.FrameDriver) -> list[str] | None:
    """What the attempt changed since the marker; None when the frame cannot say."""
    out = await frame.exec(
        f"test -e {STEP_MARKER} && find {WORK} -newer {STEP_MARKER} -type f 2>/dev/null | head -n 1000"
    )
    if not out.ok:
        return None
    return [frame.paths.virtual(line) or line for line in out.stdout.splitlines() if line]


async def _boundary(
    frame: frames.FrameDriver, allow_domains: Sequence[str], *, retry: bool, hold: Hold, profile: str
) -> list[str] | None:
    """The per-step boundary on a Machine. A retry reads what the
    failed attempt changed BEFORE the marker moves. The last step's mounts go before this step's
    are made (`materialise_inputs`, `materialise_skills`): a step never reads an earlier one's
    sources, memory or skills. On a browser Machine the sidecar outlives the step and reads its
    recording folder only when it starts, so it is moved to this attempt's (`record`; the warm page
    stays) - best-effort, as every recording is evidence, never authority."""
    changed = await _changed_files(frame) if retry else None
    await frame.reset_mounts()
    await frame.exec(f"rm -f {MANIFEST}")
    await frame.write_bytes(STEP_MARKER, b"", mode=0o444, owner="system")
    if profile == MachineProfile.BROWSER:
        moved = await frame.browser({"verb": "record"})
        if not moved.get("ok"):
            logfire.warn("the sidecar kept its recording folder: {err}", err=str(moved.get("error")))
    await push(frame, sorted(allow_domains), hold=hold)
    return changed


async def _save_egress(
    session: AsyncSession, machine_id: uuid.UUID, epoch: int, holder: str, frame: frames.FrameDriver
) -> None:
    row = await Machine.locked(session, machine_id)
    if row is not None and row.epoch == epoch and row.held_by == holder:
        row.egress_version = max(row.egress_version, frame.egress_version)
        row.egress_hosts = list(frame.egress_hosts)
    await session.commit()
