# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The writer: the prose a person receives, from memos to long documents.

Its answer IS the document: it finishes with `submit_document` (a title and the markdown below it, typed),
and `publishes_output=True` makes the system publish it through the one door, so nothing the model says
to the requester reaches the file and the draft streams from the call's arguments as they arrive. It needs
no computer: it reads its sources, opens the pages it cites, and states a figure only from a source,
from a step that ran code, or by one shown step of arithmetic the document suite re-derives.

In a fleet it plays one more part (`modes`), with the craft of the built-in `fleet-table` skill: the table that
lays every model's answer side by side, published as the fleet's report and checked by the quote suite. In
the long-form lane (`operations/longform.py`) it designs the typed outline and then writes the parts in order,
with the craft of the built-in `longform` skill.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from prismoid.ai.agents.base import AgentDef, Mode, blocks_of, register
from prismoid.ai.capabilities.citations import CitationCheck
from prismoid.ai.skills.deliverables import builtin_craft
from prismoid.schemas import outputs
from prismoid.schemas.ids import AgentName
from prismoid.schemas.longform import OutlineResult
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import VerifierSuite

PROMPT = (Path(__file__).parent / "prompts" / "writer.md").read_text()

outputs.register_output("outline_result", OutlineResult)

_LONGFORM: Final = {b.id: b.text for b in blocks_of(builtin_craft("longform"), "longform")}
_LONG: Final = _LONGFORM["longform.a-long-document"]
_DESIGN: Final = _LONG + _LONGFORM["longform.designing-the-outline"]

OUTLINE: Final = Mode(
    lane_role="outline",
    output_ref="outline_result",
    model_class=ModelClass.FRONTIER_REASONER,  # an outline error fans into every part
    craft=_DESIGN,
    guarded=True,  # reads its sources in a loop: the run guard's brakes, a retry that takes up its run
)
PROPOSAL: Final = Mode(  # one of a consensus outline's proposals (quality=max); the merge is `OUTLINE`
    lane_role="outline_draft",
    output_ref="outline_result",
    model_class=ModelClass.FRONTIER_REASONER,
    craft=_DESIGN,
    guarded=True,
)
PART: Final = Mode(
    lane_role="section",
    output_ref="part",
    model_class=ModelClass.FAST_EXECUTOR,
    craft=_LONG + _LONGFORM["longform.writing-one-part"],
    guarded=True,  # one part at a time: a retry takes up its run
)

# A fleet's table: the built-in `fleet-table` skill's craft.
TABLE: Final = Mode(
    lane_role="fleet_table",
    output_ref="document",
    model_class=ModelClass.FAST_EXECUTOR,  # transcription with manners; the quote suite holds it honest
    craft=builtin_craft("fleet-table"),
    capabilities=(CitationCheck(),),  # the table is a document: an unread source id is sent back
    quiet_stream=False,  # the table is the fleet's deliverable: its draft streams
)

WRITER = register(
    AgentDef(
        name=AgentName("writer"),
        model_class=ModelClass.FAST_EXECUTOR,
        description=(
            "Writes the prose a person receives: memos, reports, briefs, summaries, documentation and "
            "messages to send. Leads with the conclusion, cites every claim, and reads the pages it "
            "cites, including any URL the task names (web pages, PDFs). No computer: its text is "
            "published as the file. Not for: spreadsheets, decks, code, or a formatted .docx/.pdf "
            "file (those are built with code on a computer)."
        ),
        prompt=PROMPT,
        output_ref="document",  # it finishes with `submit_document`: the title and the document, typed
        publishes_output=True,  # the system publishes the submitted document
        verifier_suite=VerifierSuite.DOCUMENT,
        can_ask_user=True,  # a top-level step agent
        guarded=True,  # a looping step: the run guard's brakes, a retry that takes up its run
        cites=True,  # cites a page by its task-wide source id, an earlier step's read included
        capabilities=(CitationCheck(),),  # a document citing an id no read was given is sent back
        modes=(TABLE, OUTLINE, PROPOSAL, PART),  # the fleet's table; the long-form lane's parts
    )
)
