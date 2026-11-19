# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A product named as a SUBJECT is not a service to connect.

"Compare 4 note-taking apps (Notion, Obsidian, Evernote, Bear) in a table" must not park on a
"Connect Notion to continue?" gate, which a brief saying "naming a service is asking you to use
it" produces. The brief asks for a connection only when the goal needs the user's own account on
that service (read their data, act in it), and says that a compared, priced or reviewed product
is a topic."""

from __future__ import annotations

from prismoid.ai.context.brief import prompt_for
from prismoid.ai.tools.ask import SUBJECT_IS_NOT_A_SERVICE, with_request_connector
from prismoid.schemas.contract import Contract
from prismoid.schemas.ids import AgentName
from prismoid.schemas.plan import StepSpec

GOAL = (
    "Compare 4 note-taking apps (Notion, Obsidian, Evernote, Bear) in a table on price, platforms, "
    "offline support and export formats."
)


def _told() -> str:
    spec = StepSpec(agent=AgentName("writer"), title="Compare", contract=Contract(goal=GOAL))
    return prompt_for(spec, asker=True)


def test_naming_a_service_is_no_longer_asking_to_use_it() -> None:
    told = _told()
    assert "naming a service is asking you to use it" not in told
    assert "NAMES a service" not in told


def test_a_subject_is_researched_and_an_account_is_connected() -> None:
    told = _told()
    assert "request_connector" in told, "the connect block still exists for real account work"
    assert "the user's OWN account" in told
    assert SUBJECT_IS_NOT_A_SERVICE in told


def test_the_tool_itself_says_the_same() -> None:
    from pydantic_ai.toolsets import FunctionToolset

    toolset = with_request_connector(FunctionToolset[object]())
    doc = " ".join((toolset.tools["request_connector"].description or "").split())
    assert SUBJECT_IS_NOT_A_SERVICE in doc
