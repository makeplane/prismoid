# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Every model seat reads the routing table: a call in `prismoid/operations/` that seats a model -
`ai.agents.base.build`, or `ai.gateway.resolve.chain_for` - passes `order=` (the
routing table's chains, `models.routed_catalog`), unless it is handed one row to run on (a literal
one-row catalog, or `pin=`). A `build` also passes `settings=` (the routing table's class settings,
`routing.settings_for`), one-row or not: a seat built without them is asked at provider defaults.

Protects the starter catalog's promise that no class is routed by alphabetical order:
the planner, the router (both since retired), memory_extract, QA, the judge and the image chain once built without the
table, so a chained Haiku 5.5 still lost to Haiku 4.5 because "4" sorts before "5". The population
is every `.py` under `prismoid/operations/`, read as an AST: the names each module binds to those
functions (directly, or as attributes of a module alias), then every call of one of them.
"""

from __future__ import annotations

import ast

from tests.support.paths import BACKEND

OPERATIONS = BACKEND / "prismoid" / "operations"

SEATERS = {"prismoid.ai.agents.base": {"build"}, "prismoid.ai.gateway.resolve": {"chain_for"}}


def _bindings(tree: ast.AST) -> tuple[set[str], dict[str, set[str]]]:
    """(names bound to a seater, module aliases -> the seaters reachable on them)."""
    names: set[str] = set()
    modules: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                if a.name in SEATERS.get(node.module, ()):
                    names.add(a.asname or a.name)
                full = f"{node.module}.{a.name}"
                if full in SEATERS:
                    modules[a.asname or a.name] = SEATERS[full]
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name in SEATERS and a.asname:
                    modules[a.asname] = SEATERS[a.name]
    return names, modules


def _seats(call: ast.Call, names: set[str], modules: dict[str, set[str]]) -> bool:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in names
    return (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.attr in modules.get(func.value.id, set())
    )


def _one_row(call: ast.Call) -> bool:
    keywords = {k.arg for k in call.keywords}
    catalog = call.args[2] if len(call.args) > 2 else None
    return "pin" in keywords or isinstance(catalog, ast.List)


def _unrouted(call: ast.Call) -> bool:
    keywords = {k.arg for k in call.keywords}
    func = call.func
    builds = (func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)) == "build"
    return ("order" not in keywords and not _one_row(call)) or (builds and "settings" not in keywords)


def unrouted_seats() -> list[str]:
    found: list[str] = []
    for path in sorted(OPERATIONS.rglob("*.py")):
        tree = ast.parse(path.read_text())
        names, modules = _bindings(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _seats(node, names, modules) and _unrouted(node):
                found.append(f"{path.relative_to(OPERATIONS.parent)}:{node.lineno}")
    return found


def test_every_model_seat_reads_the_routing_table() -> None:
    assert unrouted_seats() == []


def test_the_law_bites() -> None:
    tree = ast.parse(
        "from prismoid.ai.gateway.resolve import chain_for\n"
        "from prismoid.ai.agents import base as agents_base\n"
        "chain_for(c, catalog)\n"
        "agents_base.build('namer', deps, catalog)\n"
        "agents_base.build('judge', deps, [spec], settings=s)\n"
        "chain_for(c, catalog, order=o)\n"
        "agents_base.build('namer', deps, catalog, order=o)\n"
        "agents_base.build('namer', deps, catalog, order=o, settings=s)\n"
    )
    names, modules = _bindings(tree)
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _seats(n, names, modules)]
    assert [c.lineno for c in calls if _unrouted(c)] == [3, 4, 7]
