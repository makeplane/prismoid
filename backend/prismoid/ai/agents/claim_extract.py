# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""claim_extract - a finished document's checkable claims, each with a span quoted from a source.

One bounded call per sourced document's receipt (`operations/verification`). It reads the document and
the sources it was given and lists the figures and cited statements, each with the source labels the
document attaches and the shortest source span that states it. It proposes only: code keeps a quote only
when it is verbatim in that source and decides every outcome (`ai/verification/grounding.py`).
"""

from __future__ import annotations

from pathlib import Path

from prismoid.ai.agents.base import AgentDef, register
from prismoid.schemas import outputs
from prismoid.schemas.claims import ClaimSet
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import ModelClass

PROMPT = (Path(__file__).parent / "prompts" / "claim_extract.md").read_text()

outputs.register_output("claim_set", ClaimSet)

CLAIM_EXTRACT = register(
    AgentDef(
        name=AgentName("claim_extract"),
        model_class=ModelClass.CHEAP_CLASSIFIER,
        description=(
            "Lists a finished document's figures and cited statements, each with the sources it cites "
            "and a span quoted exactly from a source. Not for: judging, writing or fixing the document."
        ),
        prompt=PROMPT,
        output_ref="claim_set",
        can_ask_user=False,
        plannable=False,  # machinery: the receipt seats it, a plan never does
    )
)
