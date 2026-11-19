# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The tasks list search (the keyset stays; q narrows): title OR goal, case-insensitive,
wildcards are literals - the /tasks page's search box speaks exactly this."""

from __future__ import annotations

import pytest

from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import steering
from prismoid.operations import tasks as task_ops
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def test_search_matches_title_or_goal() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Quin")
        await session.flush()

        keyboard = await task_ops.create_task(session, actor, goal="research mechanical keyboards")
        memo = await task_ops.create_task(session, actor, goal="draft the pricing memo")
        await steering.update_meta(session, actor, memo.id, title="Q3 pricing")

        by_goal = await Task.list_for(session, actor, q="KEYBOARD")
        assert keyboard.id in {t.id for t in by_goal}, "case-insensitive goal match"

        by_title = await Task.list_for(session, actor, q="q3 pricing")
        assert memo.id in {t.id for t in by_title}, "the label matches too"

        none = await Task.list_for(session, actor, q="100%_literal")
        assert none == [], "ILIKE wildcards in the query are literals, not patterns"


async def test_project_members_do_not_multiply_rows() -> None:
    """Visibility is an EXISTS, not a join: a project with N members must not list each of its
    tasks N times for the creator (the join fan-out also let `limit` count copies)."""
    async with session_scope() as session:
        from prismoid.db.models.project import Project
        from prismoid.db.models.project_member import ProjectMember

        owner, _ = await member(session, "Owner")
        m1, _ = await member(session, "Maya")
        m2, _ = await member(session, "Jon")
        await session.flush()

        project = Project(name="Research", owner_actor_id=owner.id)
        session.add(project)
        await session.flush()
        session.add_all(
            [
                ProjectMember(project_id=project.id, actor_id=m1.id, role="editor", added_by=owner.id),
                ProjectMember(project_id=project.id, actor_id=m2.id, role="viewer", added_by=owner.id),
            ]
        )

        task = await task_ops.create_task(session, owner, goal="one task, once", project_id=project.id)

        rows = await Task.list_for(session, owner, project_id=project.id)
        assert [t.id for t in rows] == [task.id], "each task exactly once, members notwithstanding"

        member_rows = await Task.list_for(session, m1, project_id=project.id)
        assert [t.id for t in member_rows] == [task.id], "a member sees it once too"


async def test_status_group_filter_and_counts() -> None:
    """A product group is a set of machine states: `statuses` filters with one IN-query, and
    `counts_for` tallies by status under the same visibility + search filters - so a group
    header's total and its paginated list can never disagree."""
    async with session_scope() as session:
        actor, _ = await member(session, "Tally")
        stranger, _ = await member(session, "Nobody")
        await session.flush()

        running = await task_ops.create_task(session, actor, goal="crunch the numbers")
        running.status = "running"
        done = await task_ops.create_task(session, actor, goal="ship the memo")
        done.status = "delivered"
        dead = await task_ops.create_task(session, actor, goal="doomed experiment")
        dead.status = "failed"
        await session.flush()

        active = await Task.list_for(session, actor, statuses=["running", "waiting", "paused"])
        assert [t.id for t in active] == [running.id], "the group IN-filter"

        counts = await Task.counts_for(session, actor)
        assert counts == {"running": 1, "delivered": 1, "failed": 1}

        assert await Task.counts_for(session, stranger) == {}, "visibility holds for counts"

        narrowed = await Task.counts_for(session, actor, q="memo")
        assert narrowed == {"delivered": 1}, "counts honour the same search filter as the list"
