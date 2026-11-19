# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The browser sidecar is stdlib-only and runs in the frame as another user, so it cannot import
the worker's modules and keeps its own copy of two facts. Each copy is pinned to its source here:
a value changed on one side only would break every frame, not one test."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from prismoid.ai.frames import layout
from prismoid.ai.frames.image_browser.sidecar import verbs as sidecar_verbs
from prismoid.schemas import computer_use

SIDECAR_DIR = Path(sidecar_verbs.__file__).parent


def test_the_sidecar_acts_on_exactly_the_verbs_the_engine_records_as_acts() -> None:
    assert sidecar_verbs.ACTING_VERBS == computer_use.ACTING_VERBS


def test_every_verb_the_engine_records_is_one_the_sidecar_answers() -> None:
    assert computer_use.RECORDED_VERBS <= sidecar_verbs.VERBS


def _socket_defaults(source: str) -> list[object]:
    """The default of every `os.environ.get("SIDECAR_SOCK", default)` in `source`."""
    found: list[object] = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and ast.unparse(node.func) == "os.environ.get"
            and len(node.args) == 2
            and ast.unparse(node.args[0]) == "'SIDECAR_SOCK'"
        ):
            found.append(ast.literal_eval(node.args[1]))
    return found


@pytest.mark.parametrize("module", ["ctl", "daemon", "screen"])
def test_each_sidecar_entry_point_defaults_to_the_layouts_socket_path(module: str) -> None:
    defaults = _socket_defaults((SIDECAR_DIR / f"{module}.py").read_text())
    assert defaults == [layout.SIDECAR_SOCK]
