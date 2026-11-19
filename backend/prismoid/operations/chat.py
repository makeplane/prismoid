# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Conversations - talk, as the composer (ADR-057). **A Conversation executes nothing.**

The constitution's third twin (Capture runs nothing; a Routine executes nothing): every piece
of work a conversation needs mints an ordinary Task, and everything else is ONE frameless
model call per reply. The whole of the talk-or-work decision sits in `send`:

* the DOOR is pure (`chat_verdict.verdict_for`): facts in, verdict out, journaled on
  the user's own row with its reason - "why did it start a task" is never a guess. It reads no
  words for meaning: a gesture (`/task`, the button) and the card's Reply are its only signals;
* the MODEL judges what a line means, in the reply, through typed tools (`propose_task`,
  `continue_card`, `accept_pending`, `decline_pending`), bracketed above by the door's hard rules
  and below by consent (`settle_verdict`) and the card's state. Money never moves on a verdict
  alone;
* the RESULT comes home: a chat-born task's start, delivery, failure, and gates land here as
  SYSTEM rows (`chat_home.on_task_settled`, `chat_home.on_gate_opened`), the inbox's
  finish-things law.

Replies run on the worker under a per-conversation claim (`chat_reply`), so two replies never
race out of order; a reply that cannot be written is a SYSTEM row wearing its reason, never
silence. Channels are transports for one of these: `receive` is their inbound seam and
`channels.transport_reply` their outbound one. This module is the door and the work it starts;
the record it writes is `chat_record`'s.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.bot import BotRecord
from prismoid.db.models.conversation import Conversation
from prismoid.db.models.message import Message
from prismoid.db.models.project import Project, Role
from prismoid.db.models.task import Task
from prismoid.errors import Conflict, NotFound, ValidationFailed
from prismoid.operations import allowance, chat_reply
from prismoid.operations import tasks as task_ops
from prismoid.operations.chat_home import on_gate_opened as on_gate_opened
from prismoid.operations.chat_home import on_task_settled as on_task_settled
from prismoid.operations.chat_record import (
    TITLE_MAX,
    bot_ref,
    bot_row,
    deliverable_kind,
    hosts,
    may_work,
    notify,
)
from prismoid.operations.chat_record import append as append
from prismoid.operations.chat_record import effective_lease_s as effective_lease_s
from prismoid.operations.chat_reply import answered_after as answered_after
from prismoid.operations.chat_reply import note_stall as note_stall
from prismoid.operations.chat_verdict import facts_of, verdict_for
from prismoid.operations.dispatch import get_dispatcher
from prismoid.schemas.conversation import (
    Consent,
    ConversationSource,
    DocumentFormat,
    MessageRole,
    TaskProposal,
    ThreadsMode,
    Verdict,
    VerdictDecision,
    WorkMode,
)
from prismoid.schemas.inputs import MAX_INPUTS, RawInput, TaskInput

TASK_GESTURE_PREFIX = "/task "  # the CLI/channel spelling of the Start-as-task gesture


def reply_job_key(conversation_id: uuid.UUID, seq: int) -> str:
    return f"chat-{conversation_id}-{seq}"


# ── the owner's verbs ─────────────────────────────────────────────────────────────────────────


async def open_conversation(
    session: AsyncSession,
    actor: Actor,
    *,
    bot_name: str,
    project_id: uuid.UUID | None = None,
    title: str | None = None,
) -> Conversation:
    """A new thread with one bot. Cheap and many: "New chat" per topic is the design - unless
    the bot's threads dial says ONE: then the bot IS its one conversation, and a
    second open returns the row that exists (idempotent, never a 409 - a client that asks for
    "the chat" gets the chat)."""
    if project_id is not None:
        await Project.get_for(session, project_id, actor, min_role=Role.VIEWER)
    bot = await bot_row(session, actor.id, bot_name, project_id)
    if bot.threads == ThreadsMode.ONE.value:
        # The one chat, archived or not: an archived ONE thread comes back (a second row for a
        # one-thread bot is the one thing this dial forbids - found in review).
        existing = await Conversation.for_bot(
            session, actor.id, bot_name, project_id=project_id, limit=1, include_archived=True
        )
        if existing:
            if existing[0].archived_at is not None:
                existing[0].archived_at = None
                await session.commit()
            return existing[0]
    row = Conversation(
        id=uuid.uuid4(),
        owner_actor_id=actor.id,
        project_id=project_id,
        bot_name=bot_name,
        title=(title or "").strip()[:200] or None,
        source=ConversationSource.WEB.value,
    )
    session.add(row)
    await session.commit()
    return row


