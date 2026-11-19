# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The steer door: compare-and-set on the epoch, a 409 that names what was missed,
idempotency per task, retract by the author only, and cancel/fail rejecting what never applied."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.actor import Actor
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.plan import PlanStep
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.db.models.workspace import WorkspaceRole
from prismoid.db.session import row_scope, session_scope
from prismoid.errors import Conflict, Forbidden, NotFound, StaleSteer, ValidationFailed
from prismoid.operations import execution, gates, steering
from prismoid.operations import projects as project_ops
from prismoid.operations import tasks as task_ops
from prismoid.schemas.ids import StepId
from prismoid.schemas.plan import TaskStatus
from prismoid.schemas.steering import REJECT_CANCELLED, REJECT_FAILED
from tests.support.cast import member

pytestmark = pytest.mark.integration


async def _trio(s: AsyncSession) -> tuple[Actor, Actor, Actor, Task]:
    """Ada owns a Project; Bob edits; Cy views - and Ada's task there is running."""
    ada, workspace = await member(s, "Ada")
    bob, _ = await member(s, "Bob", role=WorkspaceRole.MEMBER, workspace=workspace)
    cy, _ = await member(s, "Cy", role=WorkspaceRole.MEMBER, workspace=workspace)
    project = (await project_ops.create(s, ada, name=f"d-{uuid.uuid4().hex[:6]}")).project
    s.add(ProjectMember(project_id=project.id, actor_id=bob.id, role="editor", added_by=ada.id))
    s.add(ProjectMember(project_id=project.id, actor_id=cy.id, role="viewer", added_by=ada.id))
    await s.flush()
    task = await task_ops.create_task(s, ada, goal="steer me", project_id=project.id)
    task.transition(TaskStatus.PLANNING)
    task.transition(TaskStatus.RUNNING)
    await s.commit()
    return ada, bob, cy, task


async def _rolled_back(s: AsyncSession, *cast: Actor) -> None:
    """A refused write leaves its transaction to the caller to roll back - and a rollback expires
    every loaded row, so the cast is read again before it acts."""
    await s.rollback()
    for who in cast:
        await s.refresh(who)


