# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Tests score outcomes, never which rule fired (the measurement program; `tests/CLAUDE.md`: behaviour
through public names). A test that reaches into a routing rule's private helper pins the mechanism a
later change deletes, so the change must rewrite the test to land and the test no longer says whether
the person got the right thing.

Population: every test module under `tests/`, read by AST, against the rule modules named in
`RULE_MODULES`: the turn's routing rules, its recipes and its route. The list is kept by hand because it
names rule modules, not a property code can derive. A test may not import an underscore
name from one, reach one through the module (`recipes._facts`, or the full path
`prismoid.operations.turn.recipes._facts`), name one in `setattr` / `patch.object`, or patch one by its dotted path. A module that no longer exists matches nothing, so deleting a rule never
needs this file edited. The contract tests (`tests/contract`) pin event shapes and are exempt.

Fix a failure by asserting what the user receives (a reply, a plan, a question, the calls and Machines it
took) through a public entry point, or by promoting the helper to a public name with an outcome to test.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

from tests.support.paths import TESTS

EXEMPT = "contract"

RULE_MODULES = (
    "prismoid.operations.turn.recipes",
    "prismoid.operations.turn.route",
)

HOW_TO_FIX = (
    "Assert the outcome the user gets (reply, plan, ask, the calls and Machines it took) through a "
    "public entry point, or promote the helper to a public name that has an outcome to test."
)


def _private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _bindings(tree: ast.Module) -> dict[str, str]:
    """The local names that stand for a rule module, by the module they stand for."""
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {a.asname: a.name for a in node.names if a.asname and a.name in RULE_MODULES}
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if f"{node.module}.{alias.name}" in RULE_MODULES:
                    bound[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return bound


def _dotted(node: ast.expr) -> str | None:
    """`a.b.c` for a chain of attributes on a name, else None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    return ".".join([node.id, *reversed(parts)]) if isinstance(node, ast.Name) else None


def _touched(node: ast.AST, bound: dict[str, str]) -> Iterator[str]:
    """The private rule names one AST node reaches, as `module._name`."""
    if isinstance(node, ast.ImportFrom) and node.module in RULE_MODULES:
        yield from (f"{node.module}.{a.name}" for a in node.names if _private(a.name))
    elif isinstance(node, ast.Attribute) and _private(node.attr):
        reached = _dotted(node.value)
        module = bound.get(reached or "", reached)
        if module in RULE_MODULES:
            yield f"{module}.{node.attr}"
    elif isinstance(node, ast.Call) and len(node.args) >= 2:
        # setattr(triage, "_x", ...) and patch.object(triage, "_x")
        target, name = node.args[0], node.args[1]
        named = isinstance(name, ast.Constant) and isinstance(name.value, str) and _private(name.value)
        if named and isinstance(target, ast.Name) and target.id in bound:
            yield f"{bound[target.id]}.{ast.literal_eval(name)}"
    elif isinstance(node, ast.Constant) and isinstance(node.value, str):
        # patch("prismoid.operations.turn.recipes._x")
        for module in RULE_MODULES:
            if node.value.startswith(f"{module}.") and _private(node.value.removeprefix(f"{module}.")):
                yield node.value


def private_uses(source: str) -> Iterator[str]:
    """Every private name of a rule module that `source` touches, as `module._name`."""
    if not any(module.rpartition(".")[2] in source for module in RULE_MODULES):
        return  # every way in names the module, so most files are skipped unparsed
    tree = ast.parse(source)
    bound = _bindings(tree)
    for node in ast.walk(tree):
        yield from _touched(node, bound)


def problems(tests: Path) -> list[str]:
    """Every test module under `tests` that touches a private rule name."""
    out: list[str] = []
    for path in sorted(tests.rglob("*.py")):
        if EXEMPT in path.relative_to(tests).parts:
            continue
        out += [
            f"{path.relative_to(tests.parent).as_posix()} uses {name}. {HOW_TO_FIX}"
            for name in sorted(set(private_uses(path.read_text(encoding="utf-8"))))
        ]
    return out


def test_no_test_pins_a_private_routing_rule() -> None:
    found = problems(TESTS)
    assert not found, "\n".join(found)


def test_the_law_bites_on_planted_tests(tmp_path: Path) -> None:
    planted = {
        "imports.py": "from prismoid.operations.turn.recipes import _facts\n",
        "attribute.py": "from prismoid.operations.turn import recipes\nassert recipes._RULES\n",
        "aliased.py": "import prismoid.operations.turn.route as ladder\nladder._pick()\n",
        "full_path.py": "import prismoid.operations.turn.recipes\nassert prismoid.operations.turn.recipes._facts\n",
        "patched.py": "def test(monkeypatch):\n    monkeypatch.setattr('prismoid.operations.turn.route._KINDS', {})\n",
        "setattr.py": "from prismoid.operations.turn import route\ndef test(mp):\n    mp.setattr(route, '_leads', ())\n",
    }
    for name, source in planted.items():
        (tmp_path / name).write_text(source)
    found = problems(tmp_path)
    assert len(found) == len(planted), found
    for name, module, private in [
        ("imports.py", "prismoid.operations.turn.recipes", "_facts"),
        ("attribute.py", "prismoid.operations.turn.recipes", "_RULES"),
        ("aliased.py", "prismoid.operations.turn.route", "_pick"),
        ("full_path.py", "prismoid.operations.turn.recipes", "_facts"),
        ("patched.py", "prismoid.operations.turn.route", "_KINDS"),
        ("setattr.py", "prismoid.operations.turn.route", "_leads"),
    ]:
        assert any(f"{name} uses {module}.{private}." in line for line in found), (name, found)


def test_public_names_and_other_modules_are_fine(tmp_path: Path) -> None:
    (tmp_path / "fine.py").write_text(
        "from prismoid.operations.turn.recipes import dial_lane, lane_of\n"
        "from prismoid.operations.turn import recipes\n"
        "from prismoid.operations.planning_brief import _page_readers\n"
        "assert recipes.lane_of and recipes.__name__\n"
    )
    assert problems(tmp_path) == []


def test_contract_tests_are_exempt(tmp_path: Path) -> None:
    (tmp_path / "contract").mkdir()
    (tmp_path / "contract" / "test_shapes.py").write_text(
        "from prismoid.operations.turn.recipes import _facts\n"
    )
    assert problems(tmp_path) == []