async def list_conversations(
    session: AsyncSession, actor: Actor, *, bot_name: str, project_id: uuid.UUID | None = None
) -> list[Conversation]:
    if project_id is not None:
        await Project.get_for(session, project_id, actor, min_role=Role.VIEWER)
    return await Conversation.for_bot(session, actor.id, bot_name, project_id=project_id)


async def get_conversation(session: AsyncSession, actor: Actor, conversation_id: uuid.UUID) -> Conversation:
    return await Conversation.get_for(session, conversation_id, actor)


async def messages(
    session: AsyncSession, actor: Actor, conversation_id: uuid.UUID, *, since: int = 0, limit: int = 200
) -> list[Message]:
    """The thread after `since` (the poll cursor). An owner's read clears the unread count."""
    conv = await Conversation.get_for(session, conversation_id, actor)
    rows = await Message.since(session, conv.id, since=since, limit=limit)
    if conv.unread:
        conv.unread = 0
        await session.commit()
    return rows


async def update_conversation(
    session: AsyncSession,
    actor: Actor,
    conversation_id: uuid.UUID,
    *,
    title: str | None = None,
    archived: bool | None = None,
) -> Conversation:
    conv = await Conversation.get_for(session, conversation_id, actor)
    if title is not None:
        conv.title = title.strip()[:200] or None
    if archived is not None:
        conv.archived_at = utcnow() if archived else None
    await session.commit()
    return conv


# ── the door ──────────────────────────────────────────────────────────────────────────────────


class SendOutcome:
    """What `send` answered: the row, the verdict, and the task if one started."""

    __slots__ = ("message", "task", "verdict")

    def __init__(self, message: Message, verdict: VerdictDecision, task: Task | None) -> None:
        self.message = message
        self.verdict = verdict
        self.task = task


async def _anchor(
    session: AsyncSession, actor: Actor, conv: Conversation, reply_to_task_id: uuid.UUID | None
) -> tuple[Task | None, str | None]:
    """The EXPLICIT anchor: the card's Reply affordance named the task."""
    if reply_to_task_id is None:
        return None, None
    task = await Task.get_for(session, reply_to_task_id, actor)
    if task.origin_conversation_id != conv.id:
        raise ValidationFailed(
            "that task was not started from this conversation", fields={"reply_to": "foreign"}
        )
    if not hosts(conv, task):
        raise ValidationFailed(
            "that task has moved - continue it on its own page", fields={"reply_to": "moved"}
        )
    return task, ("settled" if task.is_terminal() else "running")


def _gesture(text: str) -> tuple[bool, str]:
    """The typed gesture: a line opening with `/task ` is the Start-as-task button, spelled out."""
    text = text.strip()
    if text.startswith(TASK_GESTURE_PREFIX):
        return True, text[len(TASK_GESTURE_PREFIX) :].strip()
    return False, text


async def send(
    session: AsyncSession,
    actor: Actor,
    conversation_id: uuid.UUID,
    text: str,
    *,
    gesture: bool = False,
    reply_to_task_id: uuid.UUID | None = None,
    inputs: Sequence[RawInput] = (),
) -> SendOutcome:
    """The owner's message through the door. Returns after the verdict is durable:
    an ANSWER is enqueued for the worker, a START/STEER/FOLLOW_UP has already acted."""
    conv = await Conversation.get_for(session, conversation_id, actor)
    if conv.archived_at is not None:
        raise Conflict("unarchive the conversation before writing in it")
    typed, text = _gesture(text)
    gesture = gesture or typed
    if not text and not inputs:
        raise ValidationFailed("an empty message says nothing", fields={"text": "empty"})
    bot = await bot_row(session, actor.id, conv.bot_name, conv.project_id)
    task, anchor = await _anchor(session, actor, conv, reply_to_task_id)
    pending = await Message.pending_proposal(session, conv.id)
    return await _receive(
        session,
        conv,
        bot,
        actor,
        text=text,
        sender="owner",
        sender_is_owner=True,
        work_grant=await may_work(session, actor, conv),
        gesture=gesture,
        anchor_task=task,
        anchor=anchor,
        anchored_by="reply" if task is not None else "",
        pending=pending,
        inputs=inputs,
    )


