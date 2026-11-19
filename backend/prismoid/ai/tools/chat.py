# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`continue_card`, `accept_pending`, `decline_pending`: a bot's hands on the cards of its conversation
(ADR-060).

The model judges what the person's newest line means for a card; code decides what happens. Each
verb calls a closure the reply lane wired (the `propose_task` precedent: the engine stays
persistence-free), which records the choice and answers with a line the model relays; the lane
carries it out after the reply, under consent and the card's state. A verb exists only while its
card does: no task card right above the line, no `continue_card`; no waiting proposal, neither
`accept_pending` nor `decline_pending`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from typing import Final

from pydantic_ai.toolsets import FunctionToolset

from prismoid.ai.tools import catalog

CardVerb = Callable[[], Awaitable[str]]

# The card verbs by the names the model calls them, in catalog order.
CHAT_TOOLS: Final = tuple(tool.name for tool in catalog.family("chat"))


@dataclass(frozen=True)
class ChatHands:
    """The card verbs one reply holds; None is a verb this reply does not have."""

    continue_card: CardVerb | None = None
    accept_pending: CardVerb | None = None
    decline_pending: CardVerb | None = None


def _never() -> CardVerb:
    async def refuse() -> str:
        return "That card is not here any more."

    return refuse


def chat_toolset(hands: ChatHands, granted: Collection[str]) -> FunctionToolset[object] | None:
    """The verbs both wired and `granted` (model names), or None when there are none."""
    held = {name for name in CHAT_TOOLS if name in granted and getattr(hands, name) is not None}
    on_card, on_yes, on_no = (
        hands.continue_card or _never(),
        hands.accept_pending or _never(),
        hands.decline_pending or _never(),
    )

    async def continue_card() -> str:
        """Hand the person's newest message to the task on the card right above it, word for word:
        it steers the task while it runs, or starts its next turn once delivered. Call it when the
        message reacts to or changes that task's work. A question, or a new and separate ask, is
        not for the card: answer it yourself."""
        return await on_card()

    async def accept_pending() -> str:
        """Start the task you proposed that is waiting for consent, because the person's newest
        message says yes to it. Only on a clear yes to that proposal."""
        return await on_yes()

    async def decline_pending() -> str:
        """Set aside the task you proposed that is waiting for consent, because the person's newest
        message turns it down."""
        return await on_no()

    if not held:
        return None
    toolset: FunctionToolset[object] = FunctionToolset(id="prismoid-chat")
    for verb in (continue_card, accept_pending, decline_pending):
        if verb.__name__ in held:
            toolset.add_function(verb, name=verb.__name__)
    return toolset
