# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The plain loop: one model, every tool a single agent can hold, no plan engine - the comparison row.

It is the design check beside the decision runner: if this loop beats the routed design by three
points at equal cost across two model changes, the design is revisited. The loop is a bot, compiled
the way every user-authored agent is (`compile_bot`), granted every tool a bot may hold and the ask,
on the Lead's model class, so the two configurations differ in their design and not their model.
It runs through `DecideRunner` and is scored the same way: its first ending or first act is its outcome.
"""

from __future__ import annotations

import uuid
from typing import Final

from pydantic_ai.models import Model

from evals.runners import Runner
from evals.runners.decide import DecideRunner, Seat
from prismoid.ai.agents.lead import LEAD
from prismoid.schemas.bots import BOT_TOOLS, BotManifest, ResolvedBot
from prismoid.schemas.ids import ActorId, BotName

NAME: Final = BotName("plain-loop")

INSTRUCTIONS: Final = (
    "Do what the person asks, yourself, with the tools you hold. When the answer is text, answer in "
    "text. When the ask needs work, do the work. Ask a question only when a wrong guess would ruin "
    "the result; otherwise state your assumption and go on."
)

BOT: Final = ResolvedBot(
    name=NAME,
    version=1,
    manifest=BotManifest(
        name=NAME,
        description="Does any ask end to end in one loop with every tool. Not for production routing.",
        use_when=("any ask",),
        capability=LEAD.model_class,
        tools=tuple(sorted(BOT_TOOLS)),
        can_ask_user=True,
    ),
    instructions=INSTRUCTIONS,
    owner_actor_id=ActorId(uuid.UUID(int=0)),
)


def runner(seat: Seat, model: Model | None = None) -> Runner:
    """The plain loop on `seat`, or on `model` offline."""
    return DecideRunner(str(NAME), seat, model, bots=(BOT,))
