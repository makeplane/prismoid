# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a Machine's life adds on the device: the grants told to a
frame on the user's own disk, a kept frame cleared after a Reset, the command a crash cut off, and
the park that keeps a step's Machine while it waits for its device. A cloud frame takes none of it.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai import frames
from prismoid.ai.frames.driver import KeepsJournal, TakesGrants
from prismoid.db.models import tenant
from prismoid.db.models.device_grant import DeviceGrant
from prismoid.db.models.machine import Machine
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations.machines.common import RowBusy, db_now, lock_briefly
from prismoid.schemas.device import InterruptedOut
from prismoid.schemas.machines import (
    LABEL_KIND,
    FrameKind,
    MachineState,
    holder_step,
    machine_label,
    step_holder,
)


async def tell_grants(session: AsyncSession, task: Task, frame: frames.FrameDriver) -> None:
    """A frame on the user's own disk is told the task's grants (folders, Docker) before its create
    or bind, as Postgres holds them now: a grant added or revoked since
    the last step reaches the frame here. Its own read, committed. A cloud frame takes none."""
    if not isinstance(frame, TakesGrants):
        return
    from prismoid.operations import device_grants  # cycle: grants ask this package for a task's driver

    folders, docker = await device_grants.grants_for(session, task.id)
    await session.commit()
    frame.grant(folders, docker)


def grant_fence(
    task_id: uuid.UUID, frame: frames.FrameDriver, *, seat: bool = False
) -> Callable[[], Awaitable[None]] | None:
    """**A grant changes under a running step**: what the step's Machine
    frame asks before each tool dispatch (a steering safe point), so a revoked folder is gone before
    the next tool runs. One indexed read on its own short session; only when the task's grants moved
    are they read, and the helper told (`bind` pushes what differs, and a first ask of the attempt
    compares, a revoke having landed since `acquire`). No row lock, so the Machine's lock order is
    not in play, and no transaction waits on the helper. A push that fails raises: the tool does not
    run on a grant that may be gone, and the ladder reads the helper's silence as for any tool.
    None for a frame that holds no grants, every cloud one and a seat's throwaway one: nothing is
    asked."""
    if seat or not isinstance(frame, TakesGrants):
        return None
    from prismoid.operations import device_grants  # cycle: grants ask this package for a task's driver

    pushing = asyncio.Lock()  # a parallel batch asks at once: one pushes, the rest find it done
    pushed: tuple[int, int] | None = None

    async def push_if_changed() -> None:
        nonlocal pushed
        async with pushing:
            async with row_scope(Task, task_id) as session:
                version = await DeviceGrant.version_of(session, task_id)
                if version == pushed:
                    return
                folders, docker = await device_grants.grants_for(session, task_id)
            frame.grant(folders, docker)
            await frame.bind()
            pushed = version

    return push_if_changed


async def clear_gone(machine_id: uuid.UUID, driver: str) -> None:
    """Destroy every frame `driver` lists wearing this Machine's labels, before a wake from `gone`
    makes one: nothing the Machine had survives a Reset. A driver that cannot list or destroy raises
    (`FrameUnreachable`, a refusal), so the wake fails before it creates over the old frame."""
    for info in await frames.inventory([driver]):
        named = machine_label(info) if info.labels.get(LABEL_KIND) == FrameKind.MACHINE else None
        if named is not None and named[0] == machine_id:
            await frames.destroy_ref(info.ref, driver=driver)


async def interrupted(session: AsyncSession, task_id: uuid.UUID) -> InterruptedOut | None:
    """The command a crash cut off in the task's Machine: its provider's
    journal names the last one started that never finished and nobody parked or resumed on. None
    when there is none, or the Machine's frame keeps no journal (every cloud driver). Raises what
    the provider does: `FrameUnreachable` when the helper is away, `RuntimeError` when it has never
    heard of the frame. The row is read on the caller's session, committed before the provider is
    asked: no transaction waits on a helper."""
    frame = await _journal_of(session, task_id)
    return await frame.interrupted() if frame is not None else None


async def ack_interrupted(session: AsyncSession, task_id: uuid.UUID, exec_id: str) -> None:
    """The step parked (or resumed) on the cut-off command `exec_id`, and Postgres holds what it
    was: the journal stops naming it, and every older entry still open with it. Called only after
    that record is committed, so a crash before this re-parks on the same evidence instead of
    losing it."""
    frame = await _journal_of(session, task_id)
    if frame is not None:
        await frame.ack_interrupted(exec_id)


async def hold_parked(task_id: uuid.UUID, step_id: uuid.UUID, attempt: int) -> None:
    """A step that waits for its device keeps the task's Machine the
    way a gate park does: `parked`, held by the step, so the idle sleep's ten minutes never stop
    what the cut-off command left running - the park hold's own rule applies instead. The Machine
    as a dead run left it (running, held by this step) or as a failed acquire gave it back (idle,
    held by nobody) is parked; any other is left as it is - asleep has no frame running, and a
    Machine another step holds is not this step's. On its own session, the Machine row first (the
    lock order), waiting at most `LOCK_WAIT_S`: a busy row is left to the next park or the
    reconciler."""
    async with row_scope(Task, task_id) as session:
        if tenant.bound_workspace(session) is None:
            return
        try:
            row = await lock_briefly(session, lambda: Machine.locked_for_task(session, task_id))
        except RowBusy:
            return
        held = holder_step(row.held_by) if row is not None else None
        now = await db_now(session)
        if (
            row is not None
            and row.state == MachineState.RUNNING
            and held is not None
            and held[0] == str(step_id)
        ):
            row.move(MachineState.PARKED, now)
        elif row is not None and row.state == MachineState.IDLE and row.held_by is None:
            row.held_by, row.held_since = step_holder(str(step_id), attempt), now
            row.move(MachineState.PARKED, now)
        await session.commit()


async def _journal_of(session: AsyncSession, task_id: uuid.UUID) -> KeepsJournal | None:
    row = await Machine.for_task(session, task_id)
    named = (row.driver, row.profile, row.frame_ref) if row is not None and row.frame_ref else None
    await session.commit()
    if named is None:
        return None
    driver, profile, ref = named
    frame = frames.new_frame(str(task_id), driver=driver, profile=profile, name=ref)
    if not isinstance(frame, KeepsJournal):
        return None
    await frame.bind()
    return frame
