# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Model features stay in the gateway (`ai/CLAUDE.md`: provider quirks live in `gateway/`): what a wire
protocol can do is the `FEATURES` table in `ai/gateway/providers.py`, and every other module asks it
instead of comparing a protocol. The population is every `.py` file under `prismoid/`, read as an AST, outside
the two provider homes (`ai/gateway/providers*` and `ai/tools/providers/`, where model strings may
live). A module fails when it compares a protocol (`x.protocol == ...`, `Protocol(p) in ...`), names a
member (`Protocol.ANTHROPIC`), or compares a family to a protocol's name (`family == "anthropic"`)."""

from __future__ import annotations

import ast
import functools
import itertools

from prismoid.schemas.model_class import Protocol
from tests.support.paths import BACKEND

PRISMOID = BACKEND / "prismoid"
HOMES = ("ai/gateway/providers", "ai/tools/providers/")
NAMES = frozenset(p.value for p in Protocol)


def _protocol(node: ast.expr) -> bool:
    """A wire protocol value: a `protocol` name or attribute, a `Protocol(...)` parse, or a member."""
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id == "Protocol"
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "Protocol":
        return True
    name = node.id if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""
    return name == "protocol" or name.endswith("_protocol")  # a constant like DISPLAY_PROTOCOL is no wire


def _family_named(left: ast.expr, right: ast.expr) -> bool:
    """A family compared with a protocol's name, spelled as a string."""
    name = left.id if isinstance(left, ast.Name) else left.attr if isinstance(left, ast.Attribute) else ""
    return "family" in name.lower() and isinstance(right, ast.Constant) and right.value in NAMES


def checks(source: str) -> list[int]:
    """The lines of one module that check a protocol or a family by hand."""
    found: set[int] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            if any(_protocol(side) for side in sides) or any(
                _family_named(a, b) or _family_named(b, a) for a, b in itertools.pairwise(sides)
            ):
                found.add(node.lineno)
        elif (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "Protocol"
            and node.attr in Protocol.__members__
        ):
            found.add(node.lineno)
    return sorted(found)


@functools.cache
def population() -> dict[str, list[int]]:
    return {
        path.relative_to(PRISMOID).as_posix(): checks(path.read_text())
        for path in sorted(PRISMOID.rglob("*.py"))
    }


def strays(found: dict[str, list[int]]) -> list[str]:
    """The hand-written protocol checks outside the provider homes."""
    return [f"prismoid/{p}:{lines}" for p, lines in found.items() if lines and not p.startswith(HOMES)]


def test_no_protocol_check_outside_the_provider_homes() -> None:
    stray = strays(population())
    assert not stray, f"ask `providers.features(protocol)` instead of checking a protocol by hand: {stray}"
    assert any(lines for p, lines in population().items() if p.startswith(HOMES)), "the law watches nothing"


def test_the_law_bites() -> None:
    assert checks("if row.protocol == Protocol.ANTHROPIC:\n    pass\n") == [1]
    assert checks("ok = protocol != 'openai'\n") == [1]
    assert checks("ok = Protocol(spec.protocol) not in CHAT\n") == [1]
    assert checks("CHAT = frozenset({Protocol.OPENAI})\n") == [1]
    assert checks("ok = spec.family == 'anthropic'\n") == [1]
    assert checks("ok = 'google' != m.family\n") == [1]
    assert checks("ok = judge_family != maker_family\nok = UsageEvent.family == 'machine'\n") == []
    assert checks("ok = features(spec.protocol).chat\n") == []


def test_the_law_bites_on_a_planted_check_outside_providers() -> None:
    planted = "def clears(protocol: str) -> bool:\n    return protocol == Protocol.ANTHROPIC\n"
    found = {**population(), "ai/agents/mounts.py": checks(planted)}
    assert strays(found) == ["prismoid/ai/agents/mounts.py:[2]"]
