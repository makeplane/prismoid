# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The judge - reads a finished deliverable in a fresh context, given only the rubric and the work.

One reader for both reviews: the receipt after delivery, and the long-form lane's editorial review
before it, whose findings become the rebuild's brief.

Its seat is the furthest from the maker the host can build (`gateway/resolve.judge_seats`): another
family, else another model of that family, else the maker's own model; the receipt says which."""

from __future__ import annotations

from pathlib import Path

from prismoid.ai.agents.base import AgentDef, register
from prismoid.schemas import outputs
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.verification import JudgeVerdict

PROMPT = (Path(__file__).parent / "prompts" / "judge.md").read_text()

# The judge's output shape lives in `schemas/verification.py` (it is a verification value, not an
# agent-specific one). This is what `register_output` exists for - and it keeps `schemas/outputs.py`
# from having to import `schemas/verification.py`, which imports it right back.
outputs.register_output("judge_verdict", JudgeVerdict)

JUDGE = register(
    AgentDef(
        name=AgentName("judge"),
        model_class=ModelClass.JUDGE,
        description=(
            "Reads a finished deliverable against its sources, the task's acceptance criteria and "
            "the deterministic suite's findings, says whether it should ship, and names each defect "
            "with the one change that fixes it. Seated as far "
            "from the model that produced it as the host allows. Not for: writing, editing, or "
            "repairing the work - a verifier that also fixes what it finds is marking its own "
            "homework one step later."
        ),
        prompt=PROMPT,
        output_ref="judge_verdict",
        can_ask_user=False,  # a verifier that can ask the author for help is not a verifier
        plannable=False,  # verification seats the judge, apart from the maker. A plan that picks
        #                   its own judge picks a friendly one.
        identity=False,  # the identity makes a seat one of the family, and the
        #                  judge's whole value is that it is not - it reads the work fresh,
        #                  never as one of the authors
    )
)
