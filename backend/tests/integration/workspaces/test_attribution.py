# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every human action on a task names its actor on the record."""

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents import base as agents_base
from prismoid.api.app import create_app
from prismoid.api.endpoints.tasks import get_task
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.gate import Gate
from prismoid.db.models.plan import PlanEdit, PlanStep
from prismoid.db.models.project import Project
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.session import AuthSession
from prismoid.db.models.task import Task
from prismoid.db.models.user import User
from prismoid.db.models.workspace import WorkspaceMember, WorkspaceRole
from prismoid.db.session import session_scope
from prismoid.errors import Forbidden, NotFound
from prismoid.operations import auth as auth_ops
from prismoid.operations import gates, steering
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.events import GateResolved
from prismoid.schemas.ids import StepId
from prismoid.schemas.plan import GateAction, GateResolution, TaskStatus
from tests.support.asks import question_set
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _trio(s: AsyncSession) -> tuple[Actor, Actor, Actor, Project, Task]:
    """Ada owns a Project; Bob edits; Cy views - all in one workspace."""
    ada, workspace = await member(s, "Ada")
    bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
    cy, _ = await member(s, "Cy", role=WorkspaceRole.MEMBER, workspace=workspace)
    project = (await project_ops.create(s, ada, name=f"launch-{uuid.uuid4().hex[:6]}")).project
    s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
    s.add(ProjectMember(project_id=project.id, actor_id=cy.id, role="viewer", added_by=ada.id))
    await s.flush()
    task = await task_ops.create_task(s, ada, goal="compare three trackers", project_id=project.id)
    return ada, bob, cy, project, task


async def _running(s: AsyncSession, task: Task) -> None:
    task.transition(TaskStatus.PLANNING)
    task.transition(TaskStatus.RUNNING)
    await s.commit()


async def _actors(s: AsyncSession, task_id: uuid.UUID, type_: str) -> list[str | None]:
    return [
        (e.actor.name if e.actor else None)
        for e in await TaskEvent.replay(s, task_id, since=0)
        if e.type == type_
    ]


