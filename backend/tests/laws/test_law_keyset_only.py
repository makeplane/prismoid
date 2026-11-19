# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Keyset pagination only: no `.offset(` call anywhere in `backend/prismoid` (ADR-075).

Protects the root CLAUDE.md line "Alembic only. Keyset pagination only." An OFFSET page reads and
throws away every row before it, and it skips or repeats rows when the list moves under it; a keyset
page does neither. The population is every `.py` file under `prismoid/` (migrations included), read as an
AST: a call whose callee is an attribute named `offset`, whatever it is called on.
"""

from __future__ import annotations

import ast

from tests.support.paths import BACKEND

PRISMOID = BACKEND / "prismoid"


def offset_calls(source: str) -> list[int]:
    """The line of every `<anything>.offset(...)` call in `source`."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "offset"
    ]


def test_no_offset_in_prismoid() -> None:
    found = [
        f"{path.relative_to(PRISMOID.parent)}:{line}"
        for path in sorted(PRISMOID.rglob("*.py"))
        for line in offset_calls(path.read_text())
    ]
    assert not found, f"OFFSET pagination, keyset only: {found}"


def test_the_law_bites() -> None:
    assert offset_calls("rows = select(Task).order_by(Task.id).offset(page * 50).limit(50)\n") == [1]
    assert offset_calls("rows = select(Task).where(Task.id > after).limit(50)\noffset = 3\n") == []
