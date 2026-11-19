# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The reply lane (worker): one frameless model call answers everything unanswered in a
conversation, under its claim.

The claim is a LEASE (`replying_since`): taken once, renewed after every batch, released only
while it is still ours. A batch that cannot be written is a SYSTEM row wearing its reason, never
silence. Memory extraction runs after the lane is released, so it never delays the next answer.
Starting and continuing work is the door's (`chat.py` alone may reach the task door), so the lane
is handed the door's verbs as a `Door`.

What a line means for a card is the model's to judge, through the card verbs (`ai/tools/chat.py`):
a line under a task's card goes to that task (`continue_card`), a yes or no on the bot's waiting
proposal starts it or sets it aside (`accept_pending`, `decline_pending`). Each verb only records
the choice during the run; the lane carries it out after, under consent and the card's state.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import logfire
from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.agents.base import build
from prismoid.ai.context.blocks import render_context_md, where_facts
from prismoid.ai.deps import TaskDeps
from prismoid.ai.tools.chat import ChatHands
from prismoid.conf import settings
from prismoid.db.base import utcnow
from prismoid.db.models.actor import Actor
from prismoid.db.models.bot import BotRecord
from prismoid.db.models.bot_channel import BotChannel
from prismoid.db.models.conversation import Conversation
from prismoid.db.models.gate import Gate
from prismoid.db.models.memory import Memory, MemoryScope
from prismoid.db.models.message import Message
from prismoid.db.models.task import Task
from prismoid.db.models.user import User
from prismoid.operations import bots as bot_ops
from prismoid.operations import channels as channel_ops
from prismoid.operations import inbox as inbox_ops
from prismoid.operations import live
from prismoid.operations import memory as memory_ops
from prismoid.operations import models as model_ops
from prismoid.operations import routing as routing_ops
from prismoid.operations import usage as usage_ops
from prismoid.operations.chat_record import (
    append,
    bot_row,
    deliverable_kind,
    effective_lease_s,
    hosts,
    may_work,
    notify,
)
from prismoid.operations.chat_verdict import settle_verdict
from prismoid.operations.context import (
    BUDGETS,
    FTS_LIMIT,
    PERSONAL,
    RECENCY_LIMIT,
    Fence,
    memory_lane,
    merge_unique,
)
from prismoid.schemas.channels import CHANNEL_LAW
from prismoid.schemas.context import ContextLane, ContextPack
from prismoid.schemas.conversation import (
    MAX_HISTORY_CHARS,
    MAX_HISTORY_TURNS,
    Consent,
    ConversationFacts,
    MessageRole,
    TaskProposal,
    Verdict,
    VerdictDecision,
    WorkMode,
)
from prismoid.schemas.ids import ConversationId, ProjectId
from prismoid.schemas.steering import DIRECTIVE_MAX, TOO_LONG_TO_STEER

# What memory extraction reads after an exchange: owner, bot, the owner's lines answered, the reply.
_Heard = tuple[Actor, BotRecord, list[Message], Message]


class StartCard(Protocol):
    """The door's card start (`chat._start_card`): mints the proposal's task and writes the
    card's `started` row; the caller commits."""

    async def __call__(
        self,
        session: AsyncSession,
        owner: Actor,
        conv: Conversation,
        bot: BotRecord,
        card: Message,
        *,
        consent: Consent,
    ) -> Task: ...


class ContinueLine(Protocol):
    """The door's card continuation (`chat._continue_line`): steers or follows up `task` with the
    line and stamps it; the caller holds the task row lock."""

    async def __call__(
        self,
        session: AsyncSession,
        owner: Actor,
        conv: Conversation,
        user: Message,
        task: Task,
        *,
        verdict: Verdict,
        consent: Consent,
    ) -> Task | None: ...


@dataclass(frozen=True)
class Door:
    """The door's verbs the lane is handed: only `chat.py` reaches the task door."""

    start_card: StartCard
    continue_line: ContinueLine


