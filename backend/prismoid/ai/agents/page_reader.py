# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The page reader - `web_fetch(view="extract")`'s cheap model.

A step that fetches a page wants a few passages, not the whole page in its context. The page reader
reads one page (or one chunk of a long one) against the step's goal and answers with quotes and a
short summary. It never sees the conversation and holds no tools; code checks its quotes against the
page before the step reads them (`ai/tools/reading.py`), so a quote it invents never arrives.
"""

from __future__ import annotations

from pathlib import Path

from prismoid.ai.agents.base import AgentDef, register
from prismoid.ai.tools.reading import PageReading
from prismoid.schemas import outputs
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import ModelClass

PROMPT = (Path(__file__).parent / "prompts" / "page_reader.md").read_text()

outputs.register_output("page_reading", PageReading)

PAGE_READER = register(
    AgentDef(
        name=AgentName("page_reader"),
        model_class=ModelClass.CHEAP_CLASSIFIER,
        description=(
            "Reads one fetched web page against a goal and returns verbatim quotes and a short "
            "summary. Not for: fetching, searching, judging sources, or writing the answer."
        ),
        prompt=PROMPT,
        output_ref="page_reading",
        can_ask_user=False,
        plannable=False,  # machinery inside web_fetch, never a step of its own
        identity=False,  # it copies the page's words; the house voice would bend a quote
    )
)
