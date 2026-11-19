# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The summarizer - the handoff a long run goes on from.

When a run's history no longer fits its model's window and clearing old tool results was not enough,
the oldest exchanges are replaced by a handoff (`capabilities/compaction.py`). A handoff written by
code keeps each call's first line and loses what the run decided and found; this cheap call keeps it.
It reads a transcript, holds no tools and never sees the task's files; the call is logged, so a
replayed run reads the summary it wrote the first time.
"""

from __future__ import annotations

from pathlib import Path

from prismoid.ai.agents.base import AgentDef, register
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import ModelClass

PROMPT = (Path(__file__).parent / "prompts" / "summarizer.md").read_text()

SUMMARIZER = register(
    AgentDef(
        name=AgentName("summarizer"),
        model_class=ModelClass.DECIDER,
        description=(
            "Writes the handoff that replaces the oldest part of a long run's history: what was "
            "asked, decisions, findings with sources, open questions, files written. Not for: doing "
            "the work, answering the person, or judging a deliverable."
        ),
        prompt=PROMPT,
        can_ask_user=False,
        plannable=False,  # machinery inside a run's context bound, never a step of its own
    )
)