async def test_a_steer_bumps_the_epoch_and_labels_it(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, bob, _cy, task = await _trio(s)
        row = await steering.steer(s, bob, task.id, "use euros", issued_epoch=1)
        assert (row.epoch, row.state, row.actor_id) == (2, "accepted", bob.id)
        fresh = await s.get(Task, task.id)
        assert fresh is not None and (fresh.plan_epoch, fresh.epoch_actor_id) == (2, bob.id)
        edited = [e for e in await TaskEvent.replay(s, task.id, since=0) if e.type == "plan.edited"]
        assert edited[-1].edit.command_id == str(row.command_id) and edited[-1].edit.epoch == 2


async def test_a_steer_without_an_epoch_is_accepted_at_the_next_one(silent_dispatcher: None) -> None:
    """The chat path and older clients send no `issued_epoch`: never stale, still ordered."""
    async with session_scope() as s:
        _ada, bob, _cy, task = await _trio(s)
        first = await steering.steer(s, bob, task.id, "use euros")
        second = await steering.steer(s, bob, task.id, "and round to cents")
        assert (first.epoch, second.epoch) == (2, 3) and second.issued_epoch is None


async def test_a_stale_steer_is_a_409_naming_what_was_missed_and_writes_nothing(
    silent_dispatcher: None,
) -> None:
    async with session_scope() as s:
        ada, bob, _cy, task = await _trio(s)
        task_id = task.id
        await steering.steer(s, bob, task_id, "Name the module todo.py", issued_epoch=1)
        with pytest.raises(StaleSteer) as stale:
            await steering.steer(s, ada, task_id, "Name the module tasks.py", issued_epoch=1)
        assert stale.value.message == "Bob steered while you were typing: 'Name the module todo.py'"
        assert stale.value.missed[0]["author"] == "Bob" and stale.value.missed[0]["kind"] == "steer"
        await s.rollback()
        assert len(await Steer.since(s, task_id, 0)) == 1


async def test_a_steer_is_not_stale_for_its_authors_own_steers(silent_dispatcher: None) -> None:
    """R51: Bob steering twice from the epoch his screen showed missed nothing - both land, in order.
    Ada typing from that same epoch missed Bob's: hers is the 409, naming him."""
    async with session_scope() as s:
        ada, bob, _cy, task = await _trio(s)
        task_id = task.id
        first = await steering.steer(s, bob, task_id, "use euros", issued_epoch=1)
        second = await steering.steer(s, bob, task_id, "and round to cents", issued_epoch=1)
        assert (first.epoch, second.epoch) == (2, 3) and second.state == "accepted"
        with pytest.raises(StaleSteer) as stale:
            await steering.steer(s, ada, task_id, "use dollars", issued_epoch=1)
        assert [m["author"] for m in stale.value.missed] == ["Bob", "Bob"]
        assert stale.value.message == "Bob steered while you were typing: 'use euros'"
        await s.rollback()
        assert len(await Steer.since(s, task_id, 0)) == 2


async def test_a_mixed_stale_names_only_the_teammate(silent_dispatcher: None) -> None:
    """R51, the mixed case: Bob steered (epoch 2), then Ada (epoch 3); Bob typing from epoch 1
    missed only Ada's - his own steer is not news to him, so the 409 names her and only her."""
    async with session_scope() as s:
        ada, bob, _cy, task = await _trio(s)
        task_id = task.id
        await steering.steer(s, bob, task_id, "use euros", issued_epoch=1)
        await steering.steer(s, ada, task_id, "and keep it short", issued_epoch=2)
        with pytest.raises(StaleSteer) as stale:
            await steering.steer(s, bob, task_id, "round to cents", issued_epoch=1)
        assert [(m["kind"], m["author"]) for m in stale.value.missed] == [("steer", "Ada")]
        assert stale.value.message == "Ada steered while you were typing: 'and keep it short'"


async def test_an_authors_own_turn_start_still_makes_their_older_steer_stale(silent_dispatcher: None) -> None:
    """R51 forgives only an author's own STEERS: a turn they started themselves still moved the
    task on - a steer typed before it is a 409 naming that turn."""
    async with session_scope() as s:
        _ada, bob, _cy, task = await _trio(s)
        task_id = task.id
        task.transition(TaskStatus.DELIVERED)
        await s.commit()
        await task_ops.continue_task(s, bob, task_id, goal="now the pricing page")
        with pytest.raises(StaleSteer) as stale:
            await steering.steer(s, bob, task_id, "use euros", issued_epoch=1)
        assert [(m["kind"], m["author"]) for m in stale.value.missed] == [("turn", "Bob")]


async def test_a_steer_from_before_a_turn_started_names_the_turn(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, bob, _cy, task = await _trio(s)
        task_id = task.id
        task.transition(TaskStatus.DELIVERED)
        await s.commit()
        await task_ops.continue_task(s, bob, task_id, goal="now the pricing page")
        with pytest.raises(StaleSteer) as stale:
            await steering.steer(s, ada, task_id, "use euros", issued_epoch=1)
        assert stale.value.message == "Bob started the next turn"
        assert [m["kind"] for m in stale.value.missed] == ["turn"]


async def test_a_command_id_is_idempotent(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, bob, _cy, task = await _trio(s)
        cid = uuid.uuid4()
        first = await steering.steer(s, bob, task.id, "use euros", command_id=cid, issued_epoch=1)
        again = await steering.steer(s, bob, task.id, "use euros", command_id=cid, issued_epoch=1)
        assert again.id == first.id and len(await Steer.since(s, task.id, 0)) == 1
        fresh = await s.get(Task, task.id)
        assert fresh is not None and fresh.plan_epoch == 2, "a resend moves nothing"


async def test_review_focus_bad_epochs_and_blank_text_are_422(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, bob, _cy, task = await _trio(s)
        task_id = task.id
        for epoch in (-1, 99):
            with pytest.raises(ValidationFailed, match="unknown epoch"):
                await steering.steer(s, bob, task_id, "x", issued_epoch=epoch)
            await _rolled_back(s, bob)
        with pytest.raises(ValidationFailed):
            await steering.steer(s, bob, task_id, "   ", issued_epoch=1)
        await s.rollback()
        assert await Steer.since(s, task_id, 0) == []


async def test_a_viewer_cannot_steer_and_only_the_author_retracts(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, bob, cy, task = await _trio(s)
        task_id = task.id
        with pytest.raises(Forbidden, match=r"^viewers watch; ask the Project owner for edit access$"):
            await steering.steer(s, cy, task_id, "x", issued_epoch=1)
        await _rolled_back(s, ada, bob)
        cid = (await steering.steer(s, bob, task_id, "use euros", issued_epoch=1)).command_id
        with pytest.raises(Forbidden, match="only Bob can retract this steer"):
            await steering.retract(s, ada, task_id, cid)
        await _rolled_back(s, bob)
        done = await steering.retract(s, bob, task_id, cid)
        assert done.state == "retracted"
        retracted = [e for e in await TaskEvent.replay(s, task_id, since=0) if e.type == "steer.retracted"]
        assert len(retracted) == 1 and retracted[0].actor is not None and retracted[0].actor.name == "Bob"
        with pytest.raises(Conflict, match="retracted"):
            await steering.retract(s, bob, task_id, cid)
        await _rolled_back(s, bob)
        with pytest.raises(NotFound):
            await steering.retract(s, bob, task_id, uuid.uuid4())
        fresh = await s.get(Task, task_id)
        assert fresh is not None and fresh.plan_epoch == 2, "a retract never moves the epoch back"


async def test_cancel_rejects_what_never_applied(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        ada, bob, _cy, task = await _trio(s)
        row = await steering.steer(s, bob, task.id, "use euros", issued_epoch=1)
        await steering.cancel(s, ada, task.id)
        await s.refresh(row)
        assert row.state == "rejected" and row.reason == REJECT_CANCELLED
        assert any(e.type == "steer.rejected" for e in await TaskEvent.replay(s, task.id, since=0))
        with pytest.raises(ValidationFailed):
            await steering.steer(s, bob, task.id, "too late")


async def test_a_gates_cancel_rejects_what_never_applied_too(silent_dispatcher: None) -> None:
    """A budget or clarification gate answered `cancel` stops the task through `execution.advance`,
    not the header's door: the steer still says it never applied."""
    async with session_scope() as s:
        _ada, bob, _cy, task = await _trio(s)
        row = await steering.steer(s, bob, task.id, "use euros")
        await execution.advance(s, task.id, TaskStatus.CANCELLED)
        await s.commit()
        await s.refresh(row)
        assert (row.state, row.reason) == ("rejected", REJECT_CANCELLED)


async def test_fail_rejects_what_never_applied_and_leaves_a_retract_alone(silent_dispatcher: None) -> None:
    async with session_scope() as s:
        _ada, bob, _cy, task = await _trio(s)
        kept = await steering.steer(s, bob, task.id, "use euros")
        gone = await steering.steer(s, bob, task.id, "and pounds")
        await steering.retract(s, bob, task.id, gone.command_id)
        await execution.fail(s, task.id, "the model gave up")
        await s.commit()
        await s.refresh(kept)
        await s.refresh(gone)
        assert (kept.state, kept.reason) == ("rejected", REJECT_FAILED)
        assert gone.state == "retracted", "a settled steer keeps its first answer"
        rejected = [e for e in await TaskEvent.replay(s, task.id, since=0) if e.type == "steer.rejected"]
        assert [e.command_id for e in rejected] == [kept.command_id]


# ── a move between the editor check and the row lock ──────────────────────────────────────────


def _move_after_the_check(
    monkeypatch: pytest.MonkeyPatch, ada_id: uuid.UUID, task_id: uuid.UUID, to: uuid.UUID | None
) -> None:
    """Ada files the task in another Project the moment the door's editor check passes - committed
    before the door takes the row lock."""
    real = steering.require_project_editor
    armed = [True]

    async def check(session: AsyncSession, task: Task, actor: Actor) -> None:
        await real(session, task, actor)
        if armed:
            armed.clear()
            async with row_scope(Task, task_id) as other:
                ada = await other.get(Actor, ada_id)
                assert ada is not None
                await steering.move(other, ada, task_id, to)

    monkeypatch.setattr(steering, "require_project_editor", check)


async def _destination(s: AsyncSession, ada: Actor, where: str) -> uuid.UUID | None:
    """Where Ada moves the task: a Project of hers where Bob holds no role, or home (personal)."""
    if where == "home":
        return None
    project = (await project_ops.create(s, ada, name=f"o-{uuid.uuid4().hex[:6]}")).project
    await s.commit()
    return project.id


@pytest.mark.parametrize("where", ["elsewhere", "home"])
async def test_a_move_before_the_lock_refuses_a_steer_and_writes_nothing(
    where: str, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moved to a Project Bob has no role in, or home to Ada's personal work, the task is no longer
    Bob's to steer - and the approval it waits on stays open for Ada."""
    async with session_scope() as s:
        ada, bob, _cy, task = await _trio(s)
        task_id, ada_id = task.id, ada.id
        step = PlanStep(task_id=task_id, ordinal=0, title="send it")
        s.add(step)
        await s.flush()
        gate = await gates.open_approval(s, task, StepId(step.id), [("call_0", "Gmail", "send", "to: a")])
        await s.commit()
        to = await _destination(s, ada, where)
        _move_after_the_check(monkeypatch, ada_id, task_id, to)
        with pytest.raises(NotFound):
            await steering.steer(s, bob, task_id, "use euros", issued_epoch=1)
        await s.rollback()
        assert await Steer.since(s, task_id, 0) == []
        fresh = await s.get(Task, task_id)
        assert fresh is not None
        await s.refresh(fresh)
        assert (fresh.project_id, fresh.plan_epoch) == (to, 1)
        await s.refresh(gate)
        assert gate.is_open(), "a refused steer supersedes nothing"
        types = [e.type for e in await TaskEvent.replay(s, task_id, since=0)]
        assert "plan.edited" not in types and "gate.resolved" not in types


@pytest.mark.parametrize("where", ["elsewhere", "home"])
async def test_a_move_before_the_lock_refuses_a_retract_and_writes_nothing(
    where: str, silent_dispatcher: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with session_scope() as s:
        ada, bob, _cy, task = await _trio(s)
        task_id, ada_id = task.id, ada.id
        row = await steering.steer(s, bob, task_id, "use euros", issued_epoch=1)
        to = await _destination(s, ada, where)
        _move_after_the_check(monkeypatch, ada_id, task_id, to)
        with pytest.raises(NotFound):
            await steering.retract(s, bob, task_id, row.command_id)
        await s.rollback()
        await s.refresh(row)
        assert row.state == "accepted"
        types = [e.type for e in await TaskEvent.replay(s, task_id, since=0)]
        assert "steer.retracted" not in types
