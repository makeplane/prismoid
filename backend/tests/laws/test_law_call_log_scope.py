# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Only a step attempt runs on the call log (ADR-089): `CallLogScope.open(` is called only under
`operations/execution/`, and the scope is entered (`active(`) only by `AgentRunner.run`, so no other
run (a chat reply, a plan, a judge, a single-call agent) writes a row or replays one. The population is every `.py` file
under `prismoid/`, read as an AST: each call of `CallLogScope.open` and of `durability.active` or a name
imported from it."""

from __future__ import annotations

import ast
import functools

from tests.support.paths import BACKEND

PRISMOID = BACKEND / "prismoid"
OPENERS = "operations/execution/"
ENTERER = "ai/agents/run.py"


def calls(source: str) -> dict[str, list[int]]:
    """The lines that open a scope (`open`) and enter one (`active`) in one module."""
    tree = ast.parse(source)
    actives = {"active"} & {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("capabilities.durability")
        for alias in node.names
    }
    found: dict[str, list[int]] = {"open": [], "active": []}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "open" and _named(func.value, "CallLogScope"):
            found["open"].append(node.lineno)
        elif (
            isinstance(func, ast.Attribute) and func.attr == "active" and _named(func.value, "durability")
        ) or (isinstance(func, ast.Name) and func.id in actives):
            found["active"].append(node.lineno)
    return found


def _named(node: ast.expr, name: str) -> bool:
    return (isinstance(node, ast.Name) and node.id == name) or (
        isinstance(node, ast.Attribute) and node.attr == name
    )


@functools.cache
def population() -> dict[str, dict[str, list[int]]]:
    return {
        path.relative_to(PRISMOID).as_posix(): calls(path.read_text())
        for path in sorted(PRISMOID.rglob("*.py"))
    }


def strays(found: dict[str, dict[str, list[int]]]) -> list[str]:
    """The modules outside a step attempt's home that open a scope."""
    return [f"prismoid/{p}:{c['open']}" for p, c in found.items() if c["open"] and not p.startswith(OPENERS)]


def test_only_a_step_attempt_opens_a_scope() -> None:
    stray = strays(population())
    assert not stray, f"only operations/execution opens a call-log scope: {stray}"
    assert any(c["open"] for p, c in population().items() if p.startswith(OPENERS)), "the law watches nothing"


def test_only_the_runner_enters_one() -> None:
    entered = sorted(p for p, c in population().items() if c["active"])
    assert entered == [ENTERER], f"only AgentRunner.run enters a call-log scope: {entered}"


def test_the_law_bites() -> None:
    assert calls("s = await CallLogScope.open(log, step, fresh=True)\n")["open"] == [1]
    assert calls("s = await durability.CallLogScope.open(log, step, fresh=True)\n")["open"] == [1]
    assert calls("with durability.active(s):\n    pass\n")["active"] == [1]
    assert calls("from prismoid.ai.capabilities.durability import active\nwith active(s):\n    pass\n")[
        "active"
    ] == [2]
    assert calls("with open(path):\n    pass\n") == {"open": [], "active": []}


def test_the_law_bites_on_a_chat_reply_that_opens_a_scope() -> None:
    planted = "scope = await CallLogScope.open(PgCallLog(ws), step, fresh=True)\nawait runner.run(brief, log=scope)\n"
    found = {**population(), "operations/chat_reply.py": calls(planted)}
    assert strays(found) == ["prismoid/operations/chat_reply.py:[1]"]
