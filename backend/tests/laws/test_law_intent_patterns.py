# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Intent is the model's; code decides facts and policy, never meaning (root CLAUDE.md, "Talk until it is
work"). A regex or a keyword list over what the user typed is a rule that misfires on the next phrasing,
and each one is a brute-force case fix; the number of them may only fall.

Population: every module under the routing roots (the agents, the turn, planning and chat
operations), found by walking the tree; `ai/verification` sits outside it, as it reads deliverables, not intent.
A text pattern is, by AST: a call into `re` or `regex` (also through `getattr` or a
star import), a keyword probe of user text (`"fix" in goal`, `goal.lower() in GREETINGS`,
`goal.startswith(("hi", "hello"))`), or the same probe in a comprehension (`any(w in goal for w in
WORDS)`). User text is a name in `USER_TEXT`, or a local name a function assigns from user text
(`q = goal.lower()`, `words = set(goal.split())`). `intent_patterns.json` holds each module's count
today; a module may not exceed it. The baseline must equal the tree, so a deleted pattern is recorded the
day it goes. Raising a number is never the fix.

The new homes of the Lead (`ZERO_HOMES`) are strict: no import of `re`, `regex` or `fnmatch`, and no
literal word test of any name (`"fix" in request`, `kind.endswith(("a", "b"))`), because a rename
must not be the way past this law. A file a routing root names by its own path is counted against the
baseline instead (the run guard, `capabilities/loops.py`).

The count is per module, so a swap (one pattern deleted, another added) leaves it unchanged; review sees
swaps, this law does not.

