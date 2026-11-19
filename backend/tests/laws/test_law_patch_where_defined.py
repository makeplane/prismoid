# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Patch where the name is looked up: a test never monkeypatches a re-export nothing reads (ADR-075).

Protects every test that stubs a seam. `monkeypatch.setattr(pkg, "f", fake)` rebinds `pkg.f` only;
when `pkg` merely re-exports `f` (`from pkg.impl import f`) and the code under test calls `f` from
`pkg.impl` (or its own `from pkg.impl import f`), the fake is never called and the test passes
while testing nothing.

The population is every `<x>.setattr(<module>, "<name>", ...)` and `<x>.setattr("<module>.<name>",
...)` under `backend/tests`, where `<module>` resolves through the test file's imports to a `prismoid.*`
module. Such a patch is fine when the module binds `<name>` itself (def, class, assignment, plain
`import`), or reads it in its own code, or some `prismoid` module reads it as `<module>.<name>` (a
package facade such as `machines.acquire`). It is a miss when the module binds `<name>` only by
`from ... import` and nothing reads it there: patch the module that looks it up instead.
Known gap: one facade reader clears a patch even if another caller imports `<name>` straight from
the submodule, where the fake does not reach.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from tests.support.paths import BACKEND

Reads = set[tuple[str, str]]


@dataclass(frozen=True)
class Module:
    own: frozenset[str]  # bound by def, class, assignment or plain `import` at module level
    imported: dict[str, str]  # name -> the module it was `from`-imported from
    reads: frozenset[str]  # every name the module's own code loads


def module_level(body: list[ast.stmt]) -> list[ast.stmt]:
    """Module-level statements, descending into `if`/`try`/`with` (TYPE_CHECKING, ImportError)."""
    out: list[ast.stmt] = []
    for stmt in body:
        out.append(stmt)
        if isinstance(stmt, ast.If):
            out += module_level(stmt.body + stmt.orelse)
        elif isinstance(stmt, ast.With):
            out += module_level(stmt.body)
        elif isinstance(stmt, ast.Try):
            out += module_level(stmt.body + stmt.orelse + stmt.finalbody)
            for handler in stmt.handlers:
                out += module_level(handler.body)
    return out


def summarize(tree: ast.Module) -> Module:
    own: set[str] = set()
    imported: dict[str, str] = {}
    for stmt in module_level(tree.body):
        if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            own.add(stmt.name)
        elif isinstance(stmt, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
            own |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        elif isinstance(stmt, ast.Import):
            own |= {a.asname or a.name.split(".")[0] for a in stmt.names}
        elif isinstance(stmt, ast.ImportFrom):
            imported |= {a.asname or a.name: stmt.module or "." for a in stmt.names if a.name != "*"}
    reads = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return Module(frozenset(own), {k: v for k, v in imported.items() if k not in own}, frozenset(reads))


def import_map(tree: ast.Module, package: str = "") -> dict[str, str]:
    """Local name -> dotted path, from every import in the file (function-local ones included)."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names[a.asname or a.name.split(".")[0]] = a.name if a.asname else a.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parent = package.rsplit(".", node.level - 1)[0]
                base = f"{parent}.{base}" if base else parent
            for a in node.names:
                names[a.asname or a.name] = f"{base}.{a.name}"
    return names


def dotted(expr: ast.expr, names: dict[str, str]) -> str | None:
    attrs: list[str] = []
    while isinstance(expr, ast.Attribute):
        attrs.append(expr.attr)
        expr = expr.value
    if not isinstance(expr, ast.Name) or expr.id not in names:
        return None
    return ".".join([names[expr.id], *reversed(attrs)])


def facade_reads(tree: ast.Module, names: dict[str, str], modules: dict[str, Module]) -> Reads:
    """Every `<module>.<name>` the file reads, where `<module>` resolves to a known module."""
    found: Reads = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and (target := dotted(node.value, names)) in modules:
            found.add((str(target), node.attr))
    return found


def patch_targets(tree: ast.Module) -> list[tuple[int, str, str]]:
    """(line, module, name) for every `<x>.setattr(...)` in a test file (any module)."""
    names = import_map(tree)
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "setattr" or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            module, _, name = first.value.rpartition(".")
            found.append((node.lineno, module, name))
        elif (
            len(node.args) > 1
            and isinstance(second := node.args[1], ast.Constant)
            and isinstance(second.value, str)
            and (module := dotted(first, names))
        ):
            found.append((node.lineno, module, second.value))
    return found


def misses(tree: ast.Module, modules: dict[str, Module], reads: Reads) -> list[str]:
    """`line: why` for every patch in a test file that rebinds a re-export nothing reads."""
    out: list[str] = []
    for line, module, name in patch_targets(tree):
        target = modules.get(module)
        if target is None or name not in target.imported or name in target.reads:
            continue
        if (module, name) not in reads:
            source = target.imported[name]
            out.append(f"{line}: {module}.{name} only re-exports {source}.{name}; patch where it is read")
    return out


def prismoid_modules() -> tuple[dict[str, Module], Reads]:
    trees: dict[str, tuple[ast.Module, str]] = {}
    for path in sorted((BACKEND / "prismoid").rglob("*.py")):
        parts = path.relative_to(BACKEND).with_suffix("").parts
        is_package = parts[-1] == "__init__"
        name = ".".join(parts[:-1] if is_package else parts)
        trees[name] = (ast.parse(path.read_text()), name if is_package else name.rpartition(".")[0])
    modules = {name: summarize(tree) for name, (tree, _) in trees.items()}
    reads: Reads = set()
    for tree, package in trees.values():
        reads |= facade_reads(tree, import_map(tree, package), modules)
    return modules, reads


def test_tests_patch_where_the_name_is_read() -> None:
    modules, reads = prismoid_modules()
    found = [
        f"{path.relative_to(BACKEND)}:{miss}"
        for path in sorted((BACKEND / "tests").rglob("*.py"))
        for miss in misses(ast.parse(path.read_text()), modules, reads)
    ]
    assert not found, "the fake is never called:\n" + "\n".join(found)


def test_the_law_bites() -> None:
    modules = {
        "prismoid.pkg": summarize(
            ast.parse("from prismoid.pkg.impl import f, g\nfrom os import path\n__all__ = ['f']\n")
        ),
        "prismoid.pkg.impl": summarize(ast.parse("def f() -> None: ...\ndef g() -> None: ...\n")),
        "prismoid.user": summarize(
            ast.parse("from prismoid.pkg.impl import f\ndef run() -> None:\n    f()\n")
        ),
    }
    facade = facade_reads(ast.parse("from prismoid import pkg\npkg.g()\n"), {"pkg": "prismoid.pkg"}, modules)
    test = ast.parse(
        "from prismoid import pkg, user\n"
        "from prismoid.pkg import impl\n"
        "def test(monkeypatch):\n"
        "    monkeypatch.setattr(pkg, 'f', None)\n"  # 4: a re-export nothing reads - a miss
        "    monkeypatch.setattr('prismoid.pkg.f', None)\n"  # 5: the same, string form - a miss
        "    monkeypatch.setattr(pkg, 'g', None)\n"  # 6: read as pkg.g - fine
        "    monkeypatch.setattr(impl, 'f', None)\n"  # 7: where it is defined - fine
        "    monkeypatch.setattr(user, 'f', None)\n"  # 8: where it is read - fine
        "    monkeypatch.setattr('prismoid.pkg.path.join', None)\n"  # 9: an object's attribute - fine
    )
    assert [m.split(":")[0] for m in misses(test, modules, facade)] == ["4", "5"]
