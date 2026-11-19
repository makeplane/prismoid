# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The dial IS the plan (ADR-019 speed pass), pinned: a chosen deliverable seats no Lead - the
deterministic one-step plan is instant, typed, and exactly the floor `validate()` would have
enforced anyway."""

from __future__ import annotations

import pytest

from prismoid.db.session import session_scope
from prismoid.operations import planning
from prismoid.operations import tasks as task_ops
from prismoid.schemas.plan import TaskStatus
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def test_a_dialed_deliverable_is_one_typed_step_with_no_lead() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Dialer")
        await session.flush()
        task = await task_ops.create_task(
            session, actor, goal="track the launch by stage", deliverable="collection", clarify="never"
        )
        task.transition(TaskStatus.PLANNING)
        await session.flush()

        view = await planning.draft_plan(session, task.id)
        await session.commit()

    assert len(view.steps) == 1, "the dial is one typed step, deterministically"
    assert view.steps[0].agent == "maker", "the kind's agent, from the format skills"
