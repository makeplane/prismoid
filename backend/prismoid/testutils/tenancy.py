# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The tenant a test or an eval acts in (an unbound insert into a tenant root fails closed).

Shared by tests/support/cast.py and the in-process eval suites (evals/), which open their own units of
work outside any request and so must bind a workspace themselves, the way a job entry point does."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.workspace import Workspace, WorkspaceKind, WorkspaceMember, WorkspaceRole


async def member(
    session: AsyncSession,
    name: str = "Ada",
    *,
    role: WorkspaceRole = WorkspaceRole.OWNER,
    workspace: Workspace | None = None,
) -> tuple[Actor, Workspace]:
    """A human actor who belongs to a workspace, and the session bound to it as them.

    `workspace` given: they join it. Otherwise they join the workspace this session is already bound to
    (a test's cast shares one tenant, as every row shared the one pre-0b world), or - in an
    unbound session - they found a fresh team workspace. A second workspace in one test is an explicit
    `workspace=` (make it with `a_workspace`)."""
    actor = Actor(kind="human", display_name=name, scopes=[], status="active")
    session.add(actor)
    await session.flush()
    if workspace is None and (bound := tenant.bound_workspace(session)) is not None:
        workspace = await session.get(Workspace, bound)
    if workspace is None:
        workspace = await Workspace.create(
            session, name=f"{name}'s team", kind=WorkspaceKind.TEAM, created_by=actor.id
        )
    await WorkspaceMember.add(session, workspace.id, actor.id, role)
    tenant.bind(session, workspace.id, role.value)
    return actor, workspace
