# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The tool catalog's side-effect classes, and what steering's "acted outside" makes of them: a
read, a question, a Prismoid record and the frame's own tools stay inside the task; a world act, any
connector tool (a declared read too: its server's word is not the catalog's), and a name nobody tagged
may have acted."""

from __future__ import annotations

import pytest

from prismoid.ai.tools import catalog
from prismoid.operations.steering import acted_outside
from prismoid.schemas.connectors import ConnectorToolInfo
from prismoid.schemas.side_effects import SideEffect

LINEAR = catalog.connector_side_effects(
    "linear",
    (
        ConnectorToolInfo(name="list_issues", read_only=True),
        ConnectorToolInfo(name="create_issue", read_only=False),
        ConnectorToolInfo(name="search", read_only=None),
        ConnectorToolInfo(name="purge", read_only=True, destructive=True),
    ),
)


def test_every_tool_has_one_row_and_one_journal_name() -> None:
    names = [tool.name for tool in catalog.TOOLS]
    journals = [tool.journal for tool in catalog.TOOLS]
    assert len(set(names)) == len(names)
    assert len(set(journals)) == len(journals)
    assert all(catalog.journal_name(tool.name) == tool.journal for tool in catalog.TOOLS)


def test_a_connector_tool_is_a_read_only_when_its_server_declared_it_and_nothing_destructive() -> None:
    assert LINEAR == {
        "linear_list_issues": SideEffect.READ,
        "linear_create_issue": SideEffect.WORLD,
        "linear_search": SideEffect.WORLD,
        "linear_purge": SideEffect.WORLD,
    }


@pytest.mark.parametrize(
    ("tool", "acted"),
    [
        ("web.fetch", False),  # a read
        ("frame.read_file", False),
        ("frame.write_file", False),  # the task's own Machine
        ("frame.publish", False),  # a Prismoid record
        ("routine.create", True),  # a record that outlives the task: twice would be two routines
        ("propose_task", False),  # a record of the conversation's own
        ("ask_user", False),  # a question
        ("linear_list_issues", True),  # a connector tool its server declared read-only: conservative
        ("frame.egress", False),  # a refusal, never a call
        ("connector.linear", False),  # a connector the mount dropped
        ("linear_create_issue", True),  # a connector write
        ("linear_search", True),  # a connector tool its server declared nothing about
        ("notion_list_pages", True),  # a connector tool nobody declared anything about
        ("browser.snapshot", True),  # the browser's live session
        ("teleport_user", True),  # a name nobody tagged
    ],
)
def test_acted_outside_reads_the_side_effect_class(tool: str, acted: bool) -> None:
    assert acted_outside(tool, egress=False) is acted


@pytest.mark.parametrize("egress", [False, True])
def test_a_shell_acts_outside_only_when_its_frame_has_a_route_out(egress: bool) -> None:
    assert acted_outside("frame.shell", egress=egress) is egress
