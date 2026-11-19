# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Getting a Machine's allowlist onto its frame (ADR-063): the step's list, a stale push re-synced
under the hold, and a list told after a commit, pinned to its version.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import logfire

from prismoid.ai import frames
from prismoid.ai.frames.driver import EgressUnavailable
from prismoid.db.models import tenant
from prismoid.db.models.machine import Machine
from prismoid.db.session import row_scope
from prismoid.operations.machines.common import RowBusy, lock_briefly, locked_machine
from prismoid.schemas.machines import MachineState


@dataclass(frozen=True, slots=True)
class Hold:
    """Who holds a Machine (the row's epoch and holder) - what the boundary's re-sync checks
    before its second push (B7)."""

    machine_id: uuid.UUID
    epoch: int
    holder: str


async def push(frame: frames.FrameDriver, domains: list[str], *, hold: Hold | None) -> None:
    """Replace the frame's allowlist, or raise: a step never runs on a list it did not ask for. A
    frame born airtight (sandbar, no domains) refuses every push, but an empty list is already true
    there; only a list it cannot reach raises. The step holds the Machine, so its list is the truth
    (`resync`, fenced by `hold`); a push that still does not land raises, and the attempt fails as
    infra."""
    try:
        if await resync(frame, domains, hold=hold):
            return
    except EgressUnavailable:
        if domains:
            raise
        return
    raise RuntimeError("the Machine's allowlist could not be applied")


async def resync(frame: frames.FrameDriver, domains: list[str], *, hold: Hold | None) -> bool:
    """Push the holder's list; on a refusal as stale, push it once more. The transport refuses a
    version it has passed, which for a holder means a push whose row update never committed (a
    worker that died between a widen's push and its commit): the driver adopted the version the
    refusal named, so the second push lands one past it, and the row catches up after. Only the
    Machine's holder may do this - a pinned push (a hand-back's narrowing) that lost stays lost,
    since the version that beat it may be a newer holder's list. The second push lands for sure, so
    on the boundary's path it is fenced by `hold` (`_held_retry`); a caller that already holds the
    row's lock (`widen`) passes `hold=None`. Required, on `push` too: a boundary
    that dropped its hold would re-sync unfenced, with every test green."""
    if await frame.allow(domains):
        return True
    if hold is None:
        return await frame.allow(domains)
    return await _held_retry(frame, domains, hold)


async def _held_retry(frame: frames.FrameDriver, domains: list[str], hold: Hold) -> bool:
    """The boundary's second push, only while this attempt still holds the Machine (B7, 1b's
    re-review): a zombie whose hold was released never overwrites the newer holder's list. Its own
    short transaction on the Machine row alone (widen's rules M3 and M5): the row stays locked
    across the push, and a row locked past `LOCK_WAIT_S` refuses the push."""
    async with row_scope(Machine, hold.machine_id) as session:
        try:
            row = await lock_briefly(session, lambda: locked_machine(session, hold.machine_id))
        except RowBusy:
            logfire.warn(
                "machine {id}: its row stayed locked - the boundary's re-sync is refused",
                id=str(hold.machine_id),
            )
            return False
        if row is None or row.epoch != hold.epoch or row.held_by != hold.holder:
            logfire.warn(
                "machine {id}: the hold moved on - the boundary's re-sync is refused", id=str(hold.machine_id)
            )
            return False
        return await frame.allow(domains)


@dataclass(frozen=True, slots=True)
class Push:
    """A list for a Machine's frame, pinned to the row's egress version, told after the commit."""

    task_id: uuid.UUID
    profile: str
    ref: str
    hosts: list[str]
    version: int
    driver: str


async def tell(pushed: Push) -> bool:
    """Push a list to a Machine's frame by its reference, pinned to its version: a newer list
    already there refuses it (`allow`'s versions), so a late or repeated push never overwrites a
    step's list or a later lease's. A stopped frame is not started: it reaches nothing, and its next
    start forgets its list. Best-effort - False when it did not land."""
    try:
        frame = frames.new_frame(
            str(pushed.task_id), driver=pushed.driver, profile=pushed.profile, name=pushed.ref
        )
        await frame.bind(start=False)
        return await frame.allow(pushed.hosts, version=pushed.version)
    except Exception as exc:
        logfire.warn(
            "egress push to {ref} v{version} did not land ({kind})",
            ref=pushed.ref,
            version=pushed.version,
            kind=type(exc).__name__,
        )
        return False


async def push_egress(machine_id: uuid.UUID, version: int) -> bool:
    """The worker's half of a control lease that moved an idle Machine's egress
    (`Machine.stepless_egress`): a claim or an ending in the API, whose process never touches a
    frame, or one the sweep's lease-end job made. The row's list goes to the frame at
    exactly `version` - nothing when the row has moved past it (a newer lease's move or a step's
    boundary carries the frame now), when a step or a seed holds the Machine (the list is the
    step's), or when it has no warm frame. True when it landed."""
    async with row_scope(Machine, machine_id) as session:
        if tenant.bound_workspace(session) is None:
            return False  # the Machine is gone
        row = await Machine.current(session, machine_id)
        if (
            row is None
            or row.egress_version != version
            or row.held_by is not None
            or row.state != MachineState.IDLE
            or not row.frame_ref
        ):
            return False
        pushed = Push(row.task_id, row.profile, row.frame_ref, list(row.egress_hosts), version, row.driver)
    return await tell(pushed)
