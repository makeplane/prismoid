# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Deleting a task removes everything it owns and nothing it does not.

The contract, stated as tests because a delete is the one operation you cannot take back:

* CASCADE - its artifacts, events, plan, gates, inbox, memory-uses go with it;
* SET NULL - the billing ledger, minted memories, and follow-up children SURVIVE (spend is
  attributed at the time it happened and never rewritten; a user's memory outlives the task;
  a follow-up becomes a root task);
* content-addressed blob GC deletes only what nothing else references (dedup safety);
* only a terminal task deletes; only the creator or the project owner may.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid import storage
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.artifact import Artifact
from prismoid.db.models.event import TaskEvent
from prismoid.db.models.memory import MemoryKind, MemoryScope, MemoryUse
from prismoid.db.models.project import Project
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.db.models.usage import UsageEvent
from prismoid.db.session import session_scope
from prismoid.errors import Conflict, Forbidden, NotFound
from prismoid.operations import chat as chat_ops
from prismoid.operations import memory as memory_ops
from prismoid.operations import tasks as task_ops
from prismoid.operations import usage as usage_ops
from prismoid.schemas.ids import ProjectId, TaskId
from prismoid.schemas.inputs import RawInput
from prismoid.schemas.model_class import Family
from prismoid.schemas.usage import Unit, UsageRecord
from tests.support.cast import member
from tests.support.chat_cast import published_bot

pytestmark = pytest.mark.integration


@pytest.fixture
async def db() -> AsyncIterator[AsyncSession]:
    async with session_scope() as session:
        yield session


async def _terminal_task(db: AsyncSession, actor: Actor, *, project_id: uuid.UUID | None = None) -> Task:
    task = await task_ops.create_task(db, actor, goal="do a thing", project_id=project_id)
    task.status = "delivered"  # a delete needs a terminal task
    await db.commit()
    return task


def _artifact(task_id: uuid.UUID, sha: str, path: str = "/artifacts/memo.md") -> Artifact:
    return Artifact(
        id=uuid.uuid4(),
        lineage_id=uuid.uuid4(),
        task_id=task_id,
        type="document",
        title="memo",
        sha256=sha,
        bytes=3,
        storage_key=storage.key_for(sha),
        provenance={"path": path, "sources": []},
    )


async def test_delete_cascades_owned_rows_and_preserves_the_ledger(db: AsyncSession) -> None:
    actor, _ = await member(db, "Ana")
    await db.flush()
    project = Project(id=uuid.uuid4(), name="Room", owner_actor_id=actor.id)
    db.add(project)
    await db.flush()

    task = await _terminal_task(db, actor, project_id=project.id)
    # an event, an artifact, and a billing row
    seq = await task.next_seq(db)
    from prismoid.schemas.events import TaskDelivered

    await TaskEvent.append(db, TaskDelivered(seq=seq, task_id=TaskId(task.id), at=utcnow(), summary="done"))
    db.add(_artifact(task.id, "a" * 64))
    await usage_ops.record(
        db,
        UsageRecord(
            task_id=TaskId(task.id),
            project_id=ProjectId(project.id),
            provider="local",
            model="llama3.2:3b",
            family=Family("llama"),
            units={Unit.INPUT_TOKEN: 10, Unit.REQUEST: 1},
            cost_usd=Decimal("0.001"),
            price_version=1,
            at=utcnow(),
        ),
    )
    await db.commit()
    tid = task.id

    await task_ops.delete_task(db, actor, tid)

    # the task and its owned rows are gone
    with pytest.raises(NotFound):
        await task_ops.get_task(db, actor, tid)
    assert await db.scalar(select(func.count()).where(Artifact.task_id == tid)) == 0
    assert await db.scalar(select(func.count()).where(TaskEvent.task_id == tid)) == 0

    # the ledger SURVIVES, with the task link nulled - spend is never un-recorded
    rows = list(await db.scalars(select(UsageEvent).where(UsageEvent.project_id == project.id)))
    assert len(rows) == 1 and rows[0].task_id is None and rows[0].cost_usd == Decimal("0.001")


async def test_minted_memories_and_follow_up_children_survive(db: AsyncSession) -> None:
    actor, _ = await member(db, "Bo")
    await db.flush()
    parent = await _terminal_task(db, actor)

    # a memory the archivist minted FROM this task, and a use record
    mem = await memory_ops.create(
        db, actor, scope=MemoryScope.USER, kind=MemoryKind.PREFERENCE, text="Prefers concise memos."
    )
    mem.source_task_id = parent.id
    MemoryUse.record_all(db, [mem.id], parent.id)
    # a follow-up child
    child = await task_ops.create_task(db, actor, goal="translate it", parent_task_id=parent.id)
    await db.commit()

    await task_ops.delete_task(db, actor, parent.id)

    # the memory survives, its provenance link nulled - a memory outlives the task that taught it
    await db.refresh(mem)
    assert mem.source_task_id is None and mem.status == "active"
    # its use record was the task's own - CASCADE removed it
    assert await db.scalar(select(func.count()).where(MemoryUse.task_id == parent.id)) == 0
    # the follow-up becomes a root task rather than vanishing
    await db.refresh(child)
    assert child.parent_task_id is None


