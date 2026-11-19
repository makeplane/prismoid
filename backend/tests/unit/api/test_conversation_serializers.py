# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A proposal card names its document's file format, so the card can say what file the work makes."""

import datetime as dt
import uuid

from prismoid.api.serializers.conversations import message_out
from prismoid.db.models.message import Message


def _card(proposal: dict[str, object]) -> Message:
    return Message(
        conversation_id=uuid.uuid4(),
        seq=4,
        role="bot",
        kind="text",
        text="I can build that as a Word file.",
        sender="standup-helper",
        verdict="none",
        verdict_reason="",
        consent="none",
        status="sent",
        detail="",
        artifact_ids=[],
        proposal=proposal,
        meta={},
        created_at=dt.datetime.now(dt.UTC),
    )


def test_a_cards_format_reaches_the_response() -> None:
    card = message_out(_card({"goal": "Summarise the standup", "deliverable": "document", "format": "docx"}))
    assert card.proposal is not None and card.proposal.format == "docx"


def test_a_card_without_a_format_says_none() -> None:
    card = message_out(_card({"goal": "Build the tracker", "deliverable": "spreadsheet"}))
    assert card.proposal is not None and card.proposal.format is None
