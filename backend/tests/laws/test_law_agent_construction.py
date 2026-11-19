# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One Agent constructor and one run loop: pydantic-ai's `Agent(` is called only where ALLOWED says, and an
agent is run (`.run(`, `.iter(`, `.run_stream(`...) only where RUN_ALLOWED says (ADR-075).

Protects the root CLAUDE.md "one place per kind": every agent, a bot included, is an
`AgentDef` that `ai/agents/base.py` compiles, so the charter, the granted tools, the steering
capability and the usage meter ride every run. A second constructor is an agent none of that reaches.
The population is every `.py` file under `prismoid/`, read as an AST: the names a module binds to
`pydantic_ai.Agent` (`from pydantic_ai import Agent [as X]`, or `pydantic_ai.Agent` through a module
import), plus one level of re-export inside `prismoid` (a name imported from a `prismoid` module whose own
source binds it to pydantic-ai's `Agent`, or that module's attribute), then every call of one of them.
A run is a run-family method called on a receiver named for an agent (`agent`, `self._agent`, `runner.agent`):
`ai/agents/run.py`'s `AgentRunner` is the one place, so metering, the tap and failure translation ride every run.
"""

from __future__ import annotations

import ast
import functools
from collections.abc import Callable

from tests.support.paths import BACKEND

PRISMOID = BACKEND / "prismoid"

ALLOWED = {
    "ai/agents/base.py": "the constructor: every AgentDef compiles here (ADR-045)",
    "ai/gateway/conformance.py": "the model drill: probes one catalog model before anyone enables it",
    "runner/dev.py": "the dev ping drill: one bare call per capability, no task, no AgentDef",
}

RUN_ALLOWED = {
    "ai/agents/run.py": "the one run loop: `AgentRunner.run`, what `build()` returns",
    "ai/gateway/conformance.py": ALLOWED["ai/gateway/conformance.py"],
    "runner/dev.py": ALLOWED["runner/dev.py"],
}

PYDANTIC_AI = ("pydantic_ai", "pydantic_ai.agent")
RUN_METHODS = frozenset({"run", "run_sync", "run_stream", "run_stream_events", "iter"})


@functools.cache
def prismoid_source(module: str) -> str | None:
    """The source of a `prismoid.*` module (a file or a package's `__init__`), or None."""
    base = PRISMOID.parent.joinpath(*module.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path.read_text()
    return None


def _pydantic_agent_names(tree: ast.AST) -> set[str]:
    """The names a module binds to pydantic-ai's `Agent` with `from pydantic_ai import Agent [as X]`."""
    return {
        a.asname or a.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module in PYDANTIC_AI
        for a in node.names
        if a.name == "Agent"
    }


@functools.cache
def _agent_names_in(source: str) -> frozenset[str]:
    return frozenset(_pydantic_agent_names(ast.parse(source)))


def _absolute(node: ast.ImportFrom, package: str) -> str:
    if not node.level:
        return node.module or ""
    parts = package.split(".")[: len(package.split(".")) - (node.level - 1)]
    return ".".join([*parts, *([node.module] if node.module else [])])


def agent_constructions(
    source: str, *, package: str = "", resolve: Callable[[str], str | None] = prismoid_source
) -> list[int]:
    """The line of every call that constructs a pydantic-ai `Agent` in `source`.

    `package` is the importing module's package (for relative imports); `resolve` reads a `prismoid`
    module's source, so a re-export one level deep is still seen.
    """
    tree = ast.parse(source)
    names = _pydantic_agent_names(tree)
    modules: dict[str, set[str]] = {}  # a local module alias -> its attributes that are `Agent`

    def reexports(module: str) -> set[str]:
        if module != "prismoid" and not module.startswith("prismoid."):
            return set()
        found = resolve(module)
        return set(_agent_names_in(found)) if found is not None else set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = _absolute(node, package)
            for a in node.names:
                if a.name in reexports(module):  # pydantic_ai itself is in `names` already
                    names.add(a.asname or a.name)
                elif attrs := reexports(f"{module}.{a.name}"):
                    modules[a.asname or a.name] = attrs
        elif isinstance(node, ast.Import):
            for a in node.names:
                attrs = {"Agent"} if a.name in PYDANTIC_AI else reexports(a.name)
                if attrs:
                    modules[a.asname or a.name] = attrs

    def constructs(func: ast.expr) -> bool:
        if isinstance(func, ast.Subscript):  # `Agent[Deps, Out](...)`
            func = func.value
        if isinstance(func, ast.Name):
            return func.id in names
        if not isinstance(func, ast.Attribute):
            return False
        return func.attr in modules.get(ast.unparse(func.value), set())

    return [node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call) and constructs(node.func)]


def _receiver_name(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else ""


def agent_runs(source: str) -> list[int]:
    """The line of every run-family call on a receiver named for an agent in `source`."""
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in RUN_METHODS
        and "agent" in _receiver_name(node.func.value).lower()
    ]


@functools.cache
def runs() -> dict[str, list[int]]:
    return {
        path.relative_to(PRISMOID).as_posix(): lines
        for path in sorted(PRISMOID.rglob("*.py"))
        if (lines := agent_runs(path.read_text()))
    }


@functools.cache
def constructors() -> dict[str, list[int]]:
    found: dict[str, list[int]] = {}
    for path in sorted(PRISMOID.rglob("*.py")):
        package = ".".join(path.parent.relative_to(PRISMOID.parent).parts)
        if lines := agent_constructions(path.read_text(), package=package):
            found[path.relative_to(PRISMOID).as_posix()] = lines
    return found


def test_agent_is_constructed_only_where_allowed() -> None:
    stray = {f"prismoid/{p}:{lines}" for p, lines in constructors().items() if p not in ALLOWED}
    assert not stray, f"compile an AgentDef through ai/agents/base.py instead: {sorted(stray)}"


def test_the_allowlist_is_not_stale() -> None:
    stale = set(ALLOWED) - set(constructors())
    assert not stale, f"no longer constructs an Agent; drop it from ALLOWED: {sorted(stale)}"


def test_an_agent_runs_only_in_the_run_loop() -> None:
    stray = {f"prismoid/{p}:{lines}" for p, lines in runs().items() if p not in RUN_ALLOWED}
    assert not stray, f"run through `build(...).run(prompt)` (ai/agents/run.py) instead: {sorted(stray)}"


def test_the_run_allowlist_is_not_stale() -> None:
    stale = set(RUN_ALLOWED) - set(runs())
    assert not stale, f"no longer runs an agent; drop it from RUN_ALLOWED: {sorted(stale)}"


def test_the_run_law_bites() -> None:
    assert agent_runs("r = await agent.run('go')\n") == [1]
    assert agent_runs("r = await self.agent.run(deps=d)\n") == [1]
    assert agent_runs("r = await runner.agent.run_stream('go')\n") == [1]
    assert agent_runs("async with self._agent.iter('go') as it: ...\n") == [1]
    assert agent_runs("r = judge_agent.run_sync('go')\n") == [1]
    assert agent_runs("r = await runner.run('go')\nr = await other.run(t, go)\n") == []
    assert agent_runs("for a in tree.iter('a'): ...\n") == []


def test_the_law_bites() -> None:
    assert agent_constructions("from pydantic_ai import Agent\na = Agent('m')\n") == [2]
    assert agent_constructions("from pydantic_ai import Agent\na = Agent[None, str]('m')\n") == [2]
    assert agent_constructions("import pydantic_ai\na = pydantic_ai.Agent[None, str]('m')\n") == [2]
    assert agent_constructions("from pydantic_ai import Agent as A\na = A('m')\n") == [2]
    assert agent_constructions("import pydantic_ai as pa\na = pa.Agent('m')\n") == [2]
    assert agent_constructions("from pydantic_ai import Agent\ndef f(a: Agent) -> None: ...\n") == []


def test_the_law_sees_one_level_of_re_export_inside_prismoid() -> None:
    sources = {
        "prismoid.x": "from pydantic_ai import Agent\n",
        "prismoid.y": "class Agent: ...\n",
        "prismoid.pkg.z": "from pydantic_ai import Agent as PA\n",
    }
    resolve = sources.get
    assert agent_constructions("from prismoid.x import Agent\na = Agent('m')\n", resolve=resolve) == [2]
    assert agent_constructions(
        "from prismoid.x import Agent as A\na = A[None, str]('m')\n", resolve=resolve
    ) == [2]
    assert agent_constructions("import prismoid.x as px\na = px.Agent('m')\n", resolve=resolve) == [2]
    assert agent_constructions("from prismoid import x\na = x.Agent('m')\n", resolve=resolve) == [2]
    assert agent_constructions("from prismoid.pkg.z import PA\na = PA('m')\n", resolve=resolve) == [2]
    assert agent_constructions(
        "from .z import PA\na = PA('m')\n", package="prismoid.pkg", resolve=resolve
    ) == [2]
    assert agent_constructions(
        "from ..x import Agent\na = Agent('m')\n", package="prismoid.pkg", resolve=resolve
    ) == [2]
    assert agent_constructions("from prismoid.y import Agent\na = Agent()\n", resolve=resolve) == []
    assert agent_constructions("from prismoid.pkg.z import Agent\na = Agent()\n", resolve=resolve) == []
    assert (
        agent_constructions("from prismoid.x import Agent\ndef f(a: Agent) -> None: ...\n", resolve=resolve)
        == []
    )