async def receive(
    session: AsyncSession,
    conv: Conversation,
    *,
    text: str,
    sender: str,
    sender_is_owner: bool,
    work_grant: bool,
    budget_usd: float | None = None,
    provider_message_id: str | None = None,
    meta: dict[str, Any] | None = None,
    inputs: Sequence[RawInput] = (),
) -> SendOutcome:
    """A channel's inbound message through the SAME door (the transport already authenticated
    and vetted it). An outsider never starts spend: rung 2 of the ladder, structurally."""
    owner = await conv.seated_owner(session)
    if conv.archived_at is not None:
        conv.archived_at = None  # a thread the world writes into is alive again (found in review)
    gesture, text = _gesture(text)
    bot = await bot_row(session, owner.id, conv.bot_name, conv.project_id)
    pending = await Message.pending_proposal(session, conv.id)
    # The owner's standing in a project bounds every grant here: a VIEWER's channel starts no work.
    work_grant = work_grant and await may_work(session, owner, conv)
    if sender_is_owner:
        work_grant = await may_work(session, owner, conv)
    return await _receive(
        session,
        conv,
        bot,
        owner,
        text=text,
        sender=sender,
        sender_is_owner=sender_is_owner,
        work_grant=work_grant,
        gesture=gesture and (sender_is_owner or work_grant),
        anchor_task=None,
        anchor=None,
        pending=pending,
        budget_usd=budget_usd,
        provider_message_id=provider_message_id,
        meta=meta,
        inputs=inputs,
    )


async def _receive(
    session: AsyncSession,
    conv: Conversation,
    bot: BotRecord,
    owner: Actor,
    *,
    text: str,
    sender: str,
    sender_is_owner: bool,
    work_grant: bool,
    gesture: bool,
    anchor_task: Task | None,
    anchor: str | None,
    pending: Message | None,
    anchored_by: str = "",
    inputs: Sequence[RawInput] = (),
    budget_usd: float | None = None,
    provider_message_id: str | None = None,
    meta: dict[str, Any] | None = None,
) -> SendOutcome:
    # Every line can cost a reply, so a workspace that spent its allowance takes none: refused before the
    # line is stored (ADR-103).
    await allowance.require(session, conv.workspace_id, actor=owner if sender_is_owner else None)
    # The line's files are stored with it on every verdict, before any row: work started later reads them back.
    stored = await task_ops.store_inputs(inputs)
    if stored:
        meta = {**(meta or {}), "inputs": [i.model_dump(mode="json") for i in stored.items]}
    # An owner who may not start work here (a project VIEWER) talks as a pure talker: the ladder's
    # first rung, so no verdict can reach a mint that would 403 (found in review).
    work_mode = bot.work_mode if (work_grant or not sender_is_owner) else "never"
    facts = facts_of(
        text,
        attachments=len(inputs),
        gesture=gesture,
        sender_is_owner=sender_is_owner,
        work_grant=work_grant,
        work_mode=work_mode,
        pending_proposal=pending is not None,
        task_anchor=anchor,
        anchored_by=anchored_by,
    )
    decision = verdict_for(facts)

    if anchor_task is not None and decision.verdict in (Verdict.STEER, Verdict.FOLLOW_UP):
        # Lock order (found in review): the gate openers hold the TASK row and then write the
        # conversation; this side writes the conversation and then steers the task. Take the
        # task row first here too, so both walks go tasks -> conversations and never deadlock.
        await Task.lock(session, anchor_task.id)
    refused = decision.verdict is Verdict.REFUSED  # a row wearing its reason, like a door's refusal
    user = await append(
        session,
        conv,
        role=MessageRole.USER,
        text=text,
        sender=sender,
        verdict=decision.verdict,
        verdict_reason=decision.reason,
        consent=decision.consent,
        facts=facts.model_dump(),
        provider_message_id=provider_message_id,
        status="refused" if refused else "sent",
        detail=decision.reason if refused else "",
        meta=meta,
    )
    # A channel thread's reply-to is whoever wrote last - the owner included (the
    # owner's own email got a row and no reply when only outsiders were remembered).
    if sender and sender != "owner":
        conv.last_sender = sender[:255]

    started = await _act(
        session,
        owner,
        conv,
        bot,
        user,
        text=text,
        decision=decision,
        anchor_task=anchor_task,
        budget_usd=budget_usd,
    )
    if user.task_id is not None:
        # The verbs above commit inside themselves (the task door's own transactions), so the
        # line's first pointer went out before `task_id` was stamped: point at it again.
        await notify(session, conv.id, user.seq)
    await session.commit()

    if decision.verdict is Verdict.ANSWER:
        await get_dispatcher().enqueue(
            "reply_chat", reply_job_key(conv.id, user.seq), conversation_id=str(conv.id), seq=user.seq
        )
    logfire.info(
        "conversation {id} message {seq} → {verdict} ({reason})",
        id=str(conv.id),
        seq=user.seq,
        verdict=decision.verdict.value,
        reason=decision.reason,
    )
    return SendOutcome(user, decision, started)


