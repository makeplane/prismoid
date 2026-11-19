# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The namer - a task's label, minted while the work starts.

A sidebar full of raw goals reads like a paste bin: "Explain to me completely about cars
and bikes." is an ask, not a name. The namer turns the ask into a shelf label - one cheap
call, at most 60 characters - so every surface (sidebar, breadcrumb, board card) says what
the work IS. The user's own rename always wins: the namer only fills silence, never
overwrites a human (the title is the creator's label; the goal stays immutable).
"""

from __future__ import annotations

from pathlib import Path

from prismoid.ai.agents.base import AgentDef, register
from prismoid.schemas import outputs
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import TaskTitle

PROMPT = (Path(__file__).parent / "prompts" / "namer.md").read_text()

outputs.register_output("task_title", TaskTitle)

NAMER = register(
    AgentDef(
        name=AgentName("namer"),
        model_class=ModelClass.CHEAP_CLASSIFIER,
        description=(
            "Mints a short display title (≤60 chars) for a task from its goal. Not for: "
            "doing the work, summarising sources, or renaming a task a human already named."
        ),
        prompt=PROMPT,
        output_ref="task_title",
        can_ask_user=False,
        plannable=False,  # machinery, not work - it labels the shelf, it is never on it
    )
)
