# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The call log's unit names are pinned with `LOG_VERSION` (ADR-089). A unit's name carries its agent,
toolset id and tool, so renaming or removing any of them strands a step's logged calls across a
deploy: the renamed call misses its row and runs again. The names are derived from the real roster
and mounts (`tests/support/durability_pins.py`) and must equal the pins for the current
`LOG_VERSION`. A new name strands nothing (no logged call carries it), so it is added to the current
section with no bump. A renamed or removed name appends a new version's section to
`tests/fixtures/operation_name_pins.json` and bumps the constant in the same change, which fences
every generation logged under the old names; a section only ever grows in place."""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic_ai.toolsets import FunctionToolset

import prismoid.ai.tools.frame_tools as frame_tools
from prismoid.ai.capabilities.durability import LOG_VERSION
from tests.support.durability_pins import operation_names
from tests.support.paths import TESTS

PINS = TESTS / "fixtures" / "operation_name_pins.json"


def _pins() -> dict[int, list[str]]:
    return {int(version): names for version, names in json.loads(PINS.read_text()).items()}


def test_the_unit_names_are_the_ones_pinned_for_this_log_version() -> None:
    pinned = _pins()
    assert max(pinned) == LOG_VERSION, "a new pin section comes with a LOG_VERSION bump"
    names, pins = set(operation_names()), set(pinned[LOG_VERSION])
    assert not pins - names, (
        f"unit names renamed or removed {sorted(pins - names)}: append a section under a new "
        "LOG_VERSION and bump the constant"
    )
    assert not names - pins, (
        f"new unit names {sorted(names - pins)}: add them to the current section, no bump"
    )


def test_every_version_bump_pinned_a_changed_list() -> None:
    pinned = _pins()
    versions = sorted(pinned)
    assert versions == list(range(1, LOG_VERSION + 1))
    assert all(pinned[v] != pinned[v + 1] for v in versions[:-1]), (
        "a bump without a rename fences for nothing"
    )


def test_a_planted_rename_is_caught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drill D7, the bite: the frame toolset under another id no longer matches its pins."""

    def renamed(*, sequential: bool, id: str) -> FunctionToolset[Any]:
        return FunctionToolset(sequential=sequential, id=f"{id}-renamed")

    monkeypatch.setattr(frame_tools, "FunctionToolset", renamed)
    assert operation_names() != _pins()[LOG_VERSION]
