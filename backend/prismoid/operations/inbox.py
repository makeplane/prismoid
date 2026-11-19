# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Inbox - what the system tells a human when they were not watching.

Every item is addressed to an **Actor**, and the two that matter are the two ends of a task's life:
it worked, and it did not. **A task that fails silently is a task the user finds out about by
noticing nothing happened**, which is the worst way to find out anything.

`operations` writes them; `api/endpoints/inbox.py` reads them. Nothing else does - an inbox
that anything can write to is a notification system, and a notification system is something people
learn to ignore.

**Unread means not seen yet.** Every item is written (the inbox stays the record), and an item
stops being unread when its person reads it here, opens or watches its task (`mark_task_read`),
or when the gate it asked about closes (`gates_closed`).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections import defaultdict
from collections.abc import Collection
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models import tenant
from prismoid.db.models.actor import Actor
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.models.project import Project, Role
from prismoid.db.models.project_member import ProjectMember
from prismoid.db.models.task import Task
from prismoid.operations import push
from prismoid.schemas.plan import QuestionSet, TaskEnding
from prismoid.schemas.verification import Diagnosis


async def _recipients(session: AsyncSession, task: Task, *, editors_only: bool = False) -> list[uuid.UUID]:
    """The creator AND the current turn's author, once each - the author only while
    they can still SEE the task (`Task._visible_to`: a member of its Project, or its owner; a
    personal task is its creator's alone), and, for a knock that asks them to act
    (`editors_only`: a gate), only while they can still answer it (editor+). A person removed
    from the Project, or a task taken home, is not knocked about work they cannot open. The creator
    is always knocked - the item is how they find their own task."""
    ids = [task.creator_actor_id]
    author = task.turn_actor_id
    if author is None or author == task.creator_actor_id or task.project_id is None:
        return ids
    project = await session.get(Project, task.project_id)
    if project is None:  # pragma: no cover - the FK keeps it
        return ids
    member = await ProjectMember.get(session, project.id, author)
    role = project.role_of(author, {author: Role(member.role)} if member is not None else {})
    if role is None or (editors_only and role is Role.VIEWER):
        return ids
    return [*ids, author]


async def _every_editor(session: AsyncSession, task: Task) -> list[uuid.UUID]:
    """The creator, then everyone who may act on the task as an editor: its Project's owner and every
    editor member, once each (a personal task is its creator's alone). A viewer is never here."""
    ids = [task.creator_actor_id]
    if task.project_id is None:
        return ids
    project = await session.get(Project, task.project_id)
    if project is None:  # pragma: no cover - the FK keeps it
        return ids
    editors = [project.owner_actor_id] + [
        m.actor_id
        for m in await ProjectMember.for_project(session, project.id)
        if Role(m.role) is Role.EDITOR
    ]
    return ids + [actor_id for actor_id in dict.fromkeys(editors) if actor_id not in ids]


async def _knock(
    session: AsyncSession,
    task: Task,
    *,
    creator_only: bool = False,
    editors_only: bool = False,
    every_editor: bool = False,
    **fields: Any,
) -> InboxItem:
    """One item per recipient (`fields` are `_add`'s: kind, title, body, gate_id, payload); the
    creator's - always first - is what the caller gets back. `creator_only` is for a knock only
    the creator can act on (a gate that runs on THEIR connector credential): the turn's author
    would be told about a button they cannot press. `editors_only` is for a knock any editor
    can act on (a question, a browser approval) - the author hears it only while an editor.
    `every_editor` is for a knock ANY editor should hear, not only the turn's author: a hand-off
    asks for whoever can take control."""
    if creator_only:
        recipients = [task.creator_actor_id]
    elif every_editor:
        recipients = await _every_editor(session, task)
    else:
        recipients = await _recipients(session, task, editors_only=editors_only)
    items = [await _add(session, actor_id=actor_id, task=task, **fields) for actor_id in recipients]
    return items[0]


async def escalated(session: AsyncSession, task: Task, diagnosis: Diagnosis) -> InboxItem:
    """We gave up, and this is the note that says so - **with the diagnosis, not a shrug.**

    Addressed to whoever asked for the work - the creator and the current turn's author,
    one item each. They did not watch the three attempts; they need what was tried, what
    was wrong with it each time, and what they can do about it.
    """
    body = "\n".join(
        [
            diagnosis.why,
            "",
            "**What to try next**",
            *(f"- {s}" for s in diagnosis.suggested_next),
        ]
    )
    return await _knock(
        session,
        task,
        kind=InboxKind.ESCALATION,
        title=f"Could not finish: {task.goal[:120]}",
        body=body,
        payload={"diagnosis": diagnosis.model_dump(mode="json")},
    )


