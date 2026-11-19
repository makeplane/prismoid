# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The cast of a test: the people a test acts as and the workspaces they act in (an unbound
insert into a tenant root fails closed). A plain module, so a test imports it and never
`tests.conftest`."""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.user import User
from prismoid.db.models.workspace import Workspace, WorkspaceKind, WorkspaceMember, WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.testutils.tenancy import member as member  # re-exported: the one definition, shared with evals/


async def a_workspace(session: AsyncSession, name: str = "Other") -> tuple[Actor, Workspace]:
    """A SECOND tenant inside a bound test: its founder and the workspace, WITHOUT rebinding the
    session - the caller stays in the workspace it was acting for."""
    before = tenant.bound_workspace(session), tenant.role_of(session)
    founder = Actor(kind="human", display_name=name, scopes=[], status="active")
    session.add(founder)
    await session.flush()
    workspace = await Workspace.create(
        session, name=f"{name}'s team", kind=WorkspaceKind.TEAM, created_by=founder.id
    )
    await WorkspaceMember.add(session, workspace.id, founder.id, WorkspaceRole.OWNER)
    if before[0] is not None:
        tenant.bind(session, before[0], before[1])
    return founder, workspace


@asynccontextmanager
async def bound_scope(workspace_id: uuid.UUID) -> AsyncGenerator[AsyncSession]:
    """`session_scope(workspace_id=...)` - a test's later unit of work, acting in its workspace."""
    async with session_scope(workspace_id=workspace_id) as session:
        yield session


async def verify_email(actor_id: uuid.UUID | str) -> None:
    """Mark a person's address proven, as their emailed `ev_` code would, granting nothing: an
    inviter needs it (ADR-086)."""
    async with session_scope() as session:
        user = await session.get(User, uuid.UUID(str(actor_id)))
        assert user is not None
        user.email_verified_at = utcnow()
        await session.commit()
