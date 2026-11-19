# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Allowed sites: the owner's standing egress grants for the browser."""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.egress_allowance import EgressAllowance
from prismoid.db.models.gate import Gate
from prismoid.db.models.task import Task
from prismoid.errors import ValidationFailed
from prismoid.schemas.computer_use import check_host


async def list_allowances(session: AsyncSession, actor: Actor) -> list[EgressAllowance]:
    return await EgressAllowance.live_for_owner(session, actor.id)


async def _insert_allowance(
    session: AsyncSession,
    actor_id: uuid.UUID,
    host: str,
    source: str,
    gate_id: uuid.UUID | None,
) -> EgressAllowance:
    """Adds the row, no commit - idempotent on a live row, race-safe under any caller's own
    transaction (the egress gate's `always` accept calls this inside its own transaction, no
    commit of its own). Two concurrent inserts for the same (owner, host) can both pass the
    `live_host` check; `ux_egress_allowances_live` lets only one land. A SAVEPOINT
    (`begin_nested`) isolates the insert so the loser's `IntegrityError` unwinds just that
    savepoint - never the caller's outer transaction - and the loser re-fetches the winner's
    row instead of raising a raw 500."""
    existing = await EgressAllowance.live_host(session, actor_id, host)
    if existing is not None:
        return existing
    row = EgressAllowance(owner_actor_id=actor_id, host=host, source=source, gate_id=gate_id)
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        winner = await EgressAllowance.live_host(session, actor_id, host)
        if winner is None:
            raise
        return winner
    return row


async def add_allowance(
    session: AsyncSession,
    actor: Actor,
    host: str,
    *,
    source: str = "settings",
    gate_id: uuid.UUID | None = None,
) -> EgressAllowance:
    try:
        clean = check_host(host)
    except ValueError as bad:
        raise ValidationFailed(str(bad), fields={"host": str(bad)}) from None
    row = await _insert_allowance(session, actor.id, clean, source, gate_id)
    await session.commit()
    return row


async def revoke_allowance(session: AsyncSession, actor: Actor, allowance_id: uuid.UUID) -> None:
    row = await EgressAllowance.get_for(session, allowance_id, actor)
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        await session.commit()


async def hosts_for_owner(session: AsyncSession, actor_id: uuid.UUID) -> list[str]:
    return sorted(r.host for r in await EgressAllowance.live_for_owner(session, actor_id))


async def grants_for_task(session: AsyncSession, task: Task) -> list[str]:
    """Task-scoped grants are DERIVED from accepted gates - this task's own and its
    ancestors' (`parent_task_id` chain: a fork inherits the parent's task-scoped accepts; a
    routine's task has no parent and inherits nothing). `hops` bounds the walk - forks nest, but
    never eight deep in practice, and a bound beats an unbounded loop on a corrupt chain."""
    hosts: set[str] = set()
    current: Task | None = task
    hops = 0
    while current is not None and hops < 8:
        hosts.update(await Gate.accepted_browser_grants(session, current.id))
        current = await session.get(Task, current.parent_task_id) if current.parent_task_id else None
        hops += 1
    return sorted(hosts)