# A settled turn's title says what its answer says it did: a declined or partial answer never reads "Delivered".
_SETTLED: dict[TaskEnding, str] = {
    TaskEnding.DONE: "Delivered",
    TaskEnding.PARTIAL: "Partly done",
    TaskEnding.COULD_NOT: "Couldn't do it",
}


async def task_completed(
    session: AsyncSession, task: Task, summary: str, ending: TaskEnding = TaskEnding.DONE
) -> InboxItem:
    """It is settled, and here is what it did - written for someone who was not watching."""
    return await _knock(
        session,
        task,
        kind=InboxKind.TASK_COMPLETED,
        title=f"{_SETTLED[ending]}: {task.goal[:120]}",
        body=summary,
        payload={"ending": ending.value},
    )


async def budget_reached(
    session: AsyncSession, task: Task, reason: str, *, gate_id: uuid.UUID | None = None
) -> InboxItem:
    """The Bursar stopped a task at its ceiling. The owner is told, not surprised by an
    invoice - a budget you learn about after the spend is a receipt. The knock carries its gate,
    so the raise or the cancel that closes it also reads it (`gates_closed`)."""
    return await _knock(
        session, task, kind=InboxKind.BUDGET, title="A task reached its budget", body=reason, gate_id=gate_id
    )


# ── routines - the silence rule's outputs. Flush-only like every writer here:
# the settle path (deliver/fail) and the fire gate own the transaction these ride in.


async def routine_run(
    session: AsyncSession, task: Task, *, routine_id: uuid.UUID, routine_name: str, title: str, body: str
) -> InboxItem:
    """One run's noteworthy outcome - a delivery the notify policy chose to surface, a failure,
    or a recovery. The run always journals; this is only the ping."""
    return await _add(
        session,
        actor_id=task.creator_actor_id,
        kind=InboxKind.ROUTINE_RUN,
        title=title,
        body=body,
        task=task,
        payload={"routine_id": str(routine_id), "routine_name": routine_name},
    )


async def routine_held(
    session: AsyncSession, task: Task, *, routine_id: uuid.UUID, routine_name: str
) -> InboxItem:
    """The `hold` posture's knock: the plan is minted and waiting - the owner releases it from
    the task page (Apple's ask-before-running, on the fork-born-held machinery)."""
    return await _add(
        session,
        actor_id=task.creator_actor_id,
        kind=InboxKind.ROUTINE_HELD,
        title=f"{routine_name} is holding a run for you",
        body="The task is minted and held - open it to review the plan and set it going.",
        task=task,
        payload={"routine_id": str(routine_id), "routine_name": routine_name},
    )


async def routine_paused(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    routine_id: uuid.UUID,
    routine_name: str,
    why: str,
    task: Task | None = None,
    title: str | None = None,
) -> InboxItem:
    """An auto-pause ALWAYS notifies (whatever the notify policy) - and the note carries the
    one-click resume, because a warning without the button is the GH-Actions gap.
    `title` lets the other pausers speak honestly: the default wording is the LADDER's voice
    ("paused itself"), which is a lie on an operator stop or an agent proposal."""
    return await _add(
        session,
        actor_id=actor_id,
        kind=InboxKind.ROUTINE_PAUSED,
        title=title or f"{routine_name} paused itself",
        body=f"{why}\n\nResume it when you're ready - `prismoid routines resume {routine_id}`.",
        task=task,
        payload={"routine_id": str(routine_id), "routine_name": routine_name, "why": why},
    )


async def routine_retired(
    session: AsyncSession, *, actor_id: uuid.UUID, routine_id: uuid.UUID, routine_name: str, why: str
) -> InboxItem:
    """A season completing is news, not noise: standing ≠ eternal, and the retirement says why."""
    return await _add(
        session,
        actor_id=actor_id,
        kind=InboxKind.ROUTINE_RETIRED,
        title=f"{routine_name} finished its season",
        body=why,
        payload={"routine_id": str(routine_id), "routine_name": routine_name},
    )