async def _act(
    session: AsyncSession,
    owner: Actor,
    conv: Conversation,
    bot: BotRecord,
    user: Message,
    *,
    text: str,
    decision: VerdictDecision,
    anchor_task: Task | None,
    budget_usd: float | None,
) -> Task | None:
    """Carry out a START, STEER or FOLLOW_UP verdict on the line `user` and stamp it with its task; returns
    the task started or continued. The caller commits the rest."""
    if decision.verdict is Verdict.START:
        started = await _start_line(
            session,
            owner,
            conv,
            bot,
            user,
            text=text,
            decision=decision,
            budget_usd=budget_usd,
        )
        user.task_id = started.id
        return started
    if anchor_task is not None and decision.verdict in (Verdict.STEER, Verdict.FOLLOW_UP):
        return await _continue_line(
            session,
            owner,
            conv,
            user,
            anchor_task,
            verdict=decision.verdict,
            consent=decision.consent,
        )
    return None


async def _continue_line(
    session: AsyncSession,
    owner: Actor,
    conv: Conversation,
    user: Message,
    task: Task,
    *,
    verdict: Verdict,
    consent: Consent,
) -> Task | None:
    """STEER `task` with the person's line or FOLLOW_UP it, stamping the line; the caller holds the task
    row lock. Returns the task continued (None for a steer)."""
    from prismoid.operations import steering

    if verdict is Verdict.STEER:
        # No `issued_epoch`, so never a 409; the line's own id makes a replayed message steer once.
        await steering.steer(
            session,
            owner,
            task.id,
            user.text,
            command_id=uuid.uuid5(uuid.NAMESPACE_URL, f"conv:{conv.id}:{user.seq}"),
        )
        user.task_id = task.id
        return None
    continued = await task_ops.continue_task(
        session,
        owner,
        task.id,
        goal=user.text,
        clarify="never",
        inputs=await _thread_inputs(session, conv, user.seq, user.seq, held=task.inputs),
        client_token=f"conv:{conv.id.hex}:{user.seq}",
    )
    user.task_id = continued.id
    await append(
        session,
        conv,
        role=MessageRole.SYSTEM,
        kind="continued",
        text=f"Continued the task (turn {continued.turn}).",
        task_id=continued.id,
        consent=consent,
    )
    return continued


