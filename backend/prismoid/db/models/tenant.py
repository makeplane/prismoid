# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The tenant of a unit of work (ADR-061): which workspace this database session
acts for. Every door binds it; every tenant-scoped read asks `workspace_of`, which FAILS CLOSED -
a forgotten binding breaks a test, it never leaks a row.

Three states: unbound (the default - tenant reads raise), bound to one workspace, and platform (a
cross-workspace sweep or the operator console - tenant reads raise too, because a sweep enumerates
ids and binds each row's workspace before acting on it)."""

from __future__ import annotations

import functools
import uuid
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from prismoid.errors import NotFound, TenantUnbound

_WORKSPACE = "tenant.workspace_id"
_ROLE = "tenant.role"
_PLATFORM = "tenant.platform"
_PINNED = "tenant.pinned"


def bind(session: AsyncSession, workspace_id: uuid.UUID, role: str | None = None) -> None:
    """Act for this workspace. A unit of work whose credential is pinned to another one (an API key)
    is a stranger here: the stranger's 404, whichever door names the workspace - a route, a join
    link, an invite, the operator's console (which refuses a key at its door as well)."""
    pinned = pinned_workspace(session)
    if pinned is not None and pinned != workspace_id:
        raise NotFound("no such workspace")
    session.info[_WORKSPACE] = workspace_id
    session.info[_ROLE] = role
    session.info[_PLATFORM] = False


def bind_platform(session: AsyncSession) -> None:
    session.info.pop(_WORKSPACE, None)
    session.info.pop(_ROLE, None)
    session.info[_PLATFORM] = True


def pin(session: AsyncSession, workspace_id: uuid.UUID) -> None:
    """The credential itself belongs to this one workspace (an API key): the unit of work may never
    act for another, even where the person behind it holds a role there. A login session is not
    pinned - it may act in any workspace its person belongs to."""
    session.info[_PINNED] = workspace_id


def pinned_workspace(session: AsyncSession) -> uuid.UUID | None:
    pinned = session.info.get(_PINNED)
    return pinned if isinstance(pinned, uuid.UUID) else None


def is_platform(session: AsyncSession) -> bool:
    return bool(session.info.get(_PLATFORM, False))


def bound_workspace(session: AsyncSession) -> uuid.UUID | None:
    workspace = session.info.get(_WORKSPACE)
    return workspace if isinstance(workspace, uuid.UUID) else None


def workspace_of(session: AsyncSession) -> uuid.UUID:
    workspace = bound_workspace(session)
    if workspace is None:
        raise TenantUnbound("platform unit of work" if is_platform(session) else "no workspace bound")
    return workspace


def role_of(session: AsyncSession) -> str | None:
    role = session.info.get(_ROLE)
    return role if isinstance(role, str) else None


async def workspace_of_row(
    session: AsyncSession, model: type[Any], row_id: uuid.UUID | str
) -> uuid.UUID | None:
    """One tenant row's workspace, read as a bare column (never the row - an operation's later locked
    read must not be served a stale identity-map copy). The job side's binding source.
    None means no such row: workspace_id is NOT NULL on every root."""
    return await session.scalar(select(model.workspace_id).where(model.id == row_id))


# ── write-time stamping ────────────────────────────────────────────────────────────────────
# Tenant roots take the bound workspace when they are inserted. STRICT: an insert with no workspace bound
# (or in a platform unit) raises before any SQL - fail closed, never a NULL or a guessed workspace.
STRICT = True


@functools.cache
def _root_tables() -> frozenset[str]:
    from prismoid.db.models.tenancy import TABLE_CLASSES, TenancyClass

    return frozenset(t for t, c in TABLE_CLASSES.items() if c is TenancyClass.ROOT)


def _stamp(session: Session, _ctx: object, _instances: object) -> None:
    roots = _root_tables()
    for obj in session.new:
        table = getattr(obj, "__tablename__", None)
        if table not in roots or getattr(obj, "workspace_id", None) is not None:
            continue
        workspace = session.info.get(_WORKSPACE)
        if isinstance(workspace, uuid.UUID):
            obj.workspace_id = workspace
        elif STRICT:
            raise TenantUnbound(f"insert into {table} with no workspace bound")


def install_stamping() -> None:
    """Idempotent: a second import or call never double-registers the listener."""
    if not event.contains(Session, "before_flush", _stamp):
        event.listen(Session, "before_flush", _stamp)
