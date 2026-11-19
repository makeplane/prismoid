# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Sessions. Two doors, and only two.

`get_session()` - the request-scoped dependency (`SessionDep` in api/deps.py).
`session_scope()` - the same session for code with no request around it (jobs' effects, the CLI's
seeder, tests); it takes the tenant binding (`workspace_id`, or `platform=True` for a cross-workspace sweep -
ADR-061). `row_scope(Model, id)` is its job-side sibling: bound to that row's workspace.

**Neither helper commits - operations own transactions**: a caller does its work and
calls `session.commit()` explicitly, and that commit is the boundary after which the job is
enqueued. What these helpers guarantee is the other half of that contract: on teardown they
`rollback()`, so anything an operation left uncommitted is discarded rather than leaking into the
next user of the connection. A view that commits is a rule violation and `just audit` greps for it.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from prismoid.db.engine import get_engine


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(),
        expire_on_commit=False,  # a committed row is still readable - serializers run after commit
        autoflush=False,
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency. Rolls back anything an operation left open, always closes."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        finally:
            await session.rollback()


@asynccontextmanager
async def session_scope(
    workspace_id: uuid.UUID | None = None, *, platform: bool = False
) -> AsyncGenerator[AsyncSession]:
    """The same session, outside a request. A job entry point that acts for one row passes that
    row's `workspace_id`; a cross-workspace sweep passes `platform=True`. Neither = unbound, and a
    tenant-scoped read then fails closed."""
    from prismoid.db.models import tenant  # models import session helpers; avoid the cycle

    async with get_sessionmaker()() as session:
        if workspace_id is not None:
            tenant.bind(session, workspace_id)
        elif platform:
            tenant.bind_platform(session)
        try:
            yield session
        finally:
            await session.rollback()


@asynccontextmanager
async def row_scope(model: type[Any], row_id: uuid.UUID | str) -> AsyncGenerator[AsyncSession]:
    """`session_scope`, bound to ONE tenant row's workspace - the job entry point's door: a job that
    acts for a task, a conversation, a routine, a bot or an artifact's task
    opens its unit of work here."""
    from prismoid.db.models import tenant

    async with session_scope() as session:
        workspace_id = await tenant.workspace_of_row(session, model, row_id)
        if workspace_id is not None:  # no such row: nothing to bind, and a fenced read fails closed
            tenant.bind(session, workspace_id)
        yield session
