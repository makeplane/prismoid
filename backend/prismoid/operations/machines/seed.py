# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Fork seeds (ADR-063): a fork starts from its parent's `/work`, never its logins.

The child's row is born `sleeping` - "a checkpoint is being written" - so `acquire` already knows
to wait on it; the `machine_seed` job fills `machines/<child>/seed.tar.gz` and leaves the child
`asleep` on it (or `gone`, starting fresh). The seed is always a COPY in the child's own prefix,
so the parent's retention, reset or deletion never strands the fork, and only `/work` travels.
With the rollback lever pulled a fork gets no Machine at all.

The job is safe to run again (DBOS re-runs a step whose output it never recorded): a child that is
no longer the seed's is never written to, nor is the `seed.tar.gz` it may already point at; and the
parent's hold is the seed's own (`seed:<child>`), which the reconciler leaves alone while the job
is active - so whichever run finds it resumes it or lets it go, and only a hold whose job is over
(or an orphan's, past any step's run) is the reconciler's to release. The step retries
what is transient (`seed_retryable`): the parent mid-transition or held
by another fork's seed, and the object store, the frame provider or the database failing for a
moment. A bug fails the job at once, and the child waits `sleeping` for the reconciler.

One limit: the key is fixed, so two runs of one seed at the same moment - which DBOS never makes of
one step, short of a split-brain recovery - would write the same object, and the one that lands
could vouch (by hash) for the other's bytes. The child's wake then fails the hash check and rebuilds
`lost`; it never restores the wrong files.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import httpx
import logfire
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeout
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai import frames
from prismoid.conf import settings
from prismoid.db.models.machine import Machine
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope
from prismoid.operations.dispatch import get_dispatcher
from prismoid.operations.machines import store
from prismoid.operations.machines.common import db_now, locked_machine, record
from prismoid.schemas.events import MachineCreated
from prismoid.schemas.machines import (
    MachineProfile,
    MachineState,
    RebuildReason,
    held_by_seed,
    persistent,
    seed_holder,
    seed_key,
)
from prismoid.schemas.screen import is_live


class SeedNotReady(Exception):
    """The parent is waking, going to sleep, or held by another fork's seed; the step retries
    shortly."""


def seed_retryable(err: BaseException) -> bool:
    """The `machine_seed` step's `should_retry`: the parent not ready yet, and infrastructure
    failing for a moment - a frame provider's error (the drivers report one as a `RuntimeError`,
    an `OSError` or an `httpx` error), the object store's, a lost database connection. Anything
    else is a bug, and fails the job at once rather than 20 times."""
    if isinstance(err, NotImplementedError):
        return False  # a RuntimeError by class, a bug by nature
    if isinstance(err, DBAPIError):
        return err.connection_invalidated or isinstance(err, OperationalError | InterfaceError)
    return isinstance(
        err,
        SeedNotReady | RuntimeError | OSError | httpx.HTTPError | BotoCoreError | ClientError | PoolTimeout,
    )


@dataclass(frozen=True, slots=True)
class _Source:
    """What a seed copies: the parent's live frame (`ref`, held by the seed while it archives),
    or its last checkpoint (`key`), or nothing - and the fork starts fresh. `workspace_id` is the
    parent's, checked against the child's before anything lands."""

    workspace_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None
    ref: str | None = None
    profile: str = MachineProfile.BASE
    driver: str | None = None  # the parent's row's: the driver its live frame is on
    key: str | None = None
    sha256: str | None = None
    size: int = 0
    as_of: dt.datetime | None = None


async def seed_fork(session: AsyncSession, parent: Task, child: Task) -> None:
    """From `tasks.fork` once the child's row has committed: a parent with a Machine
    gives the fork one born `sleeping` at epoch 0, and `machine_seed` is enqueued after the commit.
    A replayed fork (its client token) finds the child's Machine: while it still waits on its seed
    the job is enqueued again (the id dedupes), and once it has landed nothing happens. With the
    rollback lever pulled nothing happens at all: no new Machine, a fork's included."""
    if not settings.machines.enabled:
        await session.commit()  # the caller's `child` stays readable, as on every path below
        return
    source = await Machine.for_task(session, parent.id)
    if source is not None and persistent(frames.sleep_kind(source.driver)):
        # A device Machine's `/work` is the person's own folder, never archived:
        # the fork copies none of it, and its first step makes it a Machine of its own, in
        # the folder it was given or a fresh one.
        source = None
    row = await Machine.for_task(session, child.id)
    if source is not None and row is None:
        now = await db_now(session)
        row = Machine(
            workspace_id=child.workspace_id,
            task_id=child.id,
            # The parent row's, never this process's setting: a fork runs in the API, whose
            # `PRISMOID_WORKER_FRAME_DRIVER` production leaves unset. The first wake
            # stamps the driver that made the child's own frame.
            driver=source.driver,
            profile=source.profile,
            state=MachineState.SLEEPING,
            state_since=now,
            last_active_at=now,
            epoch=0,
            egress_version=0,
            egress_hosts=[],
        )
        session.add(row)
        await session.flush()
    ids = (source.id, row.id) if source is not None and row is not None and _seeding(row) else None
    await session.commit()  # every path: a rollback would expire the `child` the caller returns
    if ids is not None:
        await get_dispatcher().enqueue(
            "machine_seed",
            f"machine-seed-{child.id}",
            parent_machine_id=str(ids[0]),
            child_machine_id=str(ids[1]),
        )