async def gate_opened(
    session: AsyncSession,
    task: Task,
    *,
    gate_id: uuid.UUID,
    title: str,
    body: str,
    creator_only: bool = False,
    every_editor: bool = False,
) -> InboxItem:
    """A gate that needs a human, for the human who was not watching (the
    `GATE_OPENED` kind, approval gates first). The pointer IS the product:
    the item carries `gate_id`, so the inbox can resolve it in place. `creator_only` for a gate
    only the creator may accept (connector approvals and connect requests run on THEIR
    credential); a browser approval is any editor's, so it goes to the creator and the turn's
    author; `every_editor` for a gate whoever is free should take - a hand-off asks for someone
    at the browser, so every editor of the Project hears it (and the creator)."""
    return await _knock(
        session,
        task,
        creator_only=creator_only,
        editors_only=True,
        every_editor=every_editor,
        kind=InboxKind.GATE_OPENED,
        title=title,
        body=body,
        gate_id=gate_id,
    )


QUESTION_TITLE_MAX = 160


async def question(session: AsyncSession, task: Task, *, gate_id: uuid.UUID, asked: QuestionSet) -> InboxItem:
    """A running step asked the person ONE batch (an INPUT gate), for whoever was not watching:
    "{task} asks: {first question}", the batch numbered in the body, the gate on the item so the
    answer is one tap from the knock. Whoever can answer hears it: the creator and the turn's
    author while an editor - the approval gate's rule."""
    first = asked.questions[0].prompt
    more = len(asked.questions) - 1
    title = f"{task.title or task.goal} asks: {first}" + (f" (+{more} more)" if more else "")
    if len(title) > QUESTION_TITLE_MAX:
        title = title[: QUESTION_TITLE_MAX - 1].rstrip() + "…"
    lines = [f"{n}. {q.prompt}" for n, q in asked.numbered()]
    body = "\n".join([asked.context, "", *lines] if asked.context else lines)
    return await _knock(
        session, task, editors_only=True, kind=InboxKind.QUESTION, title=title, body=body, gate_id=gate_id
    )


async def connector_broken(
    session: AsyncSession, *, actor_id: uuid.UUID, name: str, slug: str, why: str
) -> InboxItem:
    """A connector's credential died between tasks.
    Written only on the TRANSITION edge - the caller checks the previous
    status, so a connector that stays broken knocks once, not per probe."""
    return await _add(
        session,
        actor_id=actor_id,
        kind=InboxKind.CONNECTOR_BROKEN,
        title=f"{name} needs to be reconnected",
        body=f"{why}\n\nReconnect it from the connectors page, or: prismoid connectors authorize {slug}",
        payload={"connector_slug": slug},
    )


async def project_shared(
    session: AsyncSession, *, actor_id: uuid.UUID, project: Project, by_name: str
) -> InboxItem:
    """You were added to a Project - by email or through a link - and can now see its tasks."""
    return await _add(
        session,
        actor_id=actor_id,
        kind=InboxKind.PROJECT_SHARED,
        title=f"{by_name} added you to {project.name}",
        body="Everyone in this Project sees every task in it.",
        project_id=project.id,
        payload={"project_id": str(project.id)},
    )


async def project_removed(
    session: AsyncSession, *, actor_id: uuid.UUID, project: Project, by_name: str
) -> InboxItem:
    """You were removed from a Project. No project pointer: the room is no longer yours to open, and
    the item says only what the person already knew - its name and who removed them."""
    return await _add(
        session,
        actor_id=actor_id,
        kind=InboxKind.PROJECT_REMOVED,
        title=f"{by_name} removed you from {project.name}",
        body="You no longer see this Project or its tasks.",
        payload={"project_id": str(project.id), "project_name": project.name},
    )


async def card_waiting(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    bot_name: str,
    conversation_id: uuid.UUID,
    seq: int,
    sender: str,
    goal: str,
) -> InboxItem | None:
    """Someone other than the owner asked the bot for work, and the card waits for the owner's Start.
    One unread knock per conversation: a sender asking again does not stack items."""
    if await InboxItem.unread_about(
        session, actor_id, InboxKind.CARD_WAITING.value, "conversation_id", str(conversation_id)
    ):
        return None
    return await _add(
        session,
        actor_id=actor_id,
        kind=InboxKind.CARD_WAITING,
        title=f"{sender or 'Someone'} asked {bot_name} for work",
        body=f"{goal}\n\nStart it or say Not now on the card in {bot_name}'s chat.",
        payload={
            "bot_name": bot_name,
            "conversation_id": str(conversation_id),
            "seq": seq,
            "sender": sender,
        },
    )


