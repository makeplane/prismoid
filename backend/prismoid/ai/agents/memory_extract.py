# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""memory_extract - what a turn taught, kept only in the person's own quoted words.

One cheap call after a delivered turn or an answered conversation. It reads only what the person
typed (the ask, their answers, their steers, the owner's lines to a bot) and what is already
remembered, never an agent's words. Each item quotes them; code keeps an item only when the quote
is verbatim in what they said, and only code picks the lane (`operations/memory`).
"""

from __future__ import annotations

from pathlib import Path

from prismoid.ai.agents.base import AgentDef, register
from prismoid.schemas import outputs
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import MemoryExtract

PROMPT = (Path(__file__).parent / "prompts" / "memory_extract.md").read_text()

outputs.register_output("memory_extract", MemoryExtract)

MEMORY_EXTRACT = register(
    AgentDef(
        name=AgentName("memory_extract"),
        model_class=ModelClass.CHEAP_CLASSIFIER,
        description=(
            "Quotes at most three durable memories from what a person said on one turn: standing "
            "preferences, stable facts, explicit always/nevers. Not for: summarising work or planning."
        ),
        prompt=PROMPT,
        output_ref="memory_extract",
        can_ask_user=False,
        plannable=False,  # machinery: it files what a person said, it never does work
    )
)