async def _start_line(
    session: AsyncSession,
    owner: Actor,
    conv: Conversation,
    bot: BotRecord,
    user: Message,
    *,
    text: str,
    decision: VerdictDecision,
    budget_usd: float | None,
) -> Task:
    """A gesture's START: the line's own task, minted with a `started` row. The task door commits
    inside itself; the caller commits the rows."""
    # The gesture's task is THIS bot's (with the bot merely
    # pinned, One seated `operator`, which delivered a claimed Gmail draft id). The
    # person is talking to a bot; the work they gesture at is that bot's to do. What it makes is
    # the task's own planning to read from the goal, never this door's.
    started = await _mint_task(
        session,
        owner,
        conv,
        user.seq,
        bot,
        goal=text,
        direct=True,
        deliverable=None,
        inputs=await _thread_inputs(session, conv, user.seq, user.seq),
        budget_usd=budget_usd,
    )
    await append(
        session,
        conv,
        role=MessageRole.SYSTEM,
        kind="started",
        text=f"Started a task: {text[:200]}",
        task_id=started.id,
        consent=decision.consent,
    )
    return started


async def _mint_task(
    session: AsyncSession,
    owner: Actor,
    conv: Conversation,
    seq: int,
    bot: BotRecord,
    *,
    goal: str,
    direct: bool,
    deliverable: str | None,
    deliverable_format: DocumentFormat | None = None,
    inputs: Sequence[TaskInput] = (),
    budget_usd: float | None = None,
) -> Task:
    """The one START: an ordinary task, the bot on the roster with its cap, the conversation as turn 0
    (never an input), named from its goal. Idempotent per message through the create_task client token."""

    # The bot's per-task cap - and a channel's per-turn cap on top when the line came in
    # through one (the smaller wins; the dial was minted, PATCHable and read by nothing).
    cap = float(bot.task_budget_usd)
    if budget_usd is not None and budget_usd > 0:
        cap = min(cap, float(budget_usd))
    budget = f"{cap:.4f}"
    return await task_ops.create_task(
        session,
        owner,
        goal=goal,
        project_id=conv.project_id,
        clarify="never",  # the conversation IS the intake - the bot already asked what it needed
        budget={"usd": budget},
        bots=[bot_ref(bot)],
        direct=direct,
        deliverable=deliverable_kind(deliverable),
        deliverable_format=deliverable_format,
        client_token=f"conv:{conv.id.hex}:{seq}",
        inputs=list(inputs),
        title=" ".join(goal.split())[:TITLE_MAX].rstrip(" .,:;-") or None,
        origin_conversation_id=conv.id,
        origin_message_seq=seq,
    )


def _anchor_of(card: Message) -> int:
    """The first line the proposing reply answered: a card carries files from there onward, never older ones."""
    answered = [int(s) for s in card.meta.get("answered", [])]
    return min(answered, default=card.seq)


async def _thread_inputs(
    session: AsyncSession,
    conv: Conversation,
    since: int,
    until: int | None = None,
    *,
    held: Sequence[dict[str, Any]] = (),
) -> list[TaskInput]:
    """The owner's files on the asking lines (`since`..`until`) that the task lacks (newest per name, at most
    MAX_INPUTS); an older line's file never rides (a Project's members read the task), nor a stranger's."""
    names = {str(i["filename"]) for i in held}
    picked: dict[str, TaskInput] = {}
    for line in await Message.attached(session, conv.id, since, until):
        if not conv.owners_own([line]):
            continue
        for raw in line.meta.get("inputs", []):
            item = TaskInput.model_validate(raw)
            if item.filename not in names and item.filename not in picked and len(picked) < MAX_INPUTS:
                picked[item.filename] = item
    return list(reversed(picked.values()))


async def _start_card(
    session: AsyncSession,
    owner: Actor,
    conv: Conversation,
    bot: BotRecord,
    card: Message,
    *,
    consent: Consent,
) -> Task:
    """Mint the waiting `card`'s task and write its `started` row; the task door commits inside."""
    proposal = TaskProposal.model_validate(card.proposal or {})
    if proposal.status != "pending":
        raise Conflict(f"that proposal is already {proposal.status}")
    if bot.work_mode == WorkMode.NEVER.value:
        raise Conflict(f"{bot.name} is a pure talker (work mode never) - it starts no work")
    task = await _mint_task(
        session,
        owner,
        conv,
        card.seq,
        bot,
        goal=proposal.goal,
        direct=proposal.direct,
        deliverable=proposal.deliverable,
        deliverable_format=proposal.format,
        inputs=await _thread_inputs(session, conv, _anchor_of(card)),
    )
    card.proposal = {**(card.proposal or {}), "status": "started", "task_id": str(task.id)}
    await notify(session, conv.id, card.seq)  # the card changed under a pointer already sent
    await append(
        session,
        conv,
        role=MessageRole.SYSTEM,
        kind="started",
        text=f"Started a task: {proposal.goal[:200]}",
        task_id=task.id,
        consent=consent,
    )
    return task


