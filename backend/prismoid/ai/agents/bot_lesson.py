# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""bot_lesson - a bot's next draft, from the author's idea or from one finished run.

One single-call agent with no tools: the brief names its kind (`from_idea` or `from_run`) and the
answer is a whole `BotLesson`. It proposes; the author disposes: `operations/bot_lessons` lands it
only as an untested draft the author diffs, tests and publishes or discards, never a published bot.
"""

from __future__ import annotations

from pathlib import Path

from prismoid.ai.agents.base import AgentDef, register
from prismoid.schemas import outputs
from prismoid.schemas.bots import BotLesson
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import ModelClass

PROMPT = (Path(__file__).parent / "prompts" / "bot_lesson.md").read_text()

outputs.register_output("bot_lesson", BotLesson)

BOT_LESSON = register(
    AgentDef(
        name=AgentName("bot_lesson"),
        model_class=ModelClass.FRONTIER_REASONER,  # authoring quality is the product
        description=(
            "Writes a bot's next draft from its author's idea or from one finished run: whole "
            "instructions and new selection cases. Not for: doing the work, planning, or judging."
        ),
        prompt=PROMPT,
        output_ref="bot_lesson",
        can_ask_user=False,  # the author reviews the draft; there is nothing to ask mid-run
        plannable=False,  # machinery: no plan may place a step that rewrites an agent
    )
)