async def test_steer_pause_resume_cancel_name_the_editor(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, bob, _cy, _project, task = await _trio(s)
        await _running(s, task)
        await steering.steer(s, bob, task.id, "focus on pricing")
        await steering.pause(s, bob, task.id)
        await steering.resume(s, ada, task.id)
        await steering.cancel(s, bob, task.id)
        assert await _actors(s, task.id, "plan.edited") == ["Bob"]
        assert (await _actors(s, task.id, "task.status"))[-3:] == ["Bob", "Ada", "Bob"]
        edit = await s.scalar(select(PlanEdit).where(PlanEdit.task_id == task.id, PlanEdit.kind == "steer"))
        assert edit is not None and edit.actor_id == bob.id


async def test_rename_and_move_name_the_creator(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _bob, _cy, _project, task = await _trio(s)
        await steering.update_meta(s, ada, task.id, title="Tracker memo")
        await steering.move(s, ada, task.id, None)
        assert await _actors(s, task.id, "task.renamed") == ["Ada"]
        assert await _actors(s, task.id, "task.moved") == ["Ada"]


def _revises(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The Lead's replan seat, its model scripted to revise the plan; the words each request was told."""
    told: list[str] = []
    step = {"label": "s1", "agent": "writer", "title": "Compare", "goal": "Compare them", "publishes": True}
    revision = {"say": "Tightening the scope.", "plan": {"steps": [step], "deliverable": "files"}}

    def revise(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        told.extend(
            str(p.content)
            for m in messages
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, UserPromptPart)
        )
        return ModelResponse(parts=[ToolCallPart("revise_plan", revision, "r1")])

    monkeypatch.setattr(agents_base, "resolve", lambda *a, **k: FunctionModel(revise))
    return told


async def test_replan_needs_editor_and_names_them(
    silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    told = _revises(monkeypatch)
    async with session_scope() as s:
        _ada, bob, cy, _project, task = await _trio(s)
        s.add(
            PlanStep(
                task_id=task.id,
                ordinal=0,
                title="Draft",
                kind="step",
                state="ready",
                spec={"agent": "writer"},
            )
        )
        await _running(s, task)
        with pytest.raises(Forbidden):
            await task_ops.replan(s, cy, task.id, reason="start over")
        await task_ops.replan(s, bob, task.id, reason="tighter scope")
        assert await _actors(s, task.id, "plan.edited") == ["Bob", "Bob"], "the steer and the replan"
        edit = await s.scalar(select(PlanEdit).where(PlanEdit.task_id == task.id, PlanEdit.kind == "replan"))
        assert edit is not None and edit.actor_id == bob.id
    assert any("**Bob steered**: tighter scope" in words for words in told), (
        "the Lead hears the editor's words"
    )


async def test_a_viewer_cannot_fork_into_the_project(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, _bob, cy, _project, task = await _trio(s)
        with pytest.raises(Forbidden):
            await task_ops.fork(s, cy, task.id)


async def test_a_viewer_watches_and_does_not_grade(silent_dispatcher: None) -> None:
    """The thumbs are a durable `task.feedback` write that every member's page shows as the
    turn's grade: viewers watch, so a viewer's press is refused like any write,
    and an editor's lands with their name on it."""
    async with session_scope() as s:
        _ada, bob, cy, _project, task = await _trio(s)
        with pytest.raises(Forbidden):
            await task_ops.record_feedback(s, cy, task.id, rating="down")
        await task_ops.record_feedback(s, bob, task.id, rating="up")
        assert await _actors(s, task.id, "task.feedback") == ["Bob"]


async def test_can_edit_follows_the_role(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, bob, cy, _project, task = await _trio(s)
        assert await steering.may_edit(s, task, ada) is True
        assert await steering.may_edit(s, task, bob) is True
        assert await steering.may_edit(s, task, cy) is False


async def test_gate_resolve_and_cancel_name_who_did_it(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, bob, _cy, _project, task = await _trio(s)
        await _running(s, task)
        step = PlanStep(task_id=task.id, ordinal=0, title="compare the trackers")
        s.add(step)
        await s.flush()
        answered = await gates.open_question(
            s, task, StepId(step.id), question_set(("Which currency?", ["eur", "usd"]))
        )
        await gates.open_question(s, task, StepId(step.id), question_set(("How formal?", [])))
        await s.commit()

        await gates.resolve(s, bob, answered.id, GateAction.ACCEPT, {"q1": "eur"})
        assert await _actors(s, task.id, "gate.resolved") == ["Bob"]

        await steering.cancel(s, ada, task.id)  # closes the still-open second gate as cancel
        cancels = [
            e
            for e in await TaskEvent.replay(s, task.id, since=0)
            if isinstance(e, GateResolved) and e.action is GateAction.CANCEL
        ]
        assert cancels and all(e.actor is not None and e.actor.name == "Ada" for e in cancels)


async def _open_question(s: AsyncSession, task: Task) -> Gate:
    await _running(s, task)
    step = PlanStep(task_id=task.id, ordinal=0, title="compare the trackers")
    s.add(step)
    await s.flush()
    gate = await gates.open_question(
        s, task, StepId(step.id), question_set(("Which currency?", ["eur", "usd"]))
    )
    await s.commit()
    return gate


class _HeldAnswer:
    """Bob's resolve, held just before its commit: his answer is written and his transaction holds
    the task's row lock. `entered` is set the moment the commit is reached (no timing guess), and
    nothing commits until `release`."""

    def __init__(self, workspace_id: uuid.UUID, gate_id: uuid.UUID) -> None:
        self.workspace_id, self.gate_id = workspace_id, gate_id
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def run(self, actor_id: uuid.UUID, value: str) -> GateResolution:
        async with session_scope(self.workspace_id) as x:
            who = await x.get(Actor, actor_id)
            assert who is not None
            commit = x.commit

            async def held() -> None:
                self.entered.set()
                await self.release.wait()
                await commit()

            x.commit = held
            return await gates.resolve(x, who, self.gate_id, GateAction.ACCEPT, {"q1": value})


async def _settle(*tasks: asyncio.Task[Any] | None) -> None:
    """Cancel what is still running and AWAIT it, so no task outlives its test."""
    for t in tasks:
        if t is not None and not t.done():
            t.cancel()
    await asyncio.gather(*(t for t in tasks if t is not None), return_exceptions=True)


async def _blocked(t: asyncio.Task[Any]) -> bool:
    """Still parked after a bounded wait - the lock is what holds it, not a sleep that guesses."""
    done, _ = await asyncio.wait({t}, timeout=0.5)
    return not done


async def test_two_people_answering_at_once_leave_one_answer(silent_dispatcher: None) -> None:
    """Whoever submits first wins, and the second is handed the first's resolution.
    Bob's resolve is held just before its commit (his answer written, uncommitted) while Ada's
    arrives. Unlocked, Ada read the gate as OPEN and wrote a second `gate.resolved` over his row;
    under the task's row lock she waits, then reads RESOLVED."""
    async with session_scope() as s:
        ada, bob, _cy, _project, task = await _trio(s)
        workspace_id = tenant.bound_workspace(s)
        assert workspace_id is not None
        gate = await _open_question(s, task)

    bob_answers = _HeldAnswer(workspace_id, gate.id)

    async def ada_answers() -> GateResolution:
        async with session_scope(workspace_id) as x:
            who = await x.get(Actor, ada.id)
            assert who is not None
            return await gates.resolve(x, who, gate.id, GateAction.ACCEPT, {"q1": "usd"})

    first = asyncio.create_task(bob_answers.run(bob.id, "eur"))
    second: asyncio.Task[GateResolution] | None = None
    try:
        await asyncio.wait_for(bob_answers.entered.wait(), timeout=15)
        second = asyncio.create_task(ada_answers())
        assert await _blocked(second), "Ada waits for Bob's commit instead of reading a stale OPEN"
        bob_answers.release.set()
        bobs, adas = await asyncio.wait_for(asyncio.gather(first, second), timeout=15)
    finally:
        bob_answers.release.set()
        await _settle(first, second)

    assert (adas.resolved_by, adas.content) == ("Bob", {"q1": "eur"}), "Ada is handed Bob's answer"
    assert (bobs.resolved_by, bobs.content) == ("Bob", {"q1": "eur"})
    # By id too: the web says "Bob answered first" off the id when it has one, so
    # two people who share a name are still two people.
    assert adas.resolved_by_id == bobs.resolved_by_id == bob.id
    async with session_scope(workspace_id) as s:
        assert await _actors(s, task.id, "gate.resolved") == ["Bob"], "one answer on the record"
        row = await s.get(Gate, gate.id)
        assert row is not None and row.resolved_by == bob.id
        assert GateResolution.model_validate(row.resolution).content == {"q1": "eur"}, "his row, his answer"


async def test_the_timeout_sweep_never_overwrites_an_answer_that_committed_first(
    silent_dispatcher: None,
) -> None:
    """The sweep reads expired gates unlocked; Bob's answer commits between that scan and the
    timeout write. The clock takes the same guard as `resolve` (task row lock, re-read, leave a
    closed gate alone): no `system (timeout)` resolution lands over his, no second frame."""
    async with session_scope() as s:
        _ada, bob, _cy, _project, task = await _trio(s)
        workspace_id = tenant.bound_workspace(s)
        assert workspace_id is not None
        gate = await _open_question(s, task)

    bob_answers = _HeldAnswer(workspace_id, gate.id)
    first = asyncio.create_task(bob_answers.run(bob.id, "eur"))
    clock: asyncio.Task[bool] | None = None
    async with session_scope(platform=True) as sweep_s:
        try:
            await asyncio.wait_for(bob_answers.entered.wait(), timeout=15)
            scanned = await sweep_s.get(Gate, gate.id)  # the sweep's scan: still OPEN to it
            assert scanned is not None and scanned.is_open()
            clock = asyncio.create_task(
                gates._resolve_by_timeout(sweep_s, scanned, GateAction.DECLINE)  # pyright: ignore[reportPrivateUsage]
            )
            assert await _blocked(clock), "the timeout write waits on the task's row lock"
            bob_answers.release.set()
            await asyncio.wait_for(first, timeout=15)
            resolved_by_clock = await asyncio.wait_for(clock, timeout=15)
        finally:
            bob_answers.release.set()
            await _settle(first, clock)

    assert resolved_by_clock is False, "the sweep leaves a gate a human already closed"
    async with session_scope(workspace_id) as s:
        assert await _actors(s, task.id, "gate.resolved") == ["Bob"]
        row = await s.get(Gate, gate.id)
        assert row is not None and GateResolution.model_validate(row.resolution).resolved_by == "Bob"


async def test_an_editor_removed_while_their_answer_waits_on_the_lock_is_refused(
    silent_dispatcher: None,
) -> None:
    """The role is decided on the locked row, like everything else `resolve` decides: Bob's
    answer passes the visibility read, then queues on the task's row lock (a cancel, a move, any
    task writer holds it); Ada removes him meanwhile. Checked before the lock, his answer would
    still land - and count as a teammate's input. Checked under it, he is refused."""
    async with session_scope() as s:
        ada, bob, _cy, project, task = await _trio(s)
        workspace_id = tenant.bound_workspace(s)
        assert workspace_id is not None
        gate = await _open_question(s, task)

    async def bob_answers() -> GateResolution:
        async with session_scope(workspace_id) as x:
            who = await x.get(Actor, bob.id)
            assert who is not None
            return await gates.resolve(x, who, gate.id, GateAction.ACCEPT, {"q1": "eur"})

    answer: asyncio.Task[GateResolution] | None = None
    async with session_scope(workspace_id) as holder:
        try:
            await Task.hold(holder, task.id)  # another writer holds the task row
            answer = asyncio.create_task(bob_answers())
            assert await _blocked(answer), "Bob's answer queues on the task's row lock"
            async with session_scope(workspace_id) as owner:
                who = await owner.get(Actor, ada.id)
                assert who is not None
                await project_ops.remove_member(owner, who, project.id, bob.id)
                await owner.commit()
            await holder.commit()
            with pytest.raises((Forbidden, NotFound)):
                await asyncio.wait_for(answer, timeout=15)
        finally:
            await _settle(answer)

    async with session_scope(workspace_id) as s:
        assert await _actors(s, task.id, "gate.resolved") == [], "nothing landed"
        row = await s.get(Gate, gate.id)
        assert row is not None and row.is_open()


async def test_a_system_cancel_names_no_one_on_the_gate_it_closed(silent_dispatcher: None) -> None:
    """A routine's REPLACE cancel borrows the owner's authority but is not her act: its events
    name nobody, and so does the gate row a late answerer is handed - never "Ada answered
    first" for something Ada did not do."""
    async with session_scope() as s:
        ada, bob, _cy, _project, task = await _trio(s)
        gate = await _open_question(s, task)
        await gates.resolve_open_as_cancel(s, task, ada, attribute=False)
        await s.commit()

        late = await gates.resolve(s, bob, gate.id, GateAction.ACCEPT, {"q1": "eur"})

        assert (late.action, late.resolved_by, late.resolved_by_id) == (
            GateAction.CANCEL,
            "system (cancel)",
            None,
        )
        row = await s.get(Gate, gate.id)
        assert row is not None and row.resolved_by is None
        assert await _actors(s, task.id, "gate.resolved") == [None]


async def test_answering_a_gate_that_expired_unanswered_says_so_without_an_error(
    silent_dispatcher: None,
) -> None:
    """Delivery closes the book (`expire_open`): the gate is EXPIRED with no stored resolution. A
    late answer is handed what the record already says - a decline by the system - never a 500."""
    async with session_scope() as s:
        _ada, bob, _cy, _project, task = await _trio(s)
        gate = await _open_question(s, task)
        await gates.expire_open(s, task)
        await s.commit()

        late = await gates.resolve(s, bob, gate.id, GateAction.ACCEPT, {"q1": "eur"})

        assert (late.action, late.content, late.resolved_by) == (GateAction.DECLINE, None, "system (expired)")
        assert await _actors(s, task.id, "gate.resolved") == [None], "the expiry is the only frame"


async def test_the_task_detail_wire_carries_can_edit(silent_dispatcher: None) -> None:
    """`GET /v1/tasks/{id}` says whether the caller may steer - the composer's viewer mode."""
    async with session_scope() as s:
        result = await auth_ops.signup(
            s, email=f"u{uuid.uuid4().hex[:12]}@example.com", name="Ana", password="a-long-enough-password"
        )
        principal = await AuthSession.resolve(s, result.token)
        assert principal is not None and principal.workspace_id is not None
        tenant.bind(s, principal.workspace_id, principal.role)
        task = await task_ops.create_task(s, principal.actor, goal="write a memo")
        await s.commit()
    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        r = await client.get(f"/v1/tasks/{task.id}", headers={"Authorization": f"Bearer {result.token}"})
    assert r.status_code == 200 and r.json()["can_edit"] is True


async def test_a_creator_removed_from_the_project_still_reads_their_task_read_only(
    silent_dispatcher: None,
) -> None:
    """`Task._visible_to` still shows a task to its creator after the Project took their seat back;
    the detail read must not 404 over it - `can_edit` just turns False."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        project = (await project_ops.create(s, ada, name=f"launch-{uuid.uuid4().hex[:6]}")).project
        project_id = project.id
        signed = await auth_ops.signup(
            s, email=f"u{uuid.uuid4().hex[:12]}@example.com", name="Bob", password="a-long-enough-password"
        )
        principal = await AuthSession.resolve(s, signed.token)
        assert principal is not None
        bob = principal.actor
        await WorkspaceMember.add(s, workspace.id, bob.id, WorkspaceRole.MEMBER)
        await s.execute(
            update(AuthSession)
            .where(AuthSession.id == principal.auth_session_id)
            .values(active_workspace_id=workspace.id)
        )
        s.add(ProjectMember(project_id=project_id, actor_id=bob.id, role="editor", added_by=ada.id))
        await s.flush()
        tenant.bind(s, workspace.id, WorkspaceRole.MEMBER.value)
        task = await task_ops.create_task(s, bob, goal="compare three trackers", project_id=project_id)
        task_id = task.id
        await s.commit()

        transport = httpx.ASGITransport(app=create_app())
        headers = {"Authorization": f"Bearer {signed.token}"}
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            seated = await client.get(f"/v1/tasks/{task_id}", headers=headers)
            assert seated.status_code == 200 and seated.json()["can_edit"] is True

            tenant.bind(s, workspace.id, WorkspaceRole.OWNER.value)
            await project_ops.remove_member(s, ada, project_id, bob.id)

            removed = await client.get(f"/v1/tasks/{task_id}", headers=headers)
            assert removed.status_code == 200 and removed.json()["can_edit"] is False


async def test_the_task_detail_says_who_may_curate_and_who_created_it(silent_dispatcher: None) -> None:
    """`can_curate` is the delete/archive rule (the creator, or the Project's owner) and `is_creator`
    the rename rule, both off the same operations the writes call - so the menu offers no verb the
    server would refuse. Bob (an editor) made the task; Ada owns the Project; Dee also edits; Cy views."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        dee, _ = await member(s, "Dee", role=WorkspaceRole.MEMBER, workspace=workspace)
        cy, _ = await member(s, "Cy", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"launch-{uuid.uuid4().hex[:6]}")).project
        for who, role in ((bob, "editor"), (dee, "editor"), (cy, "viewer")):
            s.add(ProjectMember(project_id=project.id, actor_id=who.id, role=role, added_by=ada.id))
        await s.flush()
        task = await task_ops.create_task(s, bob, goal="compare three trackers", project_id=project.id)
        s.add(User(actor_id=bob.id, email=f"bob{uuid.uuid4().hex[:8]}@example.com", name="Bob"))
        await s.flush()

        seen = {
            name: await get_task(task.id, who, s)
            for name, who in (("creator", bob), ("owner", ada), ("editor", dee), ("viewer", cy))
        }
        # Everyone is told who made it: "Waiting on Bob" on a connector approval
        # only he can allow reads the name off the detail, not off a replayed event.
        assert {v.creator_name for v in seen.values()} == {"Bob"}
        flags = {k: (v.can_curate, v.is_creator, v.can_edit) for k, v in seen.items()}
        assert flags == {
            "creator": (True, True, True),
            "owner": (True, False, True),  # tidies their own room, but did not make the work
            "editor": (False, False, True),  # writes into it, curates nothing of anyone else's
            "viewer": (False, False, False),
        }
        # the flag and the write are one rule: the editor is refused, the Project owner is not
        with pytest.raises(Forbidden):
            await task_ops.archive_task(s, dee, task.id)


async def test_a_personal_task_is_its_creators_to_curate(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, _workspace = await member(s, "Ada")
        task = await task_ops.create_task(s, ada, goal="a private memo")
        out = await get_task(task.id, ada, s)
        assert (out.can_curate, out.is_creator, out.can_edit) == (True, True, True)


async def test_the_task_detail_says_who_may_end_screen_control(silent_dispatcher: None) -> None:
    """`can_end_control` is the override: the Project's owner may end anyone's
    control of the browser; the task's creator, an editor and a viewer may not - they hand back
    their own. On a personal task its creator is the one who may. The pane offers "End {name}'s
    control" off this flag, and the screen release refuses by the same rule."""
    async with session_scope() as s:
        ada, workspace = await member(s, "Ada")
        bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
        dee, _ = await member(s, "Dee", role=WorkspaceRole.MEMBER, workspace=workspace)
        cy, _ = await member(s, "Cy", role=WorkspaceRole.MEMBER, workspace=workspace)
        project = (await project_ops.create(s, ada, name=f"launch-{uuid.uuid4().hex[:6]}")).project
        for who, role in ((bob, "editor"), (dee, "editor"), (cy, "viewer")):
            s.add(ProjectMember(project_id=project.id, actor_id=who.id, role=role, added_by=ada.id))
        await s.flush()
        task = await task_ops.create_task(s, bob, goal="update the admin app", project_id=project.id)
        seen = {
            name: (await get_task(task.id, who, s)).can_end_control
            for name, who in (("owner", ada), ("creator", bob), ("editor", dee), ("viewer", cy))
        }
        assert seen == {"owner": True, "creator": False, "editor": False, "viewer": False}
        solo = await task_ops.create_task(s, dee, goal="a private memo")
        assert (await get_task(solo.id, dee, s)).can_end_control is True