async def start_proposal(session: AsyncSession, actor: Actor, conversation_id: uuid.UUID, seq: int) -> Task:
    """The tap on a card (consent = proposal)."""
    conv = await Conversation.get_for(session, conversation_id, actor)
    card = await Message.get(session, conv.id, seq)
    if card is None or card.role != MessageRole.BOT.value or not card.proposal:
        raise NotFound("no proposal at that message")
    bot = await bot_row(session, actor.id, conv.bot_name, conv.project_id)
    task = await _start_card(session, actor, conv, bot, card, consent=Consent.PROPOSAL)
    await session.commit()
    return task


SAVE_FORMATS = frozenset({"md", "docx", "pdf", "html"})


async def save_reply_as_file(
    session: AsyncSession, actor: Actor, conversation_id: uuid.UUID, seq: int, *, format: str = "pdf"
) -> Task:
    """A reply that should have been a file becomes one in one tap: an ordinary task whose one source is the
    reply, the document dial set with the format. Idempotent per reply and format."""
    conv = await Conversation.get_for(session, conversation_id, actor)
    row = await Message.get(session, conv.id, seq)
    if row is None or row.role != MessageRole.BOT.value or not row.text.strip():
        raise NotFound("no reply at that message")
    if format not in SAVE_FORMATS:
        raise ValidationFailed("format is md, docx, pdf or html", fields={"format": format})
    bot = await bot_row(session, actor.id, conv.bot_name, conv.project_id)

    task = await task_ops.create_task(
        session,
        actor,
        goal=(
            f"Publish the reply in reply.md as a {format} document, verbatim: keep every word, "
            "heading and list as written; add nothing, cut nothing, and do not rewrite it."
        ),
        project_id=conv.project_id,
        clarify="never",
        budget={"usd": f"{float(bot.task_budget_usd):.4f}"},
        deliverable="document",
        deliverable_format=format,
        inputs=[RawInput(filename="reply.md", data=row.text.encode(), media_type="text/markdown")],
        client_token=f"conv:{conv.id.hex}:save:{seq}:{format}",
        title=" ".join(row.text.split())[:TITLE_MAX].rstrip(" .,:;-") or None,
        origin_conversation_id=conv.id,
        origin_message_seq=seq,
    )
    # Idempotent on the THREAD too: a replayed tap returns the same task (the client token)
    # and must not add a second card (found in review).
    if not await Message.for_task(session, task.id):
        await append(
            session,
            conv,
            role=MessageRole.SYSTEM,
            kind="started",
            text=f"Saving that reply as a {format} file",
            task_id=task.id,
            consent=Consent.GESTURE,
        )
    await session.commit()
    return task


async def decline_proposal(
    session: AsyncSession, actor: Actor, conversation_id: uuid.UUID, seq: int
) -> Message:
    conv = await Conversation.get_for(session, conversation_id, actor)
    card = await Message.get(session, conv.id, seq)
    if card is None or card.role != MessageRole.BOT.value or not card.proposal:
        raise NotFound("no proposal at that message")
    if chat_reply.set_aside(card):
        await notify(session, conv.id, card.seq)
        await session.commit()
    return card


# ── the reply lane (worker) ───────────────────────────────────────────────────────────────────


async def reply(session: AsyncSession, conversation_id: uuid.UUID, seq: int) -> str:
    """Answer everything unanswered in one conversation, under its claim (`chat_reply.reply`),
    handing the lane this door's verbs: starting and continuing work is the door's."""
    return await chat_reply.reply(
        session,
        conversation_id,
        seq,
        door=chat_reply.Door(start_card=_start_card, continue_line=_continue_line),
    )