async def test_blob_gc_respects_dedup(db: AsyncSession) -> None:
    actor, _ = await member(db, "Cy")
    await db.flush()

    shared = "c" * 64  # published by TWO tasks (content-addressed dedup)
    lonely = "d" * 64  # only this task's - no real blobs needed; GC only chooses what to delete

    doomed = await _terminal_task(db, actor)
    keeper = await _terminal_task(db, actor)
    db.add(_artifact(doomed.id, shared))
    db.add(_artifact(doomed.id, lonely, path="/artifacts/only.md"))
    db.add(_artifact(keeper.id, shared, path="/artifacts/copy.md"))
    await db.commit()

    deleted: list[str] = []
    import prismoid.operations.tasks as mod

    orig = storage.delete
    mod_storage = mod.storage
    mod_storage.delete = lambda d, bucket=None: deleted.append(d)
    try:
        await task_ops.delete_task(db, actor, doomed.id)
    finally:
        mod_storage.delete = orig

    # `lonely` had no other reference → GC'd; `shared` still backs keeper's artifact → kept.
    assert deleted == [lonely], deleted
    assert await Artifact.any_with_digest(db, shared) is True


async def test_blob_gc_collects_and_respects_rendition_pointers(db: AsyncSession) -> None:
    """Renditions added a THIRD pointer class. Deleting a task must collect its renditions (they
    are unregenerable - leaking one is forever), and a digest still referenced as ANOTHER row's
    rendition must survive a delete that only knew it as content."""
    actor, _ = await member(db, "Dee")
    await db.flush()

    own_rendition = "e" * 64  # only the doomed task's rendition points here → collected
    borrowed = "f" * 64  # doomed's CONTENT digest, but ALSO keeper's rendition → kept

    doomed = await _terminal_task(db, actor)
    keeper = await _terminal_task(db, actor)
    gone = _artifact(doomed.id, borrowed)
    gone.rendition_sha256 = own_rendition
    gone.rendition_media = "application/pdf"
    kept = _artifact(keeper.id, "a1" * 32, path="/artifacts/kept.md")
    kept.rendition_sha256 = borrowed
    kept.rendition_media = "text/html"
    db.add_all([gone, kept])
    await db.commit()

    deleted: list[str] = []
    import prismoid.operations.tasks as mod

    orig = storage.delete
    mod.storage.delete = lambda d, bucket=None: deleted.append(d)
    try:
        await task_ops.delete_task(db, actor, doomed.id)
    finally:
        mod.storage.delete = orig

    assert own_rendition in deleted, "the doomed task's rendition blob must be collected"
    assert borrowed not in deleted, "keeper's rendition still points at it - a live blob"
    assert await Artifact.any_with_digest(db, borrowed) is True, "the rendition pointer counts"


async def test_blob_gc_keeps_a_blob_a_chat_line_holds(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file sent on a conversation line lives in `meta.inputs`; a task that published the same bytes
    may go, and the line's file stays."""
    actor, _ = await member(db, "Eve")
    await db.flush()
    conv = await chat_ops.open_conversation(db, actor, bot_name=await published_bot(db, actor))
    line = await chat_ops.send(
        db, actor, conv.id, "keep this", inputs=[RawInput(filename="a.md", data=b"# A")]
    )
    held = str(line.message.meta["inputs"][0]["sha256"])

    doomed = await _terminal_task(db, actor)
    db.add(_artifact(doomed.id, held))
    await db.commit()

    deleted: list[str] = []
    monkeypatch.setattr(storage, "delete", lambda d, bucket=None: deleted.append(d))
    await task_ops.delete_task(db, actor, doomed.id)
    assert held not in deleted, "a chat line still holds the blob"


async def test_a_running_task_will_not_delete(db: AsyncSession) -> None:
    actor, _ = await member(db, "Di")
    await db.flush()
    task = await task_ops.create_task(db, actor, goal="live one")
    task.status = "running"
    await db.commit()
    with pytest.raises(Conflict, match="cancel the task"):
        await task_ops.delete_task(db, actor, task.id)


async def test_the_project_owner_sees_and_can_delete_a_members_task(db: AsyncSession) -> None:
    """The owner outranks any membership row, so they see the whole room - including a task a
    member filed (the `_visible_to` owner clause) - and may delete it. A non-owner editor cannot
    delete another's task; a stranger cannot see it at all."""
    owner, editor, viewer, stranger = [(await member(db, n))[0] for n in ("Own", "Edit", "View", "Str")]
    await db.flush()
    project = Project(id=uuid.uuid4(), name="Team", owner_actor_id=owner.id)
    db.add(project)
    await db.flush()
    db.add_all(
        [
            ProjectMember(project_id=project.id, actor_id=editor.id, role="editor", added_by=owner.id),
            ProjectMember(project_id=project.id, actor_id=viewer.id, role="viewer", added_by=owner.id),
        ]
    )
    await db.flush()

    task = await _terminal_task(db, editor, project_id=project.id)  # the editor's own task

    # the OWNER (a column, not a member row) now sees it - the fix - and may delete it.
    assert (await task_ops.get_task(db, owner, task.id)).id == task.id
    # a fellow member who is neither creator nor owner cannot delete another's task.
    with pytest.raises(Forbidden, match="creator or the project owner"):
        await task_ops.delete_task(db, viewer, task.id)
    # a stranger cannot even see it → NotFound (existence is never leaked).
    with pytest.raises(NotFound):
        await task_ops.delete_task(db, stranger, task.id)
    # the owner deletes it.
    await task_ops.delete_task(db, owner, task.id)
    with pytest.raises(NotFound):
        await task_ops.get_task(db, owner, task.id)


async def test_the_creator_can_delete_their_own_task(db: AsyncSession) -> None:
    creator, _ = await member(db, "Cr")
    await db.flush()
    task = await _terminal_task(db, creator)  # spaceless - only the creator sees it
    await task_ops.delete_task(db, creator, task.id)
    with pytest.raises(NotFound):
        await task_ops.get_task(db, creator, task.id)