# The rows that ARE a task's card in the thread: its start, its continuations, its delivery,
# failure or cancel, a gate it opened - and a bot line that answered one of its turns.
_CARD_KINDS = frozenset({"started", "continued", "delivered", "failed", "cancelled", "gate"})


async def reply(session: AsyncSession, conversation_id: uuid.UUID, seq: int, *, door: Door) -> str:
    """Answer everything unanswered in one conversation, under its claim. Returns `done`,
    `answered` (someone else already did), `busy` (the claim is held - the job waits and
    retries), or `gone`.

    The claim is a LEASE: taken once, RENEWED after every batch (a second batch
    under a stale claim used to be stolen mid-generation), and released only while it is still
    ours. A batch that cannot be written is a SYSTEM row wearing its reason and the lines it
    failed - never silence, never a wedged lane, whatever raised (found in review: only the
    model call was guarded)."""
    conv = await session.get(Conversation, conversation_id)
    if conv is None:
        return "gone"
    if await Message.answered_after(session, conv.id, seq):
        return "answered"
    stamp = await Conversation.claim_reply(session, conv.id, lease_s=effective_lease_s())
    if stamp is None:
        await session.rollback()
        return "busy"
    await session.commit()  # the claim is durable before the first provider call
    heard: list[_Heard] = []
    try:
        while True:
            pending = await Message.unanswered_user(session, conv.id)
            if not pending:
                break
            fresh, said = await _answer_batch(session, conv, pending, door=door)
            if fresh is None:
                return "gone"
            conv = fresh
            if said is not None:
                heard.append(said)
            renewed = await Conversation.touch_reply(session, conv.id, since=stamp)
            await session.commit()
            if renewed is None:
                logfire.warning("conversation {id}: the reply lease was lost mid-batch", id=str(conv.id))
                break  # another holder owns the lane now; it answers whatever is left
            stamp = renewed
    finally:
        await Conversation.release_reply(session, conversation_id, since=stamp)
        await session.commit()
    # AFTER the lane is free: extraction is a second model call, and it must never
    # delay the next answer - a message that arrived meanwhile takes the lane now.
    for said in heard:
        await _remember(session, conv, said)
    return "done"


async def _answer_batch(
    session: AsyncSession, conv: Conversation, pending: list[Message], *, door: Door
) -> tuple[Conversation | None, _Heard | None]:
    """One batch: the reply, or a rollback and the failure written down. Returns the conversation to go on
    with (None when gone) and memory extraction's work, if any. Holds no row lock."""
    conversation_id = conv.id
    seqs = [m.seq for m in pending]
    try:
        return conv, await _reply_once(session, conv, pending, door=door)
    except Exception as exc:
        # The rollback EXPIRES every loaded row: re-read the conversation and the
        # lines before touching them (an expired attribute is a lazy load - forbidden
        # off the greenlet).
        await session.rollback()
        fresh = await session.get(Conversation, conversation_id)
        if fresh is None:
            return None, None
        failed = [row for seq in seqs if (row := await Message.get(session, fresh.id, seq)) is not None]
        await _reply_failed(session, fresh, failed, exc, bot_name=fresh.bot_name)
        return fresh, None


async def _reply_failed(
    session: AsyncSession, conv: Conversation, pending: list[Message], exc: BaseException, *, bot_name: str
) -> None:
    """A reply that cannot be written is a row wearing its reason (never silence): a SYSTEM
    line, and every line it failed marked so the next batch does not retry it forever."""
    logfire.exception("conversation {id}: {bot} could not reply", id=str(conv.id), bot=bot_name)
    if isinstance(exc, TimeoutError):
        wall = int(settings.worker.chat_timeout_s)
        said = f"{bot_name} took longer than {wall}s to reply and was stopped. Send it again."
    else:
        said = f"{bot_name} could not reply ({type(exc).__name__}). Send it again, or open the task page."
    detail = str(exc)[:400] or type(exc).__name__
    await append(session, conv, role=MessageRole.SYSTEM, kind="failed", text=said, detail=detail)
    for m in pending:
        m.status = "failed"
        m.detail = detail
        await notify(session, conv.id, m.seq)
    await session.commit()


