# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""N+1 tripwires - a list's query count must not grow with its row count.

The lists are N+1-free *by design*: task summaries are scalar columns, and `list_projects` deliberately
answers owner-or-viewer without a per-project membership query (it says so in a comment). So the point of
these tests is not to find an N+1 today - it is to make that property a **tripwire**, so the day someone
adds a relation to a summary and forgets to eager-load it, a test goes red instead of a list quietly
issuing fifty queries in production.

The counter listens on the engine and tallies every statement executed. A bounded list is ~1 query at
3 rows and *the same* at 12. The control (`test_the_tripwire_catches_an_nplus1`) does the opposite on
purpose - a query per row - and proves the tripwire would catch it: that is the "remove the eager-load
and watch it fail" demonstration, run every suite rather than by hand.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.api.serializers.project_invites import project_invite_out
from prismoid.api.serializers.tasks import TaskSummaryOut, task_summary
from prismoid.db.base import utcnow
from prismoid.db.engine import get_engine
from prismoid.db.models.actor import Actor
from prismoid.db.models.machine import Machine
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.user import User
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.operations import machines
from prismoid.operations import project_invites as invite_ops
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.machines import MachineState
from tests.support.cast import member

pytestmark = pytest.mark.integration


@contextlib.contextmanager  # pyright: ignore[reportDeprecated]
def count_queries() -> Iterator[list[int]]:
    """Tally every SQL statement the engine executes inside the block."""
    tally = [0]
    sync_engine = get_engine().sync_engine

    def _on_execute(*_args: object, **_kwargs: object) -> None:
        tally[0] += 1

    event.listen(sync_engine, "before_cursor_execute", _on_execute)
    try:
        yield tally
    finally:
        event.remove(sync_engine, "before_cursor_execute", _on_execute)


async def _actor(session: AsyncSession) -> Actor:
    actor, _ = await member(session, "Nina")
    await session.flush()
    return actor


async def test_task_list_is_not_nplus1() -> None:
    async with session_scope() as session:
        actor = await _actor(session)
        for i in range(3):
            await task_ops.create_task(session, actor, goal=f"task {i}")
        with count_queries() as q:
            tasks, names, _ = await task_ops.list_tasks(session, actor, limit=50)
            _ = [task_summary(t, names) for t in tasks]
        at_three = q[0]

        for i in range(9):  # now twelve
            await task_ops.create_task(session, actor, goal=f"task {i + 3}")
        with count_queries() as q:
            tasks, names, _ = await task_ops.list_tasks(session, actor, limit=50)
            _ = [task_summary(t, names) for t in tasks]
        at_twelve = q[0]

    print(f"\n  task list:  {at_three} queries at 3 rows · {at_twelve} at 12 rows  (constant ⇒ no N+1)")
    assert at_three == at_twelve, f"task list is N+1: {at_three} queries at 3 rows, {at_twelve} at 12"
    assert at_twelve <= 2, f"the task list should be the page plus one name lookup, got {at_twelve}"