async def run_seed(parent_id: uuid.UUID, child_id: uuid.UUID) -> str:
    """The job's body. Returns `seeded`; `fresh` - the child goes `gone`, because
    there was nothing to copy or the copy failed for good (then its first wake says why: `lost` or
    `oversize`); or `gone` - the fork was deleted, or moved on without its seed.
    Raises `SeedNotReady` while the parent is not ready, and lets a transient failure propagate:
    the step retries both (`seed_retryable`)."""
    if not await _wanted(parent_id, child_id):
        return "gone"
    source = await _take(parent_id, child_id)
    seeded, reason = await _copy(parent_id, child_id, source)
    return await _land(child_id, source, seeded, reason)


def _seeding(child: Machine) -> bool:
    """The child is still the seed's to fill: born `sleeping` at epoch 0 and never held, or already
    marked `gone` by the reconciler's stuck rule while nobody has woken it."""
    return (
        child.epoch == 0
        and child.held_by is None
        and child.state in (MachineState.SLEEPING, MachineState.GONE)
        and child.checkpoint_object is None
    )


async def _wanted(parent_id: uuid.UUID, child_id: uuid.UUID) -> bool:
    """Is there anything for this run to do? Only while the child is still the seed's - a run
    after the seed landed must never overwrite the `seed.tar.gz` the child's row now vouches for
    by hash - or while the parent is still held by this seed (a dead attempt's hold, which the
    normal path gives back; it then finds the child moved on and keeps nothing)."""
    async with row_scope(Machine, child_id) as session:
        child = await locked_machine(session, child_id)
        if child is not None and _seeding(child):
            return True
    async with row_scope(Machine, parent_id) as session:
        parent = await locked_machine(session, parent_id)
        return parent is not None and parent.held_by == seed_holder(child_id)


async def _take(parent_id: uuid.UUID, child_id: uuid.UUID) -> _Source:
    """What to copy, by the parent's state (by state). An `idle` parent is held by the seed -
    `running`, `held_by = seed:<child>` - and its `last_active_at` is left alone: a seed is not the
    parent's activity, so its idle clock runs on. A `running` or `parked` one is never held (its
    step keeps `held_by`): the seed copies its last checkpoint. A parent that another fork's seed
    holds is not ready: that seed hands it back in seconds, and copying the last checkpoint
    meanwhile would seed this fork from nothing, or from files older than its sibling's (I1). An
    idle parent someone is driving (a live control lease) is never frozen under
    them: like a running one, it is copied from its last checkpoint, and the fork is as of that."""
    holder = seed_holder(child_id)
    async with row_scope(Machine, parent_id) as session:
        parent = await locked_machine(session, parent_id)
        if parent is None:
            return _Source()  # the parent task was deleted: nothing to copy
        held = parent.held_by
        if parent.state in (MachineState.WAKING, MachineState.SLEEPING):
            raise SeedNotReady(f"the parent Machine {parent_id} is {parent.state}")
        if held != holder and held_by_seed(held):
            raise SeedNotReady(f"the parent Machine {parent_id} is held by another fork's seed")
        live = _Source(
            workspace_id=parent.workspace_id,
            task_id=parent.task_id,
            ref=parent.frame_ref,
            profile=parent.profile,
            driver=parent.driver,
        )
        if held == holder and parent.state == MachineState.RUNNING and parent.frame_ref:
            return live  # our own hold, from an attempt whose worker died mid-copy: resume it
        if parent.state == MachineState.IDLE and parent.frame_ref:
            now = await db_now(session)
            if not is_live(parent.lease(), now):  # never frozen under someone driving it
                parent.held_by, parent.held_since = holder, now
                parent.move(MachineState.RUNNING, now)  # `last_active_at` untouched
                await session.commit()
                return live
        if held == holder:
            # Our hold, on a row the reconciler put to rest when its frame died (it keeps a hold it
            # moves, and leaves an active seed's): nobody else will ever let go of it.
            parent.held_by, parent.held_since = None, None
            await session.commit()
        if parent.checkpoint_object and parent.checkpoint_sha256:
            return _Source(
                workspace_id=parent.workspace_id,
                key=parent.checkpoint_object,
                sha256=parent.checkpoint_sha256,
                size=parent.checkpoint_bytes or 0,
                as_of=parent.checkpoint_at,
            )
        return _Source(workspace_id=parent.workspace_id)