def _pre_verdict(last: Message) -> tuple[ConversationFacts, VerdictDecision]:
    """The door's facts and verdict for the newest line, read back off its own row."""
    facts = ConversationFacts.model_validate(last.facts or {})
    pre = VerdictDecision(
        verdict=Verdict(last.verdict), reason=last.verdict_reason, consent=Consent(last.consent)
    )
    return facts, pre


@dataclass
class _Proposer:
    """The reply's `propose_task` tool: at most one card per reply, and a line back to the model
    that says plainly whether the card starts now (the owner's standing grant) or waits."""

    bot: BotRecord
    pre: VerdictDecision
    facts: ConversationFacts
    within_cap: bool
    proposals: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    async def __call__(self, payload: dict[str, Any]) -> str:
        if self.proposals:
            return "One proposal per reply - fold this into the one you already made."
        proposal = TaskProposal(
            goal=str(payload.get("goal", "")).strip(),
            deliverable=deliverable_kind(payload.get("deliverable")),
            format=payload.get("format"),
            why=str(payload.get("why", ""))[:300],
            direct=bool(payload.get("direct", True)),
        )
        self.proposals.append(proposal.model_dump())
        settled = settle_verdict(self.pre, self.facts, proposed=True, within_cap=self.within_cap)
        if settled.verdict is Verdict.START:
            return (
                f"Started: it is a task now, under the owner's standing grant (capped at "
                f"${float(self.bot.task_budget_usd):.2f}); its card sits under your words. Tell them it has "
                "already started and nothing waits on them; do not do its work yourself."
            )
        if not self.facts.sender_is_owner:
            return (
                "Waiting: your owner was asked, and nothing runs until they tap Start. Tell the sender it "
                "waits for your owner's decision, and do not claim any work was done."
            )
        return (
            "Waiting: a card with Start / Not now sits under your words, and nothing runs until the "
            "person taps Start or says yes. Tell them, and do not claim any work was done."
        )


@dataclass
class _Desk:
    """The reply's card verbs: what the newest line asks of the task card right above it, or of the
    bot's waiting proposal. Recorded during the run; the lane carries it out after (`_carry_out`)."""

    line: Message
    may_act: bool = False  # the sender may start work here, so may act on a card
    above: Task | None = None  # the task whose card sits right above the line
    running: bool = False  # that task's state when the reply began
    waiting: Message | None = None  # the bot's proposal card that waits for consent
    handed: Task | None = None
    choice: Literal["accept", "decline"] | None = None

    def hands(self) -> ChatHands | None:
        """The verbs this reply holds; None when there is no card to act on."""
        if not self.may_act or (self.above is None and self.waiting is None):
            return None
        return ChatHands(
            continue_card=self._continue if self.above is not None else None,
            accept_pending=self._accept if self.waiting is not None else None,
            decline_pending=self._decline if self.waiting is not None else None,
        )

    async def _continue(self) -> str:
        if self.handed is not None:
            return "Already handed to the task."
        if self.running and len(self.line.text.strip()) > DIRECTIVE_MAX:
            return f"Not handed: {TOO_LONG_TO_STEER} Tell the person so."
        self.handed = self.above
        if self.running:
            return "Handed: it steers the running task. Say so in one short line; do not do the task's work yourself."
        return (
            "Handed: the delivered task starts its next turn, and its result comes home here. Say so in "
            "one short line; do not do the task's work yourself."
        )

    async def _accept(self) -> str:
        if self.choice is not None:
            return f"Already answered: you chose to {self.choice} it."
        self.choice = "accept"
        return "Started: the waiting card is a task now. Tell them plainly it has started; do not do its work yourself."

    async def _decline(self) -> str:
        if self.choice is not None:
            return f"Already answered: you chose to {self.choice} it."
        self.choice = "decline"
        return "Set aside: the card no longer waits. Acknowledge it in one short line."


