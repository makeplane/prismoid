# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Conversations - talk with a bot (ADR-057). A conversation executes nothing: `send` returns
the door's verdict (202) and the worker answers on the thread; work mints an ordinary Task.
Every route requires an Actor; ownership is the ONE fetch path (`Conversation.get_for`).
"""

from __future__ import annotations

from uuid import UUID

from prismoid.api.deps import ActorDep, SessionDep
from prismoid.api.serializers.conversations import (
    ConversationCreateIn,
    ConversationOut,
    ConversationUpdateIn,
    MessageIn,
    MessageOut,
    SaveIn,
    SendOut,
    conversation_out,
    message_out,
)
from prismoid.operations import chat as ops


async def create_conversation(
    body: ConversationCreateIn, actor: ActorDep, session: SessionDep
) -> ConversationOut:
    row = await ops.open_conversation(
        session, actor, bot_name=body.bot, project_id=body.project_id, title=body.title
    )
    return conversation_out(row)


async def list_conversations(
    actor: ActorDep, session: SessionDep, bot: str, project_id: UUID | None = None
) -> list[ConversationOut]:
    rows = await ops.list_conversations(session, actor, bot_name=bot, project_id=project_id)
    return [conversation_out(r) for r in rows]


async def get_conversation(conversation_id: UUID, actor: ActorDep, session: SessionDep) -> ConversationOut:
    return conversation_out(await ops.get_conversation(session, actor, conversation_id))


async def update_conversation(
    conversation_id: UUID, body: ConversationUpdateIn, actor: ActorDep, session: SessionDep
) -> ConversationOut:
    row = await ops.update_conversation(
        session, actor, conversation_id, title=body.title, archived=body.archived
    )
    return conversation_out(row)


async def list_messages(
    conversation_id: UUID, actor: ActorDep, session: SessionDep, since: int = 0, limit: int = 200
) -> list[MessageOut]:
    rows = await ops.messages(session, actor, conversation_id, since=since, limit=min(max(limit, 1), 500))
    return [message_out(m) for m in rows]


async def send_message(
    conversation_id: UUID, body: MessageIn, actor: ActorDep, session: SessionDep
) -> SendOut:
    out = await ops.send(
        session,
        actor,
        conversation_id,
        body.text,
        gesture=body.start_task,
        reply_to_task_id=body.reply_to_task,
    )
    return SendOut(
        seq=out.message.seq,
        verdict=out.verdict.verdict.value,
        reason=out.verdict.reason,
        task_id=out.task.id if out.task else None,
    )


async def start_proposal(conversation_id: UUID, seq: int, actor: ActorDep, session: SessionDep) -> SendOut:
    task = await ops.start_proposal(session, actor, conversation_id, seq)
    return SendOut(seq=seq, verdict="start", reason="a tap on the proposal", task_id=task.id)


async def save_reply(
    conversation_id: UUID, seq: int, body: SaveIn, actor: ActorDep, session: SessionDep
) -> SendOut:
    task = await ops.save_reply_as_file(session, actor, conversation_id, seq, format=body.format)
    return SendOut(seq=seq, verdict="start", reason=f"saved as a {body.format} file", task_id=task.id)


async def decline_proposal(
    conversation_id: UUID, seq: int, actor: ActorDep, session: SessionDep
) -> MessageOut:
    return message_out(await ops.decline_proposal(session, actor, conversation_id, seq))
