# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The cards of a conversation (ADR-060, amended): the door reads no words, and the bot's reply acts on
a card through typed tools that code disposes of.

* a line under a task's card goes to that task only when the bot hands it there (`continue_card`):
  a FOLLOW_UP on a delivered task, a STEER on a running one, by the task's state, never twice;
* a steer past its cap is refused: by the door on the card's explicit Reply, by the tool otherwise;
* a yes the bot heard on its waiting proposal starts it (`accept_pending`), a no sets it aside
  (`decline_pending`), and while a card waits the bot proposes nothing new;
* a pure talker holds no card verb at all.

The reply runs on the real build with a scripted model (`tests/support/chat_model.py`).
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from prismoid.db.models.conversation import Conversation
from prismoid.db.models.message import Message
from prismoid.db.models.steer import Steer
from prismoid.db.models.task import Task
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import bots as bot_ops
from prismoid.operations import chat as chat_ops
from prismoid.operations import execution, live
from prismoid.schemas.plan import TaskStatus
from tests.support.chat_cast import an_owner, published_bot, settle
from tests.support.chat_model import reply_choosing

pytestmark = pytest.mark.integration

CARD_VERBS = frozenset({"continue_card", "accept_pending", "decline_pending"})


@pytest.fixture(autouse=True)
def quiet_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    async def quiet(event: Any) -> None:
        return None

    monkeypatch.setattr(live, "notify", quiet)


async def _started(*, mode: str = "auto") -> tuple[uuid.UUID, uuid.UUID, Any]:
    """An owner, her bot under `mode`, and a task started from the chat by the gesture."""
    async with session_scope() as session:
        owner = await an_owner(session)
        name = await published_bot(session, owner)
        await bot_ops.set_face(session, actor=owner, name=name, work_mode=mode)
        conv = await chat_ops.open_conversation(session, owner, bot_name=name)
        work = await chat_ops.send(session, owner, conv.id, "write the 3.2 release notes", gesture=True)
        assert work.task is not None
        return work.task.id, conv.id, owner


async def _running(task_id: uuid.UUID) -> int:
    async with row_scope(Task, task_id) as session:
        task = await session.get(Task, task_id)
        assert task is not None
        task.transition(TaskStatus.PLANNING)
        await session.flush()
        await execution.advance(session, task_id, TaskStatus.RUNNING)
        await session.commit()
        return task.plan_epoch


async def test_the_door_reads_no_words() -> None:
    async with session_scope() as session:
        owner = await an_owner(session)
        name = await published_bot(session, owner)
        conv = await chat_ops.open_conversation(session, owner, bot_name=name)
        for line in ("write me a memo on the renewals pipeline", "make me a spreadsheet of it", "shorter"):
            out = await chat_ops.send(session, owner, conv.id, line)
            assert (out.verdict.verdict.value, out.verdict.reason, out.task) == ("answer", "talk", None)
            assert "deliverable" not in out.message.facts and out.message.facts["anchored_by"] == ""


async def test_a_line_the_bot_hands_to_a_delivered_card_is_its_next_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id, cid, owner = await _started()
    await settle(task_id, "Release notes published.")

    async with row_scope(Conversation, cid) as session:
        line = await chat_ops.send(session, owner, cid, "shorter, and drop the open questions")
        assert line.verdict.verdict.value == "answer"  # the bot decides, not the door
    tools = await reply_choosing(monkeypatch, cid, line.message.seq, "continue_card")
    assert "continue_card" in tools and "propose_task" in tools

    async with row_scope(Conversation, cid) as session:
        row = await Message.get(session, cid, line.message.seq)
        assert row is not None and (row.verdict, row.task_id) == ("follow_up", task_id)
        assert row.facts["anchored_by"] == "card" and "delivered" in row.verdict_reason
        task = await session.get(Task, task_id)
        assert task is not None and task.turn == 2
    await settle(task_id, "Shorter now, open questions gone.")

    async with row_scope(Conversation, cid) as session:
        rows = await Message.since(session, cid)
        assert rows[-1].role == "bot" and rows[-1].task_id == task_id  # the turn came home as the bot's line
        assert [r.kind for r in rows if r.role == "system"] == ["started", "delivered", "continued"]
        ask = await chat_ops.send(session, owner, cid, "what did the team decide about pricing?")
    await reply_choosing(monkeypatch, cid, ask.message.seq, None)  # a question: the bot answers it
    async with row_scope(Conversation, cid) as session:
        row = await Message.get(session, cid, ask.message.seq)
        task = await session.get(Task, task_id)
        assert row is not None and (row.verdict, row.task_id) == ("answer", None)
        assert task is not None and task.turn == 2


