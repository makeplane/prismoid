# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A stranger's work request on a bot channel knocks the owner's inbox."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from prismoid.db.models.conversation import Conversation
from prismoid.db.models.inbox import InboxItem, InboxKind
from prismoid.db.session import row_scope, session_scope
from prismoid.operations import bots as bot_ops
from prismoid.operations import channels as channel_ops
from prismoid.operations import inbox as inbox_ops
from prismoid.operations import live
from tests.support.chat_cast import an_owner, published_bot
from tests.support.chat_model import reply_choosing

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def quiet_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    async def quiet(event: Any) -> None:
        return None

    async def carried(*_args: Any) -> bool:
        return True

    monkeypatch.setattr(live, "notify", quiet)
    monkeypatch.setattr(channel_ops, "transport_reply", carried)


async def test_a_strangers_waiting_card_knocks_the_owner_once(monkeypatch: pytest.MonkeyPatch) -> None:
    async with session_scope() as session:
        owner = await an_owner(session)
        name = await published_bot(session, owner)
        await bot_ops.set_face(session, actor=owner, name=name, work_mode="auto")
        channel, _raw = await channel_ops.create_channel(session, actor=owner, bot_name=name, kind="webhook")
        await session.commit()
        inbound = await channel_ops.accept_inbound(session, channel, sender="vendor", body="order 40 units")
        cid, seq = uuid.UUID(inbound.conversation_id), inbound.seq
        owner_id = owner.id
    await reply_choosing(monkeypatch, cid, seq, "propose_task", {"goal": "Order 40 units"}, heard=[])

    async with row_scope(Conversation, cid) as session:
        owner = await session.get(type(owner), owner_id)
        assert owner is not None
        items = [
            i for i in await InboxItem.list_for(session, owner) if i.kind == InboxKind.CARD_WAITING.value
        ]
        assert len(items) == 1, f"owner card_waiting items: {len(items)}"
        assert items[0].payload["conversation_id"] == str(cid) and items[0].payload["sender"] == "vendor"
        assert "Order 40 units" in items[0].body
        # A second ask in the same conversation while the first is unread stacks nothing.
        again = await inbox_ops.card_waiting(
            session,
            actor_id=owner_id,
            bot_name=name,
            conversation_id=cid,
            seq=seq + 2,
            sender="vendor",
            goal="Order 50 units",
        )
        assert again is None