async def _card_above(session: AsyncSession, conv: Conversation) -> tuple[Task, bool] | None:
    """The task whose card is the newest thing in the thread that is not the person's own line, and
    whether it runs; None when the bot's last word was a plain reply, the task moved, or it asks."""
    for m in await Message.newest(session, conv.id, limit=8):
        if m.role == MessageRole.USER.value:
            continue  # the person's earlier lines do not move the anchor
        if m.task_id is None:
            return None  # the bot's last word was a plain reply: the thread moved on
        if m.role == MessageRole.SYSTEM.value and m.kind not in _CARD_KINDS:
            return None
        if m.kind == "gate" and m.gate_id is not None:
            gate = await session.get(Gate, m.gate_id)
            if gate is not None and gate.is_open():
                return None  # the task is ASKING: a line here answers the gate, never steers
        task = await session.get(Task, m.task_id)
        if task is None or not hosts(conv, task):
            return None
        return task, not task.is_terminal()
    return None


async def _desk(
    session: AsyncSession, conv: Conversation, last: Message, facts: ConversationFacts, *, may_work_here: bool
) -> _Desk:
    """The card verbs the newest line may use: only a sender who may start work acts on a card."""
    desk = _Desk(line=last, may_act=may_work_here and (facts.sender_is_owner or facts.work_grant))
    desk.waiting = await Message.pending_proposal(session, conv.id)
    if desk.may_act and (found := await _card_above(session, conv)) is not None:
        desk.above, desk.running = found
    return desk


async def _reply_once(
    session: AsyncSession, conv: Conversation, pending: list[Message], *, door: Door
) -> _Heard | None:
    """One reply for every unanswered line; returns memory extraction's work for the caller to run after the
    claim is released. Raises to the caller's guard on anything that cannot be written."""
    owner = await conv.seated_owner(session)
    bot = await bot_row(session, owner.id, conv.bot_name, conv.project_id)
    resolved = bot_ops.to_resolved(bot)
    last = pending[-1]
    last_facts, pre = _pre_verdict(last)
    within_cap = float(bot.task_budget_usd) > 0
    # A pure talker (work mode never) holds no proposer and no card verb at all - absent beats
    # broken - and so does an owner who may not start work here (a project viewer).
    may_propose = bot.work_mode != WorkMode.NEVER.value and await may_work(session, owner, conv)
    desk = await _desk(session, conv, last, last_facts, may_work_here=may_propose)
    channel = await session.get(BotChannel, conv.channel_id) if conv.channel_id else None
    brief = await _brief(session, conv, bot, owner, pending, last_facts, desk, channel)

    proposer = _Proposer(bot=bot, pre=pre, facts=last_facts, within_cap=within_cap)
    tools: list[dict[str, Any]] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        *,
        ref: str | None = None,
    ) -> None:
        tools.append(
            {
                "tool": tool,
                "ok": ok,
                "summary": summary[:120],
                "ms": duration_ms,
                "denied": denied_reason[:120],
            }
        )

    # The reply STREAMS: the tap pours chat.* frames through a conversation-keyed
    # wire anchored on the last durable message, and the durable bot row ends the stream.
    wire = live.ChatWire(ConversationId(conv.id), after_seq=int(conv.message_seq or 0))
    deps = TaskDeps(
        project_id=ProjectId(conv.project_id) if conv.project_id else None,
        conversation_id=ConversationId(conv.id),
        emit_event=live.emitter(wire),
        emit_usage=usage_ops.emitter(session, own_transaction=True),
        journal_tool=journal,
        # One card waits at a time: while it does, the bot answers for it instead of proposing.
        propose_task=proposer if may_propose and desk.waiting is None else None,
        chat=desk.hands(),
        bots=(resolved,),
        fetch_anchor=await _fetch_anchor(session, conv, resolved.instructions, pending),
    )
    catalog = await model_ops.catalog(session, enabled_only=True)
    order = await routing_ops.order_for(session)
    asked = await routing_ops.settings_for(session)
    runner = build(str(resolved.name), deps, catalog, order=order, settings=asked)
    await session.commit()  # release the snapshot + connection before the provider call

    # The reply's wall clock: past it the call is stopped and the thread says so
    # (the caller's guard) - a reply that never lands is the routines lane's named defect.
    wall = float(settings.worker.chat_timeout_s)
    deps.deadline = time.monotonic() + wall  # no web call waits past it (`ai/tools/wall.py`)
    output = (await asyncio.wait_for(runner.run(brief), timeout=wall)).output
    text = output if isinstance(output, str) else str(getattr(output, "summary", output))

    if desk.handed is not None:
        await _hand_to_card(session, owner, conv, last, desk.handed, door=door)
    bot_msg = await append(
        session,
        conv,
        role=MessageRole.BOT,
        text=text.strip() or "(no reply)",
        sender=bot.name,
        proposal=proposer.proposals[0] if proposer.proposals else None,
        meta={"answered": [m.seq for m in pending], "tools": tools[:20], "bot_version": bot.version},
    )
    for m in pending:
        m.status = "answered"
        await notify(session, conv.id, m.seq)

    if proposer.proposals:
        settled = settle_verdict(pre, last_facts, proposed=True, within_cap=within_cap)
        await _start_waiting(session, owner, conv, bot, bot_msg, last=last, decision=settled, door=door)
    elif desk.waiting is not None and desk.choice is not None:
        await _answer_waiting(
            session, owner, conv, bot, desk.waiting, last=last, accept=desk.choice == "accept", door=door
        )
    await session.commit()

    if channel is not None:
        await _carry(session, channel, conv, bot_msg)
    # Memory is private: only a personal bot's owner, speaking for herself, ever teaches it.
    if bot.project_id is None and conv.owners_own(pending):
        return owner, bot, list(pending), bot_msg
    return None


