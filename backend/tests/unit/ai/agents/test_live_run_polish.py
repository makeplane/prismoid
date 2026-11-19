# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Two endings a live amazon.in run got wrong. A cart filled without signing in is a GUEST cart -
the answer says so from the agent's typed `session` fact, never from its prose. And the computer's
prose between tool calls ("Apply the brand filter.") is working noise: it lights the progress tree
and never becomes a bubble in the thread, whose answer is the Outcome summary."""

from __future__ import annotations

import prismoid.ai.agents.all  # noqa: F401  # pyright: ignore[reportUnusedImport] - registers every built-in
from prismoid.ai.agents.base import REGISTRY
from prismoid.schemas.guard import GUEST_NOTE, outcome_narration
from prismoid.schemas.ids import AgentName
from prismoid.schemas.outputs import Outcome


def test_a_guest_session_says_so_in_the_answer() -> None:
    ended = Outcome(status="done", summary="Added the air fryer to the Amazon.in cart.", session="guest")
    assert outcome_narration(ended) == f"Added the air fryer to the Amazon.in cart.\n\n{GUEST_NOTE}"


def test_a_signed_in_or_accountless_ending_reads_as_its_summary() -> None:
    for session in ("signed_in", None):
        ended = Outcome(status="done", summary="Done.", session=session)
        assert outcome_narration(ended) == "Done."


def test_a_partial_guest_ending_keeps_both_words() -> None:
    ended = Outcome(status="partial", summary="Added one of two.", session="guest")
    assert outcome_narration(ended) == f"**Partly done.** Added one of two.\n\n{GUEST_NOTE}"


def test_the_computer_keeps_its_inter_tool_prose_off_the_thread() -> None:
    assert REGISTRY[AgentName("operator")].quiet_stream
