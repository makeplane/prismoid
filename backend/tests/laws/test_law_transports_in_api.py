# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Transports live in api/: `starlette` and `fastapi` are imported only under `prismoid/api/` and
`prismoid/runner/`.

Protects the root CLAUDE.md layering ("views never touch agents; agents never contain business
logic; operations compose both"): HTTP and WebSocket speak belongs to the door, so an operation is
callable from a job, the CLI or a test without a request in hand. The population is every `.py` file
under `prismoid/` outside those two folders, read as an AST: any `import`/`from ... import` (TYPE_CHECKING
blocks and function-local imports included) whose top-level package is one of the two frameworks.
Client libraries such as `websockets` and `httpx` are not server frameworks and are not covered.
"""

from __future__ import annotations

import ast
import functools

from tests.support.paths import BACKEND

PRISMOID = BACKEND / "prismoid"

FRAMEWORKS = frozenset({"starlette", "fastapi"})
HOMES = ("api/", "runner/")

ALLOWED: dict[str, str] = {}


def framework_imports(source: str) -> list[int]:
    """The line of every import of a server framework in `source`."""
    lines: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            tops = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            tops = {node.module.split(".")[0]}
        else:
            continue
        if tops & FRAMEWORKS:
            lines.append(node.lineno)
    return lines


@functools.cache
def importers() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(PRISMOID.rglob("*.py")):
        rel = path.relative_to(PRISMOID).as_posix()
        if not rel.startswith(HOMES) and (lines := framework_imports(path.read_text())):
            found[rel] = lines
    return found


def test_frameworks_are_imported_only_in_the_doors() -> None:
    stray = {f"prismoid/{p}:{lines}" for p, lines in importers().items() if p not in ALLOWED}
    assert not stray, f"move the transport into api/ (or runner/) and pass plain values: {sorted(stray)}"


def test_the_allowlist_is_not_stale() -> None:
    stale = set(ALLOWED) - set(importers())
    assert not stale, f"no longer imports a server framework; drop it from ALLOWED: {sorted(stale)}"


def test_the_law_bites() -> None:
    assert framework_imports("from starlette.websockets import WebSocket\n") == [1]
    assert framework_imports("import fastapi\n") == [1]
    assert framework_imports("if TYPE_CHECKING:\n    from fastapi import Request\n") == [2]
    assert framework_imports("import websockets\nfrom httpx import AsyncClient\n") == []