def _stamp(line: Message, decision: VerdictDecision) -> None:
    """The person's line wears the verdict it settled on."""
    line.verdict, line.verdict_reason, line.consent = (
        decision.verdict.value,
        decision.reason[:400],
        decision.consent.value,
    )


def set_aside(card: Message) -> bool:
    """Mark a waiting proposal declined; False when it no longer waits. The caller notifies and commits."""
    if (card.proposal or {}).get("status") != "pending":
        return False
    card.proposal = {**(card.proposal or {}), "status": "declined"}
    return True


async def _hand_to_card(
    session: AsyncSession, owner: Actor, conv: Conversation, last: Message, task: Task, *, door: Door
) -> None:
    """Carry out `continue_card` under the task row lock (tasks, then conversations): STEER if it runs,
    FOLLOW_UP if it settled. Fail-soft (a person is waiting): a refusal is a row wearing why."""
    try:
        locked = await Task.lock(session, task.id)
        if locked is None:
            raise LookupError("the task is gone")
        running = not locked.is_terminal()
        verdict = Verdict.STEER if running else Verdict.FOLLOW_UP
        await door.continue_line(session, owner, conv, last, locked, verdict=verdict, consent=Consent.NONE)
    except Exception as exc:
        logfire.exception("conversation {id}: the card did not take the line", id=str(conv.id))
        await _failed(session, conv, "Could not hand that to the task", exc)
        return
    state = "a running" if running else "a delivered"
    _stamp(last, VerdictDecision(verdict=verdict, reason=f"the bot handed it to {state} task's card above"))
    last.facts = {
        **(last.facts or {}),
        "task_anchor": "running" if running else "settled",
        "anchored_by": "card",
    }