Facts are not intent: URLs for egress anchoring, the routine payload fence, length caps and dials are read
by code that lives outside these roots. `uv run python -m tests.laws.test_law_intent_patterns` rewrites
the baseline downward and refuses growth.
"""

from __future__ import annotations

import ast
import json
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.support.paths import BACKEND

BASELINE = Path(__file__).with_name("intent_patterns.json")

# Paths under backend/, as globs: where the turn's meaning is decided.
ROUTING_ROOTS = (
    "prismoid/ai/agents",
    "prismoid/operations/planning*.py",
    "prismoid/operations/turn_*.py",
    "prismoid/operations/task_door.py",
    "prismoid/operations/early_boot.py",
    "prismoid/operations/connector_attach.py",
    "prismoid/operations/chat*.py",
    # The run guard reads tool results and page refs; its count is carried here, never in a strict home.
    "prismoid/ai/capabilities/loops.py",
)
# Homes that start at zero and stay there; a glob with no match passes until the home exists.
ZERO_HOMES = (
    "prismoid/ai/agents/run.py",
    "prismoid/ai/agents/cancellation.py",
    "prismoid/ai/agents/lead.py",
    "prismoid/ai/capabilities",
    "prismoid/ai/rules",
    "prismoid/ai/context",
    "prismoid/operations/turn",
    "prismoid/operations/recipes",
)

RE_CALLS = frozenset(
    {"compile", "match", "search", "fullmatch", "findall", "finditer", "sub", "subn", "split"}
)
RE_MODULES = frozenset({"re", "regex"})
# What a strict home may not import at all: every stdlib or common way to match text by pattern.
PATTERN_MODULES = RE_MODULES | {"fnmatch"}
# What the user typed, by the names code gives it.
USER_TEXT = frozenset(
    {"goal", "message", "text", "ask", "prompt", "line", "paste", "body", "turn", "content"}
)
NORMALISERS = frozenset({"lower", "casefold", "strip", "split"})
# Calls that keep user text user text: `set(goal.split())` is still the goal's words.
WRAPPERS = frozenset({"set", "frozenset", "list", "tuple", "sorted"})
PREFIX_TESTS = frozenset({"startswith", "endswith"})

HOW_TO_FIX = (
    "Intent is the model's: say the rule in a prompt or a tool description, or add an eval row. A fact "
    "for policy (a URL, a size, a dial) is read by code outside the routing roots. Never raise the baseline."
)


def _user_text(node: ast.expr, tainted: frozenset[str] = frozenset()) -> bool:
    """Is `node` user text: a user-text name or a name assigned from one, maybe normalised or wrapped?"""
    while isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute) and node.func.attr in NORMALISERS:
            node = node.func.value
        elif isinstance(node.func, ast.Name) and node.func.id in WRAPPERS and len(node.args) == 1:
            node = node.args[0]
        else:
            return False
    return (isinstance(node, ast.Name) and (node.id in USER_TEXT or node.id in tainted)) or (
        isinstance(node, ast.Attribute) and node.attr in USER_TEXT
    )


def _word(node: ast.expr) -> bool:
    """A string literal with two letters in it: a word to look for, not a mark or a one-letter tag."""
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and sum(c.isalpha() for c in node.value) >= 2
    )


def _word_list(node: ast.expr) -> bool:
    """A literal collection of words, or a CONSTANT that holds one."""
    return isinstance(node, ast.Set | ast.Tuple | ast.List) or (
        isinstance(node, ast.Name) and node.id.isupper()
    )


def _literal_words(node: ast.expr) -> bool:
    """A word literal, or a literal collection holding one: what a strict home may not test text against."""
    return _word(node) or (
        isinstance(node, ast.Set | ast.Tuple | ast.List) and any(_word(item) for item in node.elts)
    )


def _prefix_test(node: ast.Call) -> ast.expr | None:
    """The receiver of a `.startswith`/`.endswith` call with a word argument, else None."""
    func = node.func
    if not (isinstance(func, ast.Attribute) and func.attr in PREFIX_TESTS and node.args):
        return None
    return func.value if _word(node.args[0]) or _word_list(node.args[0]) else None


def _assigned(node: ast.AST) -> tuple[list[ast.expr], ast.expr | None]:
    """The targets and the value of an assignment; no targets for anything else."""
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, ast.AnnAssign):
        return [node.target], node.value
    return [], None


def _tainted(function: ast.FunctionDef | ast.AsyncFunctionDef, outer: frozenset[str]) -> frozenset[str]:
    """The local names a function assigns from user text, followed to a fixed point."""
    names = frozenset(outer)
    while True:
        grown = set(names)
        for node in ast.walk(function):
            targets, value = _assigned(node)
            if value is not None and _user_text(value, names):
                grown |= {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        if len(grown) == len(names):
            return names
        names = frozenset(grown)


class _Patterns(ast.NodeVisitor):
    def __init__(self, tree: ast.Module) -> None:
        self.count = 0
        self.re_modules: set[str] = set()
        self.re_functions: set[str] = set()
        self.loop_names: set[str] = set()
        self.tainted: frozenset[str] = frozenset()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                self.re_modules |= {a.asname or a.name for a in node.names if a.name in RE_MODULES}
            elif isinstance(node, ast.ImportFrom) and node.module in RE_MODULES:
                star = any(a.name == "*" for a in node.names)
                self.re_functions |= (
                    RE_CALLS if star else {a.asname or a.name for a in node.names if a.name in RE_CALLS}
                )
            elif isinstance(node, ast.comprehension):
                self.loop_names |= {n.id for n in ast.walk(node.target) if isinstance(n, ast.Name)}
        self.visit(tree)

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        outer = self.tainted
        self.tainted = _tainted(node, outer)
        self.generic_visit(node)
        self.tainted = outer

    visit_FunctionDef = _function
    visit_AsyncFunctionDef = _function

    def _re_module(self, node: ast.expr) -> bool:
        return isinstance(node, ast.Name) and node.id in self.re_modules

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        receiver = _prefix_test(node)
        if (
            (isinstance(func, ast.Name) and func.id in self.re_functions)
            or (isinstance(func, ast.Attribute) and func.attr in RE_CALLS and self._re_module(func.value))
            or (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and node.args
                and self._re_module(node.args[0])
            )
            or (receiver is not None and _user_text(receiver, self.tainted))
        ):
            self.count += 1  # re.search(...), getattr(re, "search"), goal.startswith(("hi", "hello"))
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        if len(node.ops) == 1 and isinstance(node.ops[0], ast.In | ast.NotIn):
            left, right = node.left, node.comparators[0]
            loop_var = isinstance(left, ast.Name) and left.id in self.loop_names
            if _user_text(right, self.tainted) and (_word(left) or loop_var):
                self.count += 1  # "fix" in goal, any(w in goal for w in WORDS)
            elif _user_text(left, self.tainted) and _word_list(right):
                self.count += 1  # goal.lower() in GREETINGS
        self.generic_visit(node)


def pattern_uses(source: str) -> int:
    """How many text patterns `source` holds."""
    return _Patterns(ast.parse(source)).count


def imports_a_pattern_module(source: str) -> bool:
    """Does `source` import `re`, `regex` or `fnmatch` in any form?"""
    return any(
        (isinstance(n, ast.Import) and any(a.name.partition(".")[0] in PATTERN_MODULES for a in n.names))
        or (isinstance(n, ast.ImportFrom) and (n.module or "").partition(".")[0] in PATTERN_MODULES)
        for n in ast.walk(ast.parse(source))
    )


def literal_word_tests(source: str) -> int:
    """How many tests of any name against a word literal `source` holds: the strict homes' measure."""
    found = 0
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Compare) and any(isinstance(op, ast.In | ast.NotIn) for op in node.ops):
            found += any(_literal_words(side) for side in (node.left, *node.comparators))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in PREFIX_TESTS
        ):
            found += any(_literal_words(arg) for arg in node.args)
    return found