async def _copy(
    parent_id: uuid.UUID, child_id: uuid.UUID, source: _Source
) -> tuple[store.Stored | None, RebuildReason | None]:
    """The slow part, outside any transaction: the seed, or none and why (None when there was
    simply nothing to copy). Only a dead frame (`lost`), a `/work` over the cap (`oversize`) or a
    checkpoint that is really gone (`lost`) lands the child fresh; anything else propagates for the
    step to retry. A live copy gives the parent back first, whatever happens.
    Nothing here deletes the child's `seed.tar.gz` (M2): a failure uploaded nothing."""
    key = seed_key(child_id)
    if source.ref is not None:
        try:
            stored = await _archive(source, key)
        except store.Oversize:
            logfire.warn(
                "machine seed {child}: the parent's /work is over the cap; the fork starts fresh",
                child=str(child_id),
            )
            return None, RebuildReason.OVERSIZE
        finally:
            await _release_parent(parent_id, child_id)
        if stored is None:
            logfire.warn(
                "machine seed {child}: the parent's frame is gone; the fork starts fresh", child=str(child_id)
            )
            return None, RebuildReason.LOST
        return stored, None
    if source.key is not None and source.sha256 is not None:
        try:
            await store.copy(source.key, key)
        except store.CheckpointLost:
            if await _checkpoint_of(parent_id) != source.key:
                # The parent slept again since `_take`, and its new sleep deleted the checkpoint we
                # chose: a race, not a loss - decide again (M3). The same key still missing is lost.
                raise SeedNotReady(f"the parent Machine {parent_id} replaced its checkpoint") from None
            return None, RebuildReason.LOST
        return store.Stored(key=key, sha256=source.sha256, size=source.size, raw_bytes=0), None
    return None, None


async def _archive(source: _Source, key: str) -> store.Stored | None:
    """Freeze the parent's frame, archive its `/work` into the child's seed, thaw it. None when the
    frame is gone (a driver's `bind` raises RuntimeError for a missing frame, as `sleep` reads it).
    A freeze that fails partway is thawed all the same, and a thaw that fails after a good upload
    is logged and the seed kept (M1)."""
    frame = frames.new_frame(
        str(source.task_id), driver=source.driver, profile=source.profile, name=source.ref
    )
    try:
        await frame.bind()
    except RuntimeError:
        return None
    try:
        await frame.freeze()
        return await store.save_work(frame, key, settings.machines.checkpoint_max_bytes)
    finally:
        try:
            await frame.thaw()
        except Exception:
            logfire.warn("machine seed: the parent's frame {ref} did not thaw", ref=str(source.ref))


async def _checkpoint_of(parent_id: uuid.UUID) -> str | None:
    async with row_scope(Machine, parent_id) as session:
        parent = await locked_machine(session, parent_id)
        return parent.checkpoint_object if parent is not None else None


async def _land(
    child_id: uuid.UUID, source: _Source, seeded: store.Stored | None, reason: RebuildReason | None
) -> str:
    """The child's one write, a compare-and-set on `_seeding`: `asleep` on its seed, or `gone`
    with the reason its first wake records. A child someone else moved on is left alone, and the
    seed this run uploaded is deleted - unless the row already points at that key. A child in
    another workspace than the parent's lands `gone` with nothing kept (M4)."""
    stray: store.Stored | None = None
    async with row_scope(Machine, child_id) as session:
        child = await locked_machine(session, child_id)
        if child is None or not _seeding(child):
            referenced = child is not None and child.checkpoint_object == seed_key(child_id)
            await session.rollback()  # let go of the row before the slow part
            if seeded is not None and not referenced:
                await store.delete_keys([seeded.key])
            return "gone"
        if source.workspace_id is not None and source.workspace_id != child.workspace_id:
            logfire.error(
                "machine seed {child}: the parent's Machine is in another workspace; nothing is kept",
                child=str(child_id),
            )
            stray, seeded, reason = seeded, None, None
        now = await db_now(session)
        if seeded is not None:
            child.gone_reason = None  # the reconciler's stuck rule may have called it lost meanwhile
            child.checkpoint_object, child.checkpoint_sha256 = seeded.key, seeded.sha256
            child.checkpoint_bytes = seeded.size
            child.checkpoint_at = source.as_of or now
            child.move(MachineState.ASLEEP, now)
        else:
            child.gone_reason = reason
            child.move(MachineState.GONE, now)
        await record(
            session, child.task_id, MachineCreated, profile=child.profile, seeded_as_of=child.checkpoint_at
        )
        await session.commit()
    if stray is not None:
        await store.delete_keys([stray.key])
    return "seeded" if seeded is not None else "fresh"


async def _release_parent(parent_id: uuid.UUID, child_id: uuid.UUID) -> None:
    """Give the parent back if this seed holds it: unheld, and `idle` again if it is still `running`
    (the reconciler may have put it to rest meanwhile). `last_active_at` stays where it was."""
    async with row_scope(Machine, parent_id) as session:
        parent = await locked_machine(session, parent_id)
        if parent is None or parent.held_by != seed_holder(child_id):
            return
        parent.held_by, parent.held_since = None, None
        if parent.state == MachineState.RUNNING:
            parent.move(MachineState.IDLE, await db_now(session))
        await session.commit()
