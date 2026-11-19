# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The operator - acts in the world: in the person's connected services, on the task's computer, on websites.

One agent for the work whose deliverable is a change or an answer found by acting: a page created through a
connector, a command run, a cart filled on a site. It finishes with `done(Outcome)`, whose proof the system
checks before it believes it (`operations/execution/proof.py`).

Where it works is the step's fact, not the agent's: a step that drives the browser (`StepSpec.browser`, set by
code when the step is minted) takes the browser seat - the browser image, the browser tools, the craft of the
built-in `browse` skill, and no connect block, since a step told to connect first never opens the page. Any
other step is a connector act or computer work with no browser, starting on paper; the connect block is its
own (`request_connector`). Both seats keep one prompt, one model class and one ending.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from prismoid.ai.agents.base import AgentDef, register
from prismoid.ai.skills.deliverables import builtin_craft
from prismoid.schemas.ids import AgentName
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.outputs import VerifierSuite

PROMPT = (Path(__file__).parent / "prompts" / "operator.md").read_text()

BROWSE: Final = builtin_craft("browse")

OPERATOR = register(
    AgentDef(
        name=AgentName("operator"),
        model_class=ModelClass.COMPUTER_USE,  # strong structured tool use and vision; the browser seat needs both
        description=(
            "Acts in the world and reports what it did or found: changes things in connected services "
            "through their tools (creates the page, sends the mail, files the ticket), runs commands and "
            "works on files on the task's computer, and, where this deployment has computer use, drives "
            "websites in a real browser (searches and filters a site, signs in through the person, fills "
            "forms, books, buys, checks a page). It asks for details it lacks and hands the person the "
            "screen to sign in or pay. The change or the answer is the deliverable; it publishes a file "
            "only when the goal asks for one. Not for research across many sources or documents written "
            "from them (the researcher, the writer), or program deliverables nobody needs to see running "
            "(the maker)."
        ),
        prompt=PROMPT,
        output_ref="outcome",  # finishes with `done(Outcome)`
        # The engine's receipt for a browser step is its recording (structural, no model); a file it
        # publishes is verified by its kind's suite after delivery.
        verifier_suite=VerifierSuite.RECORDING,
        can_ask_user=True,
        guarded=True,  # a looping step: the run guard's brakes, a retry that takes up its run
        # Its prose between tool calls ("Apply the brand filter.") is working noise: the thread's answer
        # is the Outcome summary.
        quiet_stream=True,
        browser_craft=BROWSE,
    )
)