async def _answer_waiting(
    session: AsyncSession,
    owner: Actor,
    conv: Conversation,
    bot: BotRecord,
    card: Message,
    *,
    last: Message,
    accept: bool,
    door: Door,
) -> None:
    """Carry out the bot's `accept_pending` (the person's yes is the card's consent) or `decline_pending`."""
    if accept:
        decision = VerdictDecision(
            verdict=Verdict.START,
            reason="a yes the bot heard on its waiting proposal",
            consent=Consent.PROPOSAL,
        )
        await _start_waiting(session, owner, conv, bot, card, last=last, decision=decision, door=door)
    elif set_aside(card):
        await notify(session, conv.id, card.seq)


async def _start_waiting(
    session: AsyncSession,
    owner: Actor,
    conv: Conversation,
    bot: BotRecord,
    card: Message,
    *,
    last: Message,
    decision: VerdictDecision,
    door: Door,
) -> None:
    """Stamp the newest line with `decision`, and on a START mint the waiting `card`'s task. Never
    commits (the reply's transaction does); the card start commits inside the task door."""
    fallback = VerdictDecision(
        verdict=Verdict(last.verdict), reason=last.verdict_reason, consent=Consent(last.consent)
    )
    _stamp(last, decision)
    if (
        decision.verdict is Verdict.PROPOSE
        and not ConversationFacts.model_validate(last.facts or {}).sender_is_owner
    ):
        # The bot told the sender "your owner was asked" - this is the asking.
        await inbox_ops.card_waiting(
            session,
            actor_id=owner.id,
            bot_name=bot.name,
            conversation_id=conv.id,
            seq=card.seq,
            sender=last.sender,
            goal=str((card.proposal or {}).get("goal", ""))[:1000],
        )
    if decision.verdict is not Verdict.START:
        return
    # Guarded like the model call: a mint that fails (a kind the door refuses, a role, a budget)
    # leaves the card WAITING and says why - never a half-stamped record (found in review).
    try:
        started = await door.start_card(session, owner, conv, bot, card, consent=decision.consent)
        last.task_id = started.id
    except Exception as exc:
        logfire.exception("conversation {id}: the start failed; the card waits", id=str(conv.id))
        if decision.consent is Consent.GRANT:
            fallback = VerdictDecision(verdict=Verdict.PROPOSE, reason="proposed")
        _stamp(
            last,
            fallback.model_copy(update={"reason": f"{fallback.reason}; the start failed - {str(exc)[:200]}"}),
        )
        await _failed(session, conv, "Could not start that task", exc)


async def _failed(session: AsyncSession, conv: Conversation, said: str, exc: Exception) -> None:
    """A SYSTEM row wearing why an act on a card failed."""
    why = str(exc)[:200] or type(exc).__name__
    await append(
        session,
        conv,
        role=MessageRole.SYSTEM,
        kind="failed",
        text=f"{said}: {why}",
        detail=str(exc)[:400] or type(exc).__name__,
    )


async def _fetch_anchor(
    session: AsyncSession, conv: Conversation, instructions: str, pending: list[Message]
) -> str | None:
    """On a reply to anyone but the owner, the only words that anchor `web_fetch`: the bot's
    instructions and the owner's own lines; None when every pending line is hers."""
    if conv.owners_own(pending):
        return None
    rows = reversed(await Message.newest(session, conv.id, limit=MAX_HISTORY_TURNS))
    hers = [m.text for m in rows if m.role == MessageRole.USER.value and conv.owners_own([m])]
    return "\n".join([instructions, *hers])


async def _carry(session: AsyncSession, channel: BotChannel, conv: Conversation, bot_msg: Message) -> None:
    """The reply over the conversation's channel. Commits only when the transport failed, to
    write that failure on the row; takes no row lock."""
    carried = False
    try:
        carried = await channel_ops.transport_reply(session, channel, conv, bot_msg.text)
    except Exception:
        logfire.exception("conversation {id}: transport failed; the row stands", id=str(conv.id))
    if not carried:
        # The row stands; the transport's failure is written on it (the retired ledger's
        # out-row status, one column over) - never a silent drop.
        bot_msg.status = "failed"
        bot_msg.detail = f"not delivered over {channel.kind}"[:400]
        await notify(session, conv.id, bot_msg.seq)
        await session.commit()