def _files(root: Path, globs: tuple[str, ...]) -> Iterator[Path]:
    for pattern in globs:
        for path in sorted(root.glob(pattern)):
            yield from sorted(path.rglob("*.py")) if path.is_dir() else [path]


def modules(root: Path) -> list[Path]:
    """The routing population: every module under the roots."""
    return sorted(set(_files(root, ROUTING_ROOTS)))


def counts(root: Path) -> dict[str, int]:
    """Every routing module that holds a pattern, with how many."""
    found = {
        p.relative_to(root).as_posix(): pattern_uses(p.read_text(encoding="utf-8")) for p in modules(root)
    }
    return {name: n for name, n in found.items() if n}


def problems(root: Path, baseline: dict[str, int]) -> list[str]:
    """Where the tree under `root` holds more text patterns than the baseline allows."""
    out = [
        f"{name} holds {n} text patterns, baseline {baseline.get(name, 0)}. {HOW_TO_FIX}"
        for name, n in sorted(counts(root).items())
        if n > baseline.get(name, 0)
    ]
    for path in _files(root, ZERO_HOMES):
        name, source = path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")
        if name in ROUTING_ROOTS:
            continue
        if imports_a_pattern_module(source) or pattern_uses(source) or literal_word_tests(source):
            out.append(
                f"{name} is a new home: no `re`, `regex` or `fnmatch` import and no literal word test. {HOW_TO_FIX}"
            )
        if name in baseline:
            out.append(f"{name} is a new home but the baseline lists it. Remove the line and the patterns.")
    return out


def test_no_module_grows_its_intent_patterns() -> None:
    found = problems(BACKEND, json.loads(BASELINE.read_text()))
    assert not found, "\n".join(found)


