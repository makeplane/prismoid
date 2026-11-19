# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The researcher - search, read, cite.

It answers a question from what it reads: the attached files, the pages the web door brings back, the
notes it keeps along the way. Its ANSWER is the deliverable (`publishes_output=True`, text), so the
system publishes it through the one door: a direct answer with its evidence, or the findings a later
step writes from. Every page it reads carries a source id and its answer cites those ids (`cites`); the
publish turns each into the page's link and refuses an id no read was given, so a URL is never re-typed
and a citation always names a page that was read.

It is verified like any document (the document suite checks its figures and quotes against the pages
the task kept), and the run ends in an answer even at its ceiling (`ForcedAnswer`): the last request
the ceiling allows has its tools off.

In the scatter lane it plays two parts (`modes`), with the craft of the built-in `scatter` skill: the
map, whose typed `ScatterResult` the lane expands into seats, and each gather seat, whose `RecordSet`
the lane tallies. The map is one looping seat on the frontier class; a wave of seats runs on the fast
class, each record naming its page by source id.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from prismoid.ai.agents.base import AgentDef, Mode, blocks_of, register
from prismoid.ai.capabilities.citations import CitationCheck
from prismoid.ai.capabilities.forced_answer import ForcedAnswer
from prismoid.ai.capabilities.record_sources import RecordSources
from prismoid.ai.skills.deliverables import builtin_craft
from prismoid.schemas import outputs
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.swarm import RecordSet, ScatterResult

PROMPT = (Path(__file__).parent / "prompts" / "researcher.md").read_text()

outputs.register_output("scatter_result", ScatterResult)
outputs.register_output("record_set", RecordSet)

_SCATTER: Final = {b.id: b.text for b in blocks_of(builtin_craft("scatter"), "scatter")}
_SWEEP: Final = _SCATTER["scatter.a-wide-sweep"]

MAP: Final = Mode(
    lane_role="scatter_map",
    output_ref="scatter_result",
    model_class=ModelClass.FRONTIER_REASONER,  # most of a sweep's misses are lost in discovery, before any seat runs
    craft=_SWEEP + _SCATTER["scatter.mapping-the-territory"],
    guarded=True,  # one seat looping on search and fetch: the stall brakes and a retry that takes up its run
)
SEAT: Final = Mode(
    lane_role="gather",
    output_ref="record_set",
    model_class=ModelClass.FAST_EXECUTOR,  # the wave is wide; a retry climbs the chain
    craft=_SWEEP + _SCATTER["scatter.harvesting-one-territory"],
    capabilities=(RecordSources(),),
)

RESEARCHER = register(
    AgentDef(
        name=AgentName("researcher"),
        model_class=ModelClass.LONG_CONTEXT,
        description=(
            "Searches the web, reads every source (attached files, web pages, PDFs, Office files) and "
            "answers with citations: a direct answer to a question that needs several reads, or the "
            "findings a later step writes from (figures in their exact wording, contradictions, gaps). "
            "Not for: writing the final document, or anything one read settles."
        ),
        prompt=PROMPT,
        output_ref=None,  # text: the answer or the findings
        publishes_output=True,  # the system publishes the answer; no file tools, no Machine
        verifier_suite=VerifierSuite.DOCUMENT,
        cites=True,
        can_ask_user=True,  # a top-level step agent
        guarded=True,  # a looping step: the run guard's brakes, a retry that takes up its run
        capabilities=(
            CitationCheck(),
            ForcedAnswer(),
        ),  # an unread source id is sent back; the ceiling answers
        modes=(MAP, SEAT),
    )
)
