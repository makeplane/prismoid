# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The result comes home: a chat-born task's delivery, failure, cancel and gates land in the
conversation it started from as rows (ADR-060's card), the inbox's finish-things law.

A task that moved away (`chat_record.hosts`) reports nothing here - a teammate's turn on it is
the Project's, never the owner's private thread.
"""

from __future__ import annotations

import uuid

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.models.artifact import Artifact
from prismoid.db.models.bot_channel import BotChannel
from prismoid.db.models.conversation import Conversation
from prismoid.db.models.message import Message
from prismoid.db.models.task import Task
from prismoid.operations import channels as channel_ops
from prismoid.operations.chat_record import append, hosts, notify
from prismoid.schemas.conversation import MessageRole

_SETTLE_KINDS = frozenset({"delivered", "failed", "cancelled"})

_SETTLE_TEXT = {
    "delivered": "Delivered.",
    "failed": "The task failed.",
    "cancelled": "The task was cancelled.",
}


def _came_home(rows: list[Message], turn: int) -> bool:
    """A settle row (or the bot's answer) for this turn is already in the thread."""
    for m in rows:
        settled = m.kind in _SETTLE_KINDS or (m.role == MessageRole.BOT.value and m.kind == "text")
        if settled and int((m.meta or {}).get("turn", 1)) == turn:
            return True
    return False


async def _report(
    session: AsyncSession, conv: Conversation, task: Task, *, outcome: str, summary: str, turn: int
) -> Message:
    """The turn's row: a later turn's delivery is the BOT's own line, anything else a SYSTEM row
    wearing the outcome; the turn's live artifacts ride it as chips. Flushes, never commits."""
    artifacts = [a for a in await Artifact.latest_for_task(session, task.id) if a.role != "superseded"]
    text = summary.strip() or _SETTLE_TEXT[outcome]
    continued = turn > 1 and outcome == "delivered"
    return await append(
        session,
        conv,
        role=MessageRole.BOT if continued else MessageRole.SYSTEM,
        kind="text" if continued else outcome,
        text=text,
        sender=conv.bot_name if continued else "",
        task_id=task.id,
        artifact_ids=[str(a.id) for a in artifacts],
        meta={"turn": turn},
    )


async def _carry_report(session: AsyncSession, conv: Conversation, task: Task, row: Message) -> None:
    """The report over the conversation's channel, if it still has one. Commits only when the
    transport failed, to write that failure on the row; takes no row lock."""
    channel = await session.get(BotChannel, conv.channel_id)
    if channel is None:
        return
    body = f"{row.text[:3500]}\n\nOpen in Plane: /tasks/{task.id}"
    if not await channel_ops.transport_reply(session, channel, conv, body):
        row.status = "failed"  # the row stands; the transport's failure is on it
        row.detail = f"not delivered over {channel.kind}"[:400]
        await notify(session, conv.id, row.seq)
        await session.commit()


async def on_task_settled(session: AsyncSession, task: Task, *, outcome: str, summary: str) -> None:
    """A chat-born task settled (`outcome` ∈ delivered | failed | cancelled). Its FIRST turn is
    the card: a SYSTEM row with the delivery (or the failure, or the cancel) and its artifacts.
    A LATER turn is a reply the person typed under that card, so its answer comes
    home as the BOT's own line - the summary as the text, the turn's artifacts as chips, the
    task on the row for Details - never a second "Delivered" receipt. A failure or a cancel
    stays a system row on every turn. Idempotent per turn: the cancel door and the workflow's
    own exit may both report, and the thread gets ONE row. Then the transport, if the
    conversation rides a channel. A task that moved away (`hosts`) reports nothing here - a
    teammate's turn on it is the Project's, never the owner's private thread. Never raises."""
    if task.origin_conversation_id is None or outcome not in _SETTLE_KINDS:
        return
    try:
        conv = await session.get(Conversation, task.origin_conversation_id)
        if conv is None or not hosts(conv, task):
            return
        turn = int(task.turn or 1)
        if _came_home(await Message.for_task(session, task.id), turn):
            return  # this turn already came home
        row = await _report(session, conv, task, outcome=outcome, summary=summary, turn=turn)
        await session.commit()
        if conv.channel_id is not None:
            await _carry_report(session, conv, task, row)
    except Exception:
        logfire.exception("conversation report for {task} failed; the delivery stands", task=str(task.id))


async def on_gate_opened(
    session: AsyncSession, task: Task, *, gate_id: uuid.UUID, title: str, body: str
) -> None:
    """A chat-born task asked something: the gate lands in the conversation as a SYSTEM row the
    web answers in place - unless the task moved away (`hosts`). Rides the caller's
    transaction (the gate's own)."""
    if task.origin_conversation_id is None:
        return
    conv = await session.get(Conversation, task.origin_conversation_id)
    if conv is None or not hosts(conv, task):
        return
    await append(
        session,
        conv,
        role=MessageRole.SYSTEM,
        kind="gate",
        text=title,
        task_id=task.id,
        gate_id=gate_id,
        detail=body,
    )
