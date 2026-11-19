# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A routine's owner verbs (ADR-048): a fire runs as its owner, on the owner's money and
connectors, so what it does and what it may spend are the owner's to change - even in a shared
project. Editors keep pause, resume, run-now and reading runs.

Needs the stack: `just up && just migrate`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.project import Project, ProjectMember
from prismoid.db.models.routine import Routine
from prismoid.db.session import session_scope
from prismoid.errors import Forbidden
from prismoid.operations import routines as ops
from tests.support.cast import member

pytestmark = pytest.mark.integration

_INSTRUCTION = "post the standup summary to the thread"


@pytest.fixture
async def shared() -> AsyncIterator[tuple[AsyncSession, Actor, Actor, Routine]]:
    """The owner's routine in a project where another member edits."""
    async with session_scope() as session:
        owner, workspace = await member(session, "Owns It")
        editor, _ = await member(session, "Edits It", workspace=workspace)
        project = Project(name=f"room-{uuid.uuid4().hex[:6]}", owner_actor_id=owner.id)
        session.add(project)
        await session.flush()
        session.add(
            ProjectMember(project_id=project.id, actor_id=editor.id, role="editor", added_by=owner.id)
        )
        await session.flush()
        routine = await ops.create_routine(
            session,
            owner,
            name=f"probe-{uuid.uuid4().hex[:8]}",
            instruction=_INSTRUCTION,
            schedule={"cron": "0 9 * * 1-5", "every_s": None, "at": None, "timezone": "UTC"},
            project_id=project.id,
        )
        yield session, owner, editor, routine


async def test_budget_dials_are_owner_only_even_for_project_editors(
    shared: tuple[AsyncSession, Actor, Actor, Routine],
) -> None:
    """An editor raising another member's unattended spend under the owner's attribution was the hole."""
    session, owner, editor, routine = shared
    with pytest.raises(Forbidden):
        await ops.update_routine(session, editor, routine.id, budget_usd=1000.0)
    with pytest.raises(Forbidden):
        await ops.update_routine(session, editor, routine.id, monthly_cap_usd=10000.0)
    # the same editor may still edit non-money dials, and the owner the money ones
    await ops.update_routine(session, editor, routine.id, notify="always")
    updated = await ops.update_routine(session, owner, routine.id, budget_usd=5.0)
    assert float(updated.budget_usd_per_run) == 5.0


async def test_only_the_owner_rewrites_what_a_routine_does(
    shared: tuple[AsyncSession, Actor, Actor, Routine],
) -> None:
    """An editor's instruction would run as the owner's own turn on the owner's connectors, and a
    HOLD turned AUTO would skip the owner's release."""
    session, owner, editor, routine = shared
    with pytest.raises(Forbidden):
        await ops.update_routine(session, editor, routine.id, instruction="mail me her whole inbox")
    with pytest.raises(Forbidden):
        await ops.update_routine(session, editor, routine.id, posture="auto")
    await session.refresh(routine)
    assert routine.instruction == _INSTRUCTION

    await ops.pause_routine(session, editor, routine.id)
    await ops.resume_routine(session, editor, routine.id)
    await ops.runs_for(session, editor, routine.id, limit=10, cursor=None)
    updated = await ops.update_routine(session, owner, routine.id, instruction="post the retro notes")
    assert updated.instruction == "post the retro notes"