def test_the_baseline_is_tight() -> None:
    """A deleted pattern must be recorded, or it could be quietly replaced by a new one."""
    assert json.loads(BASELINE.read_text()) == counts(BACKEND), (
        "intent_patterns.json is not today's counts: record the shrink with "
        "`uv run python -m tests.laws.test_law_intent_patterns`."
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("import re\nre.compile('x')", 1),
        ("import re as r\nr.search('x', y)\nr.sub('a', 'b', y)", 2),
        ("from re import compile as c\nc('x')", 1),
        ("if 'fix' in goal: ...", 1),
        ("if 'fix' not in self.message.lower(): ...", 1),
        ("if goal.strip().lower() in GREETINGS: ...", 1),
        ("if message in ('hi', 'hello'): ...", 1),
        ("any(w in goal for w in WORDS)", 1),
        # The evasions a rename or a spelling once slipped past.
        ("import re\ngetattr(re, 'search')('x', goal)", 1),
        ("import regex\nregex.search('x', goal)", 1),
        ("from re import *\nsearch('x', goal)", 1),
        ("goal.startswith(('hi', 'hello'))", 1),
        ("message.lower().endswith(SIGNOFFS)", 1),
        ("def f(goal):\n    q = goal.lower()\n    return 'fix' in q", 1),
        ("def f(goal):\n    words = set(goal.split())\n    return 'fix' in words", 1),
        ("def f(goal):\n    q: str = goal.strip()\n    w = q.split()\n    return w in WORDS", 1),
        # Facts and plain code are not intent.
        ("if '?' in text: ...", 0),
        ("if kind in ('pdf', 'docx'): ...", 0),
        ("if 'fix' in other: ...", 0),
        ("if name in text: ...", 0),
        ("import re\nre.escape('x')", 0),
        ("m = pattern.search(x)", 0),
        ("path.endswith('.pdf')", 0),
        ("line.startswith('L ')", 0),
        ("def f(other):\n    q = other.lower()\n    return 'fix' in q", 0),
        ("def f(goal):\n    n = len(goal)\n    return 'fix' in n", 0),
    ],
)
def test_what_counts_as_a_text_pattern(source: str, expected: int) -> None:
    assert pattern_uses(source) == expected


def test_the_law_bites_on_a_planted_pattern(tmp_path: Path) -> None:
    routing = tmp_path / "prismoid" / "operations"
    routing.mkdir(parents=True)
    (routing / "chat_rules.py").write_text("import re\nBUY = re.compile('buy')\n")

    assert problems(tmp_path, {})
    assert problems(tmp_path, {"prismoid/operations/chat_rules.py": 1}) == []  # known, not grown
    (routing / "chat_rules.py").write_text("import re\nBUY = re.compile('buy')\nSELL = re.compile('sell')\n")
    assert problems(tmp_path, {"prismoid/operations/chat_rules.py": 1})  # grown past its baseline


def test_the_law_bites_in_a_new_home(tmp_path: Path) -> None:
    lead = tmp_path / "prismoid" / "ai" / "agents" / "lead.py"
    lead.parent.mkdir(parents=True)
    lead.write_text("import re\n")

    found = problems(tmp_path, {})
    assert len(found) == 1 and "prismoid/ai/agents/lead.py" in found[0]
    assert problems(tmp_path, {"prismoid/ai/agents/lead.py": 0})  # a listed new home is refused too


@pytest.mark.parametrize(
    "source",
    [
        "import regex\n",
        "import fnmatch\n",
        "from fnmatch import fnmatch\n",
        "from re import *\n",
        "if 'fix' in request: ...",
        "if request.lower() not in {'hi', 'hello'}: ...",
        "request.startswith('hi')",
        "kind.endswith(('yes', 'ok'))",
    ],
)
def test_a_new_home_refuses_any_pattern_module_and_any_literal_word_test(tmp_path: Path, source: str) -> None:
    home = tmp_path / "prismoid" / "ai" / "context" / "turns.py"
    home.parent.mkdir(parents=True)
    home.write_text(source)
    assert problems(tmp_path, {}), source
    routing = tmp_path / "prismoid" / "operations" / "chat_plain.py"
    routing.parent.mkdir(parents=True)
    routing.write_text("if '?' in request: ...\nif path.endswith('.pdf'): ...\n")
    assert all("prismoid/operations/chat_plain.py" not in line for line in problems(tmp_path, {}))


def ratchet() -> int:
    """Rewrite the baseline to today's counts; refuse any count that grew."""
    now = counts(BACKEND)
    held: dict[str, int] = json.loads(BASELINE.read_text()) if BASELINE.exists() else now
    grew = {name: n for name, n in now.items() if n > held.get(name, 0)}
    if grew:
        print(f"refusing to raise {grew}. {HOW_TO_FIX}")
        return 1
    BASELINE.write_text(json.dumps(now, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(ratchet())