async def test_task_list_names_are_not_nplus1() -> None:
    """Each row names its creator and its turn's author. Three people, mixed both
    ways, and the count of statements is the same at 3 rows as at 12 - the names ride ONE batched
    `User.by_actor_ids`, never a lookup per task."""
    async with session_scope() as session:
        ada, workspace = await member(session, "Ada")
        bob, _ = await member(session, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        cy, _ = await member(session, "Cy", role=WorkspaceRole.MEMBER, workspace=workspace)
        crew = [ada, bob, cy]
        for person in crew:
            session.add(
                User(
                    actor_id=person.id,
                    email=f"{person.display_name.lower()}@nplus1.test",
                    name=person.display_name,
                )
            )
        project = (await project_ops.create(session, ada, name="names")).project
        for person in (bob, cy):
            session.add(
                ProjectMember(project_id=project.id, actor_id=person.id, role="editor", added_by=ada.id)
            )
        await session.flush()
        made = 0

        async def add(count: int) -> None:
            nonlocal made
            for _ in range(count):
                creator = crew[made % 3]
                task = await task_ops.create_task(
                    session, creator, goal=f"task {made}", project_id=project.id
                )
                task.turn_actor_id = crew[(made + 1) % 3].id  # someone else's turn: creators and authors mix
                made += 1
            await session.flush()  # the counted block reads; it must not also pay for the setup's UPDATEs

        async def counted() -> tuple[int, list[TaskSummaryOut]]:
            with count_queries() as q:
                tasks, names, _ = await task_ops.list_tasks(session, ada, project_id=project.id, limit=50)
                rows = [task_summary(t, names) for t in tasks]
            return q[0], rows

        await add(3)
        at_three, three = await counted()
        await add(9)  # now twelve
        at_twelve, twelve = await counted()

    print(f"\n  task names: {at_three} queries at 3 rows · {at_twelve} at 12 rows  (constant ⇒ no N+1)")
    assert (len(three), len(twelve)) == (3, 12)
    assert at_three == at_twelve, f"task names are N+1: {at_three} queries at 3 rows, {at_twelve} at 12"
    assert at_twelve <= 2, f"the page plus ONE name lookup, got {at_twelve}"
    named = {r.creator_name for r in twelve}, {r.turn_author_name for r in twelve}
    assert named == ({"Ada", "Bob", "Cy"}, {"Ada", "Bob", "Cy"}), "the names are filled, not just cheap"
    assert all(r.creator_name != r.turn_author_name for r in twelve), "the two fields are not one copied"


async def test_project_list_is_not_nplus1() -> None:
    async with session_scope() as session:
        actor = await _actor(session)
        for i in range(3):
            await project_ops.create(session, actor, name=f"project {i}")
        with count_queries() as q:
            _ = await project_ops.list_projects(session, actor)
        at_three = q[0]

        for i in range(9):
            await project_ops.create(session, actor, name=f"project {i + 3}")
        with count_queries() as q:
            _ = await project_ops.list_projects(session, actor)
        at_twelve = q[0]

    print(f"\n  project list: {at_three} queries at 3 rows · {at_twelve} at 12 rows  (constant ⇒ no N+1)")
    assert at_three == at_twelve, f"project list is N+1: {at_three} at 3, {at_twelve} at 12"
    assert at_twelve <= 2, f"the project list should be ~1 query, got {at_twelve}"


async def test_project_invite_list_is_not_nplus1() -> None:
    async with session_scope() as session:
        actor = await _actor(session)
        project = (await project_ops.create(session, actor, name="links")).project
        for _ in range(3):
            await invite_ops.create_invite(session, actor, project.id, role="viewer", expires_in_days=None)
        with count_queries() as q:
            _ = [project_invite_out(r) for r in await invite_ops.list_invites(session, actor, project.id)]
        at_three = q[0]

        for _ in range(9):
            await invite_ops.create_invite(session, actor, project.id, role="viewer", expires_in_days=None)
        with count_queries() as q:
            _ = [project_invite_out(r) for r in await invite_ops.list_invites(session, actor, project.id)]
        at_twelve = q[0]

    print(f"\n  invite list: {at_three} queries at 3 rows · {at_twelve} at 12 rows  (constant ⇒ no N+1)")
    assert at_three == at_twelve, f"project invite list is N+1: {at_three} at 3, {at_twelve} at 12"
    assert at_twelve <= 3, f"the invite list should be the owner gate plus one query, got {at_twelve}"


async def test_the_tripwire_catches_an_nplus1() -> None:
    """The control: a query-per-row loop MUST show the counter growing with N - otherwise the tripwires
    above would be green on a genuine N+1 and prove nothing. This is the eager-load removal, standing."""
    from sqlalchemy import text

    async with session_scope() as session:
        actor = await _actor(session)
        for i in range(6):
            await task_ops.create_task(session, actor, goal=f"task {i}")
        tasks, _names, _ = await task_ops.list_tasks(session, actor, limit=50)
        with count_queries() as q:
            for (
                _t
            ) in tasks:  # the N+1 shape: one round-trip per row (a per-row SELECT stands in for a lazy load)
                await session.execute(text("SELECT 1"))
    print(
        f"\n  control:    {q[0]} queries for {len(tasks)} rows  (grows with N ⇒ the tripwire would catch it)"
    )
    assert q[0] >= len(tasks), f"the counter failed to see an N+1: {q[0]} queries for {len(tasks)} rows"


async def test_the_door_is_one_query_per_credential(silent_dispatcher: None) -> None:
    """The door joins the workspace membership and the workspace's standing INTO the credential's
    resolve query - so a session and an API key each cost exactly ONE statement on the success
    path, the same as before workspaces existed (the rebind runs only on the failure path).
    `GET /v1/auth/me` is that one plus the view's own two reads (the user; the bound workspace it now
    names)."""
    import uuid

    from httpx import ASGITransport, AsyncClient
    from sqlalchemy import text

    from prismoid.api.app import create_app
    from prismoid.api.deps import authenticate
    from prismoid.operations import auth as auth_ops

    async with AsyncClient(transport=ASGITransport(app=create_app()), base_url="http://test") as client:
        signup = await client.post(
            "/v1/auth/signup",
            json={
                "email": f"door{uuid.uuid4().hex[:10]}@example.com",
                "name": "Pia",
                "password": "a-long-enough-password",
            },
        )
        session_token = str(signup.json()["token"])
        minted = await client.post(
            "/v1/auth/api-keys", headers={"Authorization": f"Bearer {session_token}"}, json={"name": "perf"}
        )
        api_key = str(minted.json()["key"])
        await client.get(
            "/v1/auth/me", headers={"Authorization": f"Bearer {api_key}"}
        )  # the once-a-minute bump

        counts: dict[str, tuple[int, int]] = {}
        for label, raw in (("session", session_token), ("api key", api_key)):
            async with session_scope() as session:
                await session.execute(text("SELECT 1"))  # the transaction is open before we count
                with count_queries() as door:
                    await authenticate(session, raw)
            with count_queries() as request:
                response = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {raw}"})
            assert response.status_code == 200, response.text
            counts[label] = (door[0], request[0])
    await auth_ops.flush_outbound_email()

    for label, (door_q, me_q) in counts.items():
        print(f"\n  door ({label}): {door_q} query · GET /v1/auth/me: {me_q} queries")
        assert door_q == 1, f"the {label} door must be ONE query, got {door_q}"
        assert me_q == 3, f"GET /v1/auth/me with a {label} should be door + user + workspace, got {me_q}"


async def test_the_task_detail_gains_at_most_one_query_for_its_machine(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with session_scope() as s:
        ada, workspace = await member(s, "Nina")
        task = await task_ops.create_task(s, ada, goal="counted")
        await s.commit()
        s.add(
            Machine(
                workspace_id=workspace.id,
                task_id=task.id,
                driver="docker",
                profile="base",
                state=MachineState.IDLE,
                state_since=utcnow(),
                last_active_at=utcnow(),
                epoch=1,
                frame_ref="machine-n-1",
            )
        )
        await s.commit()

        async def _none(*_a: object, **_k: object) -> None:
            return None

        with monkeypatch.context() as m:  # the detail read without the Machine: the baseline
            m.setattr(machines, "summary", _none)
            await task_ops.read_task(s, ada, task.id)  # warm
            with count_queries() as bare:
                await task_ops.read_task(s, ada, task.id)
        await task_ops.read_task(s, ada, task.id)  # warm
        with count_queries() as seen:
            read = await task_ops.read_task(s, ada, task.id)
        with count_queries() as one:
            await machines.summary(s, task.id)
    assert read.machine is not None
    assert one[0] == 1
    assert seen[0] <= bare[0] + 1, f"the Machine added {seen[0] - bare[0]} queries to the task detail"
