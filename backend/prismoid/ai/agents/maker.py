# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The maker - one deliverable file, built with code in the step's computer.

A *role*, not a model. It makes every kind a format skill builds (a Word or PDF document, a web page, a
workbook, a deck, a program, a collection): the loop is one (read the sources, build with a script,
check, look, publish once) and the craft of each file is its format skill, pinned for the step by the
deliverable's kind (`ai/skills/deliverables.py`). That skill also seats the run: its model class, and for a
collection the typed answer the system publishes.
"""

from __future__ import annotations

from pathlib import Path

from prismoid.ai.agents.base import AgentDef, register
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import ModelClass

PROMPT = (Path(__file__).parent / "prompts" / "maker.md").read_text()

MAKER = register(
    AgentDef(
        name=AgentName("maker"),
        model_class=ModelClass.FAST_EXECUTOR,  # the bare seat's; a step's format skill names its own
        description=(
            "Builds one deliverable file with code: a formatted .docx or .pdf document, a "
            "self-contained .html page, an .xlsx workbook whose totals foot, a .pptx deck, a "
            "tested program, or a collection with board, table and timeline views. Not for: "
            "markdown prose answers (the writer's), research, or acting in a service."
        ),
        prompt=PROMPT,
        makes_formats=True,  # its kinds are the format skills'; each kind's suite checks its file
        can_ask_user=True,  # a top-level step agent
        guarded=True,  # a looping step: the run guard's brakes, a retry that takes up its run
        device_browser=True,  # a program built on the device is tested in the task's own browser
    )
)