async def _brief(
    session: AsyncSession,
    conv: Conversation,
    bot: BotRecord,
    owner: Actor,
    pending: list[Message],
    facts: ConversationFacts,
    desk: _Desk,
    channel: BotChannel | None,
) -> str:
    personal = conv.owners_own(pending)  # a stranger's line reads none of her private memory
    context_md = await memory_context(session, conv, owner, bot, query=pending[-1].text, personal=personal)
    rows = list(reversed(await Message.newest(session, conv.id, limit=MAX_HISTORY_TURNS)))
    earlier = [m for m in rows if m.seq < pending[0].seq]
    history = _render(conv, earlier)
    latest = _render(conv, pending)
    who = pending[-1].sender if pending[-1].sender and pending[-1].sender != "owner" else "your owner"
    parts: list[str] = [context_md] if context_md else []
    if history:
        parts.append(f"# Earlier in this conversation\n\n{history}")
    parts.append(f"# The latest message{'s' if len(pending) > 1 else ''}\n\n{latest}")
    parts.append("# Now\n\n" + "\n\n".join(_now(who, facts, desk, channel)))
    # Where the person is, from the owner's timezone; a stranger's country is unknown.
    user = await session.get(User, owner.id) if facts.sender_is_owner else None
    parts.append("# Ambient facts\n\n" + where_facts(user.timezone if user else None).rstrip())
    return "\n\n".join(parts)


def _now(who: str, facts: ConversationFacts, desk: _Desk, channel: BotChannel | None) -> list[str]:
    """The brief's closing facts and instructions for this one reply. Pure."""
    now = [f"Reply to {who}, in one message."]
    if channel is not None and not facts.sender_is_owner and channel.input_mode != "instruction":
        now.append(CHANNEL_LAW.format(label=channel.label or channel.kind, sender=who).strip())
    if facts.attachments:
        now.append(
            f"[{facts.attachments} attachment(s) came with the latest message; a reply cannot open them.]"
        )
    if desk.above is not None:
        state = "running" if desk.running else "delivered"
        title = desk.above.title or " ".join(desk.above.goal.split())[:80]
        now.append(f"[The task card right above the latest message: {title!r}, {state}.]")
    if desk.waiting is not None:
        goal = str((desk.waiting.proposal or {}).get("goal", ""))[:200]
        now.append(f"[A proposal card of yours waits for consent: {goal!r}. Remind them if it matters.]")
    if not facts.sender_is_owner and not facts.work_grant:
        now.append("[This sender cannot start work; if work is needed, propose it - the owner decides.]")
    return now


def _render(conv: Conversation, rows: list[Message]) -> str:
    lines: list[str] = []
    budget = MAX_HISTORY_CHARS
    for m in reversed(rows):  # newest first, so the clip drops the OLDEST lines
        if m.role == MessageRole.USER.value:
            who = f"{m.sender}" if m.sender and m.sender != "owner" else "Owner"
            line = f"**{who}:** {m.text}"
        elif m.role == MessageRole.BOT.value:
            line = f"**{conv.bot_name} (you):** {m.text}"
            if m.proposal:
                line += f"\n_[you proposed a task, {m.proposal.get('status', 'pending')}: {m.proposal.get('goal', '')}]_"
        else:
            line = f"_[system · {m.kind}] {m.text}_"
        if budget - len(line) < 0:
            break
        budget -= len(line)
        lines.append(line)
    lines.reverse()
    return "\n\n".join(lines)


