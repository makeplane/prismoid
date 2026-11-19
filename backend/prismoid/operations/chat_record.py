# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A conversation's record and the bot it talks to: the pieces every chat lane shares.

The door (`chat.py`), the reply lane (`chat_reply.py`) and the result coming home
(`chat_home.py`) all write lines through `append` and point at edits with `notify`, resolve the
bot with `bot_row`, and ask `hosts` and `may_work` the same questions. Nothing here commits:
the caller owns the transaction.
"""

from __future__ import annotations

import uuid
from typing import Any

import orjson
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents.base import roster as agent_roster
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.bot import BotRecord, BotStatus
from prismoid.db.models.conversation import Conversation
from prismoid.db.models.event import NOTIFY_CHANNEL
from prismoid.db.models.message import Message
from prismoid.db.models.project import Project, Role
from prismoid.db.models.task import Task
from prismoid.errors import Forbidden, NotFound
from prismoid.operations.context import Fence
from prismoid.schemas.conversation import MAX_MESSAGE_CHARS, Consent, MessageRole, Verdict
from prismoid.schemas.outputs import ARTIFACT_TYPES, ArtifactType

TITLE_MAX = 60


def effective_lease_s() -> float:
    """The reply lane's lease: never shorter than the reply's wall clock plus a
    margin, so a live reply is never re-claimed under itself. ONE formula, read by the claim
    and by the waiting job alike."""
    return float(max(settings.worker.chat_lease_s, settings.worker.chat_timeout_s + 30))


# ── the bot a conversation talks to ───────────────────────────────────────────────────────────


async def bot_row(
    session: AsyncSession, owner_actor_id: uuid.UUID, name: str, project_id: uuid.UUID | None
) -> BotRecord:
    """The published head when one exists; otherwise the author's own draft - chatting with an
    unpublished bot is the workbench (drafts already resolve only for their author)."""
    head = await BotRecord.head(session, name, project_id=project_id, owner_actor_id=owner_actor_id)
    if head is not None:
        return head
    draft = await BotRecord.latest_draft(session, name, owner_actor_id, project_id=project_id)
    if draft is None:
        raise NotFound(f"no bot named {name!r} in this scope")
    return draft


def bot_ref(bot: BotRecord) -> str:
    return bot.name if bot.status == BotStatus.PUBLISHED.value else f"{bot.name}@draft"


def deliverable_kind(value: str | None) -> str | None:
    """A deliverable word the model or the detector produced → a kind the task door ACCEPTS, or
    None. Never trusted raw: `create_task` 422s a kind no plannable agent publishes (dataset,
    webapp, image...), and a chat must not - found in review, the grant-start died on it."""
    if not value:
        return None
    try:
        kind = ArtifactType(value.strip().lower())
    except ValueError:
        return None
    if kind not in ARTIFACT_TYPES:
        return None
    producible = {kind.value for a in agent_roster(plannable=True) if a.publishes for kind in a.kinds}
    return kind.value if kind.value in producible else None


async def may_work(session: AsyncSession, actor: Actor, conv: Conversation) -> bool:
    """May this person's messages START work here? A personal conversation: always. A project
    conversation: only an EDITOR (the task door's own role) - a VIEWER may talk, and every
    work verdict for them would 403 mid-mint (found in review)."""
    if conv.project_id is None:
        return True
    try:
        await Project.get_for(session, conv.project_id, actor, min_role=Role.EDITOR)
    except (Forbidden, NotFound):
        return False
    return True


def hosts(conv: Conversation, task: Task) -> bool:
    """`conv` still carries `task`'s card: the task started here, and its work may write here
    (`Fence.writes_to`). A task moved into a Project - or out of this conversation's
    Project - keeps its turns on its own thread: the conversation neither hears from it (no
    report, no gate row, nothing over a channel) nor continues it (a reply there would be a turn
    whose answer could never come home)."""
    return task.origin_conversation_id == conv.id and Fence.of(task).writes_to(conv.project_id)


# ── the record ────────────────────────────────────────────────────────────────────────────────


async def append(
    session: AsyncSession,
    conv: Conversation,
    *,
    role: MessageRole,
    text: str,
    kind: str = "text",
    sender: str = "",
    verdict: Verdict = Verdict.NONE,
    verdict_reason: str = "",
    consent: Consent = Consent.NONE,
    facts: dict[str, Any] | None = None,
    proposal: dict[str, Any] | None = None,
    task_id: uuid.UUID | None = None,
    gate_id: uuid.UUID | None = None,
    artifact_ids: list[str] | None = None,
    provider_message_id: str | None = None,
    status: str = "sent",
    detail: str = "",
    meta: dict[str, Any] | None = None,
) -> Message:
    """One line, appended under the conversation's own counter (dense, gap-free). Bot and system
    lines bump `unread`; a title is minted from the first user line into silence. Flushes, never
    commits."""
    seq = await conv.next_seq(session)
    row = Message(
        conversation_id=conv.id,
        seq=seq,
        role=role.value,
        kind=kind,
        # The person's line is capped at the door's number; the bot's reply is kept WHOLE - the
        # stream already delivered it in full and the durable row must agree.
        text=text if role is MessageRole.BOT else text[:MAX_MESSAGE_CHARS],
        sender=sender[:255],
        verdict=verdict.value,
        verdict_reason=verdict_reason[:400],
        consent=consent.value,
        facts=facts or {},
        proposal=proposal,
        task_id=task_id,
        gate_id=gate_id,
        artifact_ids=artifact_ids or [],
        provider_message_id=provider_message_id,
        status=status,
        detail=detail[:400],
        meta=meta or {},
    )
    session.add(row)
    conv.last_message_at = utcnow()
    if role is not MessageRole.USER:
        await conv.bump_unread(session)
    elif conv.title is None and text.strip():
        conv.title = " ".join(text.split())[:TITLE_MAX].rstrip(" .,:;-") or None
    await session.flush()
    await notify(session, conv.id, seq)
    return row


async def notify(session: AsyncSession, conversation_id: uuid.UUID, seq: int) -> None:
    """Lane 1 of the conversation stream: a POINTER rides NOTIFY in this very
    transaction, delivered on its commit - the hub reads the row back, so a subscriber can
    never see a line a rollback then erased (the TaskEvent.append law, one table over). Called
    again after a row is EDITED in place (a card started, a line answered or failed, a memory
    filed) so an open thread sees the edit too - the stream carries known seqs as updates."""
    await session.execute(
        sa_text("select pg_notify(:channel, :payload)"),
        {
            "channel": NOTIFY_CHANNEL,
            "payload": orjson.dumps(
                {"conversation_id": str(conversation_id), "lane": "record", "kind": "message", "seq": seq}
            ).decode(),
        },
    )
