# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Conversations - request bodies and responses (ADR-057). `*In` = request, `*Out` = response."""

from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from prismoid.db.models.conversation import Conversation
from prismoid.db.models.message import Message
from prismoid.schemas.conversation import (
    MAX_MESSAGE_CHARS,
    ChatActivity,
    ChatDelta,
    ChatEnd,
    ChatStart,
    DocumentFormat,
)


class ConversationCreateIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    bot: str = Field(min_length=1, max_length=64)
    project_id: UUID | None = None
    title: str | None = Field(default=None, max_length=200)


class ConversationUpdateIn(BaseModel):
    model_config = ConfigDict(frozen=True)

    title: str | None = Field(default=None, max_length=200)
    archived: bool | None = None


class ConversationOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID
    bot_name: str
    title: str | None
    source: str
    project_id: UUID | None
    channel_id: UUID | None
    thread_key: str
    message_seq: int
    replying: bool
    unread: int
    last_message_at: dt.datetime | None
    archived_at: dt.datetime | None
    created_at: dt.datetime


def conversation_out(row: Conversation) -> ConversationOut:
    return ConversationOut(
        id=row.id,
        bot_name=row.bot_name,
        title=row.title,
        source=row.source,
        project_id=row.project_id,
        channel_id=row.channel_id,
        thread_key=row.thread_key,
        message_seq=row.message_seq,
        replying=row.replying,
        unread=row.unread,
        last_message_at=row.last_message_at,
        archived_at=row.archived_at,
        created_at=row.created_at,
    )


class MessageIn(BaseModel):
    """One line from the owner. `start_task` is the explicit gesture (the Start-as-task
    button); `reply_to_task` anchors the line on a task card (steer / follow-up)."""

    model_config = ConfigDict(frozen=True)

    text: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    start_task: bool = False
    reply_to_task: UUID | None = None


class SaveIn(BaseModel):
    """Save a bot's reply as a file: the format the one tap asks for."""

    model_config = ConfigDict(frozen=True)

    format: str = Field(default="pdf", pattern=r"^(md|docx|pdf|html)$")


class ProposalOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    goal: str
    deliverable: str | None = None
    why: str = ""
    direct: bool = False
    status: str = "pending"
    task_id: UUID | None = None
    format: DocumentFormat | None = None  # a document's file format; None for any other kind


class MessageOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    seq: int
    role: str  # user | bot | system
    kind: str  # text | started | continued | delivered | failed | gate
    text: str
    sender: str
    verdict: str
    verdict_reason: str
    consent: str
    status: str
    detail: str
    task_id: UUID | None
    gate_id: UUID | None
    artifact_ids: list[str]
    proposal: ProposalOut | None
    meta: dict[str, Any]
    at: dt.datetime


def message_out(m: Message) -> MessageOut:
    proposal = None
    if m.proposal:
        p = m.proposal
        proposal = ProposalOut(
            goal=str(p.get("goal", "")),
            deliverable=p.get("deliverable"),
            why=str(p.get("why", "")),
            direct=bool(p.get("direct", False)),
            status=str(p.get("status", "pending")),
            task_id=UUID(str(p["task_id"])) if p.get("task_id") else None,
            format=p.get("format"),
        )
    return MessageOut(
        seq=m.seq,
        role=m.role,
        kind=m.kind,
        text=m.text,
        sender=m.sender,
        verdict=m.verdict,
        verdict_reason=m.verdict_reason,
        consent=m.consent,
        status=m.status,
        detail=m.detail,
        task_id=m.task_id,
        gate_id=m.gate_id,
        artifact_ids=[str(a) for a in (m.artifact_ids or [])],
        proposal=proposal,
        meta={
            k: v
            for k, v in (m.meta or {}).items()
            if k in ("answered", "remembered", "bot_version", "subject")
        },
        at=m.created_at,
    )


class ChatStreamOut(BaseModel):
    """The conversation stream's frames, declared so the frozen contract carries
    their shapes: an SSE `message` event is a `MessageOut` row (`id: seq`); `chat.start`,
    `chat.delta`, `chat.activity`, `chat.end` are the reply's ephemeral frames (no id). This
    model exists for the OpenAPI export - no route ever returns it as a body."""

    model_config = ConfigDict(frozen=True)

    message: MessageOut | None = None
    start: ChatStart | None = None
    delta: ChatDelta | None = None
    activity: ChatActivity | None = None
    end: ChatEnd | None = None


class SendOut(BaseModel):
    """What the door answered: the line's seq, the verdict it wore, and the task if one
    started. 202 - a reply, when the verdict is `answer`, arrives on the thread."""

    model_config = ConfigDict(frozen=True)

    seq: int
    verdict: str
    reason: str
    task_id: UUID | None = None