async def memory_context(
    session: AsyncSession, conv: Conversation, owner: Actor, bot: BotRecord, *, query: str, personal: bool
) -> str:
    """The reply's memory block: none for a reply to a stranger (`personal` False, ADR-059); else the
    bot lane, plus the user lane outside a Project (the task fence's rule). Receipt: `mark_used`."""
    if not personal:
        return ""
    lanes: list[ContextLane] = []
    if Fence(conv.project_id).admits(PERSONAL):
        user_lane = memory_lane(
            "user",
            merge_unique(
                await Memory.search(
                    session, MemoryScope.USER, user_id=owner.id, query_text=query, limit=FTS_LIMIT
                ),
                await Memory.for_scope(session, MemoryScope.USER, user_id=owner.id, limit=RECENCY_LIMIT),
            ),
            BUDGETS["user"],
        )
        if user_lane:
            lanes.append(user_lane)
    carrier: dict[str, Any] = {"project_id": bot.project_id} if bot.project_id else {"user_id": owner.id}
    bot_lane = memory_lane(
        "bot",
        merge_unique(
            await Memory.search(
                session, MemoryScope.BOT, bot_name=bot.name, query_text=query, limit=FTS_LIMIT, **carrier
            ),
            await Memory.for_scope(
                session, MemoryScope.BOT, bot_name=bot.name, limit=RECENCY_LIMIT, **carrier
            ),
        ),
        BUDGETS["bot"],
        label=f"What {bot.name} remembers",
    )
    if bot_lane:
        lanes.append(bot_lane)
    if not lanes:
        return ""
    pack = ContextPack(lanes=lanes, total_tokens=sum(lane.tokens for lane in lanes))
    await Memory.mark_used(session, pack.memory_ids(), at=utcnow())
    return render_context_md(pack)


async def _remember(session: AsyncSession, conv: Conversation, heard: _Heard) -> None:
    """memory_extract over the owner's own lines to her personal bot, never the bot's reply;
    fail-soft (the reply stands). Commits; holds no row lock. Spend rides the conversation."""
    owner, bot, pending, bot_msg = heard
    try:
        ledger = await Memory.for_scope(
            session, MemoryScope.BOT, user_id=owner.id, bot_name=bot.name, limit=memory_ops.LEDGER_SHOWN
        )
        said = [m.text for m in pending]
        brief = memory_ops.extract_brief(
            memory_ops.bot_memory_owner(bot.name), said, [m.text for m in ledger]
        )
        deps = TaskDeps(
            project_id=ProjectId(conv.project_id) if conv.project_id else None,
            conversation_id=ConversationId(conv.id),
            emit_usage=usage_ops.emitter(session, own_transaction=True),
        )
        catalog, order = await model_ops.routed_catalog(session)
        runner = build(
            "memory_extract", deps, catalog, order=order, settings=await routing_ops.settings_for(session)
        )
        await session.commit()
        kept = memory_ops.keepable((await runner.run(brief)).output, said, ledger)
        if kept:
            minted = await memory_ops.consolidate(
                session, kept, user_id=owner.id, source_task_id=None, bot_name=bot.name
            )
            bot_msg.meta = {**(bot_msg.meta or {}), "remembered": [m.text[:120] for m in minted]}
            await notify(session, conv.id, bot_msg.seq)  # the line grew a receipt under a sent pointer
            await session.commit()
    except Exception:
        logfire.exception("memory after conversation {id} failed; the reply stands", id=str(conv.id))
        await session.rollback()


async def answered_after(session: AsyncSession, conv: Conversation, seq: int) -> bool:
    return await Message.answered_after(session, conv.id, seq)


async def note_stall(session: AsyncSession, conv: Conversation, seq: int) -> None:
    """The reply lane stayed claimed past the job's patience: a SYSTEM row says so, so a
    wedged conversation is never silent (the routines lane's named industry defect)."""
    await append(
        session,
        conv,
        role=MessageRole.SYSTEM,
        kind="failed",
        text=f"{conv.bot_name} did not reply in time. Send it again.",
        detail=f"reply lane stalled behind message {seq}",
    )