async def test_a_line_the_bot_hands_to_a_running_card_steers_it_once(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, cid, owner = await _started()
    await _running(task_id)
    async with row_scope(Conversation, cid) as session:
        line = await chat_ops.send(session, owner, cid, "use euros throughout")
    await reply_choosing(monkeypatch, cid, line.message.seq, "continue_card")

    async with row_scope(Task, task_id) as session:
        row = await Message.get(session, cid, line.message.seq)
        assert row is not None and (row.verdict, row.task_id) == ("steer", task_id)
        command_id = uuid.uuid5(uuid.NAMESPACE_URL, f"conv:{cid}:{line.message.seq}")
        steer = await Steer.by_command(session, task_id, command_id)
        assert steer is not None and (steer.text, steer.issued_epoch) == ("use euros throughout", None)
        assert len(await Steer.since(session, task_id, 0)) == 1


async def test_a_steer_past_its_cap_is_refused_at_the_door_and_by_the_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A steer is at most 2,000 characters, a chat line up to 20,000: never cut short, never a 422."""
    task_id, cid, owner = await _started()
    epoch = await _running(task_id)
    long = "Use these notes: " + "the margins held in Q3 and churn fell. " * 54
    async with row_scope(Task, task_id) as session:
        explicit = await chat_ops.send(session, owner, cid, long, reply_to_task_id=task_id)
        assert explicit.verdict.verdict.value == "refused" and "2,000 characters" in explicit.verdict.reason
        assert explicit.message.status == "refused"
        implicit = await chat_ops.send(session, owner, cid, long)
        assert implicit.verdict.verdict.value == "answer"
    await reply_choosing(monkeypatch, cid, implicit.message.seq, "continue_card")
    async with row_scope(Task, task_id) as session:
        row = await Message.get(session, cid, implicit.message.seq)
        task = await session.get(Task, task_id)
        assert row is not None and (row.verdict, row.task_id) == ("answer", None)
        assert await Steer.since(session, task_id, 0) == []
        assert task is not None and task.plan_epoch == epoch


async def test_a_yes_the_bot_hears_on_its_waiting_card_starts_it(monkeypatch: pytest.MonkeyPatch) -> None:
    async with session_scope() as session:
        owner = await an_owner(session)
        name = await published_bot(session, owner)
        await bot_ops.set_face(session, actor=owner, name=name, work_mode="ask")
        conv = await chat_ops.open_conversation(session, owner, bot_name=name)
        cid = conv.id
        ask = await chat_ops.send(session, owner, cid, "make me a spreadsheet of the renewals pipeline")
    proposal = {"goal": "A spreadsheet of the renewals pipeline", "deliverable": "spreadsheet"}
    heard: list[str] = []
    first = await reply_choosing(monkeypatch, cid, ask.message.seq, "propose_task", proposal, heard=heard)
    assert not first & CARD_VERBS, "no card waits yet"
    assert heard[0].startswith("Waiting:"), "under ask the bot is told the card waits"

    async with row_scope(Conversation, cid) as session:
        proposed = await Message.get(session, cid, ask.message.seq)
        assert proposed is not None and proposed.verdict == "propose"
        yes = await chat_ops.send(session, owner, cid, "yes, go ahead")
        assert yes.verdict.verdict.value == "answer"  # no word list: the bot hears the yes
    heard: list[str] = []
    tools = await reply_choosing(monkeypatch, cid, yes.message.seq, "accept_pending", heard=heard)
    assert {"accept_pending", "decline_pending"} <= tools and "propose_task" not in tools
    assert heard[0].startswith("Started:") and "reply" not in heard[0].lower()

    async with row_scope(Conversation, cid) as session:
        row = await Message.get(session, cid, yes.message.seq)
        card = await Message.pending_proposal(session, cid)
        assert row is not None and (row.verdict, row.consent) == ("start", "proposal") and row.task_id
        assert card is None
        task = await session.get(Task, row.task_id)
        assert task is not None and task.goal == proposal["goal"] and task.origin_conversation_id == cid


async def test_a_no_the_bot_hears_sets_its_card_aside(monkeypatch: pytest.MonkeyPatch) -> None:
    async with session_scope() as session:
        owner = await an_owner(session)
        name = await published_bot(session, owner)
        await bot_ops.set_face(session, actor=owner, name=name, work_mode="ask")
        conv = await chat_ops.open_conversation(session, owner, bot_name=name)
        cid = conv.id
        ask = await chat_ops.send(session, owner, cid, "turn the renewals story into a deck")
    await reply_choosing(monkeypatch, cid, ask.message.seq, "propose_task", {"goal": "A renewals deck"})
    async with row_scope(Conversation, cid) as session:
        no = await chat_ops.send(session, owner, cid, "not now, thanks")
    await reply_choosing(monkeypatch, cid, no.message.seq, "decline_pending")
    async with row_scope(Conversation, cid) as session:
        row = await Message.get(session, cid, no.message.seq)
        assert row is not None and (row.verdict, row.task_id) == ("answer", None)
        card = next(m for m in await Message.since(session, cid) if m.proposal)
        assert card.proposal is not None and card.proposal["status"] == "declined"


async def test_a_pure_talker_holds_no_card_verb(monkeypatch: pytest.MonkeyPatch) -> None:
    task_id, cid, owner = await _started()
    await settle(task_id, "Release notes published.")
    async with row_scope(Conversation, cid) as session:
        conv = await session.get(Conversation, cid)
        assert conv is not None
        await bot_ops.set_face(session, actor=owner, name=conv.bot_name, work_mode="never")
        line = await chat_ops.send(session, owner, cid, "shorter")
    tools = await reply_choosing(monkeypatch, cid, line.message.seq, "continue_card")
    assert not tools & (CARD_VERBS | {"propose_task"})


async def test_a_grant_start_carries_the_format_and_tells_the_bot_it_already_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under work mode auto a proposal starts as the reply lands: the bot is told it STARTED (never "once
    you reply"), and the file format it set rides the card into the task's dials."""
    async with session_scope() as session:
        owner = await an_owner(session)
        name = await published_bot(session, owner)
        await bot_ops.set_face(session, actor=owner, name=name, work_mode="auto")
        conv = await chat_ops.open_conversation(session, owner, bot_name=name)
        cid = conv.id
        ask = await chat_ops.send(session, owner, cid, "make this a doc I can download")
    proposal = {"goal": "The standup summary as a Word file", "format": "docx"}
    heard: list[str] = []
    await reply_choosing(monkeypatch, cid, ask.message.seq, "propose_task", proposal, heard=heard)
    assert heard[0].startswith("Started:") and "reply" not in heard[0].lower()

    async with row_scope(Conversation, cid) as session:
        row = await Message.get(session, cid, ask.message.seq)
        assert row is not None and (row.verdict, row.consent) == ("start", "grant") and row.task_id
        task = await session.get(Task, row.task_id)
        assert task is not None and (task.deliverable, task.deliverable_format) == ("document", "docx")


async def test_a_strangers_work_waits_for_the_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A channel sender who is not the owner never starts work, even under work mode auto: the bot's
    proposal waits as a card for the owner's Start, and the bot is told to say it waits for the owner."""
    from prismoid.operations import channels as channel_ops

    async def carried(*_args: Any) -> bool:
        return True

    monkeypatch.setattr(channel_ops, "transport_reply", carried)
    async with session_scope() as session:
        owner = await an_owner(session)
        name = await published_bot(session, owner)
        await bot_ops.set_face(session, actor=owner, name=name, work_mode="auto")
        channel, _raw = await channel_ops.create_channel(session, actor=owner, bot_name=name, kind="webhook")
        await session.commit()
        inbound = await channel_ops.accept_inbound(session, channel, sender="vendor", body="order 40 units")
        cid, seq = uuid.UUID(inbound.conversation_id), inbound.seq
    heard: list[str] = []
    await reply_choosing(monkeypatch, cid, seq, "propose_task", {"goal": "Order 40 units"}, heard=heard)
    assert heard[0].startswith("Waiting:") and "owner" in heard[0]

    async with row_scope(Conversation, cid) as session:
        row = await Message.get(session, cid, seq)
        assert row is not None and (row.verdict, row.task_id) == ("propose", None)
        card = await Message.pending_proposal(session, cid)
        assert card is not None and card.proposal is not None and card.proposal["goal"] == "Order 40 units"