async def routine_fire_failed(
    session: AsyncSession, *, actor_id: uuid.UUID, routine_id: uuid.UUID, routine_name: str, why: str
) -> InboxItem:
    """A fire that could not become a task - a refused mint or a crash. No task to point at; the
    knock lands on the routine, whose history holds the error row."""
    return await _add(
        session,
        actor_id=actor_id,
        kind=InboxKind.ROUTINE_RUN,
        title=f"{routine_name} could not start a run",
        body=f"{why}\n\nFix the routine, then Run now to try again.",
        payload={"routine_id": str(routine_id), "routine_name": routine_name},
    )


async def mark_read(session: AsyncSession, actor: Actor, item_id: uuid.UUID) -> InboxItem:
    """**Operations own transactions**, and this one exists because the view tried to.

    `just audit` caught it: two `session.commit()` calls in `api/endpoints/inbox.py`. It is a
    two-line write and it *works* perfectly well from the view - which is exactly why the rule is
    mechanical rather than a matter of taste. A codebase where views commit "when it is simple
    enough" is a codebase where the boundary is a judgement call, and the next one is not simple.
    """
    item = await InboxItem.get_for(session, item_id, actor)  # NotFound, never Forbidden
    if item.is_unread():
        item.read_at = at = utcnow()
        await push.announce_read(session, actor.id, item.workspace_id, item_ids=[item.id], at=at)
        await session.commit()
    return item


async def mark_all_read(session: AsyncSession, actor: Actor) -> int:
    """Read everything, then tell the person's phones (only when something changed)."""
    at = utcnow()
    count = await InboxItem.mark_all_read(session, actor, at)
    if count:
        await push.announce_read(session, actor.id, tenant.workspace_of(session), item_ids=None, at=at)
    await session.commit()
    return count


async def mark_task_read(session: AsyncSession, actor: Actor, task_id: uuid.UUID) -> tuple[int, int]:
    """**Unread means not seen yet**, and a person looking at the task has seen what it would tell
    them: the task page calls this when it opens, and again when its stream brings news (a gate,
    an ending). Only the caller's own items: a teammate who has not looked still has theirs.
    A task the caller cannot see is NotFound, like every other door to it - another workspace's
    id answers exactly as a made-up one does. The caller's phones hear what it read. Commits;
    returns how many it read and how many of the caller's stay unread, so a client refreshes its
    count only when one moved."""
    task = await Task.get_for(session, task_id, actor)
    at = utcnow()
    marked = await InboxItem.mark_task_read(session, actor, task_id, at)
    await push.announce_read(session, actor.id, task.workspace_id, item_ids=marked, at=at)
    await session.commit()
    return len(marked), await InboxItem.unread_count(session, actor)


async def gates_closed(
    session: AsyncSession, task: Task, gate_ids: Collection[uuid.UUID], *, at: dt.datetime | None = None
) -> int:
    """These gates closed (answered, expired, superseded, lifted, or answered by the system): the
    knocks that asked someone to act on them read, for every recipient, since nobody can act any
    more. Flush-only, in the transaction that closes them - a knock still unread over a closed
    gate is the inbox telling someone they are needed when they are not. Every closer has bound
    the task's workspace first (`resolve`'s door, `close_as_system`, the effect's row scope).
    Each recipient's phones hear which of theirs read, once the closing transaction commits."""
    at = at or utcnow()
    read: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for item_id, actor_id in await InboxItem.mark_gates_read(session, gate_ids, at):
        read[actor_id].append(item_id)
    for actor_id, item_ids in read.items():
        await push.announce_read(session, actor_id, task.workspace_id, item_ids=item_ids, at=at)
    return sum(len(item_ids) for item_ids in read.values())


async def _add(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    kind: InboxKind,
    title: str,
    body: str,
    task: Task | None = None,
    project_id: uuid.UUID | None = None,
    gate_id: uuid.UUID | None = None,
    payload: dict[str, object] | None = None,
) -> InboxItem:
    """The one writer. `project_id` is for an item about a Project with no task (PROJECT_SHARED); a
    task's item takes the task's Project."""
    item = InboxItem(
        id=uuid.uuid4(),
        actor_id=actor_id,
        kind=kind.value,
        title=title[:400],
        body=body[:4000],
        task_id=task.id if task else None,
        project_id=task.project_id if task else project_id,
        gate_id=gate_id,
        payload=payload or {},
    )
    session.add(item)
    await session.flush()
    await push.announce(session, item)  # the phone hears it once the caller's transaction commits
    return item
