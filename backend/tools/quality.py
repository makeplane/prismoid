# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The quality ratchet (ADR-075): new code meets the caps; known offenders may only shrink.

`python tools/quality.py check` measures per-symbol size and complexity across backend, web and
sandbar, compares the offenders with the checked-in baseline (`tools/quality_baseline.json`) and
exits 1 on any symbol over its allowance. `ratchet` rewrites the baseline downward only (`--init`
seeds it with today's offenders verbatim); `report` prints the before/after table a PR quotes.

Allowance: a known key may not grow past its baseline value; a key the baseline lacks gets the cap,
unless it is a moved symbol (same text after `::`, its old key gone and its old symbol no longer in its
old file) or sits in a file git saw renamed (`git mv`, staged or committed since the merge-base with
main), which inherits the old value. `check` also fails while the baseline is not tight (`ratchet`
would change it: a move to re-key, an improvement to record): each PR that shrinks an offender
commits the tightened baseline. Ruff ignores `# noqa` for these metrics; oxlint cannot ignore its
disables, so a comment disabling a measured rule is itself a metric with a cap of 0. Keys ending
`::<anonymous>` or `::depth` hold the summed EXCESS of nameless oxlint diagnostics in a file, so their cap is 0; they and `::<module>` keys move only with a renamed file.

`py.local_imports` counts, per file, the `prismoid` imports written inside a function: each one is an
edge the import contracts and a reader cannot see, usually a cycle dodged instead of a rule moved to
a leaf. `release` is the pay-down gate run before a tag: each metric in `SHRINK_PER_RELEASE` must
have shed that share of its excess since the baseline at the last release tag.

A file's length is counted after its copyright header (`code_lines`, ADR-102): a legal notice is not
code, so heading a file moves no baseline entry and pushes no file over its cap.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import math
import re
import subprocess
import sys
import tempfile
import tokenize
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

Metrics = dict[str, dict[str, int]]

CAPS: dict[str, int] = {
    "py.complexity": 15,
    "py.statements": 80,
    "py.nesting": 3,
    "py.positional": 5,
    "py.file_lines": 800,
    "py.endpoint_lines": 20,
    "web.complexity": 15,
    "web.depth": 3,
    "web.function_lines": 300,
    "web.file_lines": 800,
    "web.metric_disables": 0,
    "go.file_lines": 600,
    "go.function_lines": 60,
    "comments.process_refs": 0,
    "tests.private_access": 0,
    "py.local_imports": 0,
}

# The share of a metric's excess each release must pay down, measured against the baseline at the
# previous release tag. A metric the old baseline does not hold has no target until a tag records it.
SHRINK_PER_RELEASE: dict[str, float] = {"py.local_imports": 0.10, "py.complexity": 0.05}

REPO = Path(__file__).resolve().parents[2]
BASELINE = REPO / "backend" / "tools" / "quality_baseline.json"

PY_EXCLUDE = ("backend/prismoid/db/migrations/", "backend/prismoid/ai/frames/image/house/")
RUFF_METRICS = {
    "C901": "py.complexity",
    "PLR0915": "py.statements",
    "PLR1702": "py.nesting",
    "PLR0917": "py.positional",
}
RUFF_CMD = [
    *("uv", "run", "ruff", "check", "prismoid", "--isolated", "--preview", "--ignore-noqa"),
    *("--select", ",".join(RUFF_METRICS)),
    *("--config", f"lint.mccabe.max-complexity={CAPS['py.complexity']}"),
    *("--config", f"lint.pylint.max-statements={CAPS['py.statements']}"),
    *("--config", f"lint.pylint.max-nested-blocks={CAPS['py.nesting']}"),
    *("--config", f"lint.pylint.max-positional-args={CAPS['py.positional']}"),
    *("--output-format", "json", "--exit-zero"),
]
OXLINT_METRICS = {
    "eslint(complexity)": "web.complexity",
    "eslint(max-depth)": "web.depth",
    "eslint(max-lines-per-function)": "web.function_lines",
}
OXLINT_CONFIG = {
    "plugins": [],
    "categories": {"correctness": "off"},
    "rules": {
        "complexity": ["warn", {"max": CAPS["web.complexity"]}],
        "max-depth": ["warn", {"max": CAPS["web.depth"]}],
        "max-lines-per-function": [
            "warn",
            {"max": CAPS["web.function_lines"], "skipBlankLines": True, "skipComments": True},
        ],
    },
}
WEB_ROOTS = ("web/app", "packages/blocks/src")
_DATE = r"20[0-9]{2}-[01][0-9]-[0-3][0-9x]"
# A comment says why the code is so; the incident, ticket or review round that taught it belongs in
# the commit. Caught: rulings and review rounds, an internal task or commit id (eight hex digits
# with both a digit and a letter), a wave or review id (W3, N14, P0-1), and a date wearing a story
# ("found live 2026-10-25", "e2e 2026-11-11", "2026-08-09 audit"). A bare date is data, not history.
PROCESS_REF = re.compile(
    r"ruling [A-Z]?[0-9]+|Review Focus|plan deviation|final review|fix wave|\bRR-[0-9]+|review [A-Z]-[0-9]+"
    r"|(?<![#\w])(?=[0-9a-f]{0,7}[0-9])(?=[0-9a-f]{0,7}[a-f])[0-9a-f]{8}\b"
    r"|\bW[0-9]{1,2}\b|\bN[0-9]{1,3}\b|\bP[0-9]-[0-9]+\b"
    rf"|(?i:\b(?:found|live|e2e|re-taken|taken|note of|record)),? (?:live,? )?{_DATE}"
    rf"|{_DATE} (?i:audit|review)"
)
COMMENT_OPEN = re.compile(r"(?:^|[\s{(;,])(//|/\*)")  # `{` reads JSX's `{/* ... */}`
OXLINT_VALUE = {
    "eslint(complexity)": re.compile(r"complexity of (\d+)"),
    "eslint(max-depth)": re.compile(r"nested too deeply \((\d+)\)"),
    "eslint(max-lines-per-function)": re.compile(r"too many lines \((\d+)\)"),
}
LINT_DISABLE = re.compile(r"(?:eslint|oxlint)-disable(?:-next-line|-line)?\b([^\n]*)")
PYRIGHT_LAX_MODE = re.compile(
    r"^\s*#\s*pyright:(?!\s*ignore\b)(?:[^\n]*,)?[ \t]*(?:basic|standard)\b", re.MULTILINE
)
# pyright honours a rule list only on `pyright: ignore`; `type: ignore[x]` silences the whole line
BLANKET_IGNORE = re.compile(r"#\s*(?:type:\s*ignore\b|pyright:\s*ignore\b(?!\s*\[))")
GO_FUNC = re.compile(r"^func\s+(?:\(([^)]*)\)\s*)?(\w+)")
NOTICE_LINE = re.compile(r"\s*(?:#|//|/\*|\*)")  # a line of the copyright header, in any comment syntax
WEB_TOKEN = re.compile(  # what `_web_stop` reads: a comment or a string first, so neither one counts
    r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`|[()\[\]{};]""",
    re.DOTALL,
)
WEB_MODIFIERS = r"(?:(?:async|static|get|set|public|private|protected|readonly|override)\s+)*"
WEB_GENERICS = r"(?:<[^(]*>\s*)?"
WEB_BODY = re.compile(r"\s*(?::\s*[^;(\n]*)?\{")  # after a parameter list: a return type, then `{`
WEB_ARROW = re.compile(r"\s*(?::[^=;\n]*)?=>")  # after a parameter list: a return type, then `=>`
WEB_FUNCTION = re.compile(rf"\s*(?:async\s+)?(?:function\b|\w+\s*=>|{WEB_GENERICS}(?P<params>\())")
AGGREGATE_SUFFIXES = ("::<anonymous>", "::depth")
UNMOVABLE_SUFFIXES = (*AGGREGATE_SUFFIXES, "::<module>")  # every file shares these names


@dataclass(frozen=True)
class Violation:
    metric: str
    key: str
    value: int
    allowed: int


# ── shared helpers ────────────────────────────────────────────────────────────


def cap(metric: str, key: str) -> int:
    """The allowance of a key the baseline has never seen (0 for summed-excess keys)."""
    return 0 if key.endswith(AGGREGATE_SUFFIXES) else CAPS[metric]


def _put(out: Metrics, metric: str, key: str, value: int) -> None:
    """Record `value` if it is over the cap; the worst of several diagnostics on one key wins."""
    if value > cap(metric, key):
        keys = out.setdefault(metric, {})
        keys[key] = max(keys.get(key, 0), value)


def code_lines(lines: list[str]) -> int:
    """A file's length without its copyright header (ADR-102): the opening comment block that names an
    SPDX license, after any shebang, and the blank line that closes it. A legal notice is not code."""
    start = end = 1 if lines and lines[0].startswith("#!") else 0
    while end < len(lines) and NOTICE_LINE.match(lines[end]):
        end += 1
    if not any("SPDX-License-Identifier:" in line for line in lines[start:end]):
        return len(lines)
    if end < len(lines) and not lines[end].strip():
        end += 1
    return len(lines) - (end - start)


def _merge(into: Metrics, part: Metrics) -> None:
    for metric, keys in part.items():
        into.setdefault(metric, {}).update(keys)


def _rel(repo: Path, path: Path | str) -> str:
    return Path(path).resolve().relative_to(repo.resolve()).as_posix()


def _files(root: Path, suffixes: tuple[str, ...]) -> Iterator[Path]:
    return (p for p in sorted(root.rglob("*")) if p.suffix in suffixes and p.is_file())


def _unparsed(rel: str, message: str) -> SystemExit:
    return SystemExit(f"quality: cannot read a diagnostic for {rel}: {message!r} (update quality.py)")


def _run(cmd: list[str], cwd: Path) -> str:
    done = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if not done.stdout.strip():
        raise SystemExit(f"quality: `{' '.join(cmd[:4])}` produced no output\n{done.stderr}")
    return done.stdout


# ── python ────────────────────────────────────────────────────────────────────


def _py_files(repo: Path) -> list[Path]:
    prismoid = (p for p in _files(repo / "backend" / "prismoid", (".py",)))
    return [p for p in prismoid if not _rel(repo, p).startswith(PY_EXCLUDE)]


def _test_files(repo: Path) -> list[Path]:
    return list(_files(repo / "backend" / "tests", (".py",)))


def _def_spans(tree: ast.Module, *, classes: bool = False) -> list[tuple[int, int, str]]:
    """Every def (and class, when asked) as (first line, last line, dotted qualname)."""
    spans: list[tuple[int, int, str]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}{child.name}"
                if classes or not isinstance(child, ast.ClassDef):
                    spans.append((child.lineno, child.end_lineno or child.lineno, name))
                walk(child, f"{name}.")
            else:
                walk(child, prefix)

    walk(tree, "")
    return spans


def _symbol_at(spans: list[tuple[int, int, str]], row: int) -> str:
    """The innermost def containing `row` (the latest-starting span that contains it)."""
    inside = [(start, name) for start, end, name in spans if start <= row <= end]
    return max(inside)[1] if inside else "<module>"


def parse_ruff(raw: str, repo: Path) -> Metrics:
    """Ruff's JSON diagnostics -> py.* symbol metrics, keyed `<path>::<qualname>`."""
    out: Metrics = {}
    spans: dict[str, list[tuple[int, int, str]]] = {}
    for diag in cast(list[dict[str, Any]], json.loads(raw)):
        rel, code = _rel(repo, diag["filename"]), str(diag["code"])
        metric = RUFF_METRICS.get(code)
        if metric is None or rel.startswith(PY_EXCLUDE):
            continue
        found = re.search(r"\((\d+)\s*[>/]\s*\d+\)", str(diag["message"]))
        if rel not in spans:
            spans[rel] = _def_spans(ast.parse((repo / rel).read_text(encoding="utf-8")))
        symbol = _symbol_at(spans[rel], int(diag["location"]["row"]))
        if found is None or (symbol == "<module>" and code != "PLR1702"):
            raise _unparsed(rel, str(diag["message"]))
        _put(out, metric, f"{rel}::{symbol}", int(found.group(1)))
    return out


def endpoint_lines(rel: str, text: str) -> Metrics:
    """Public top-level functions of an endpoint file, measured from the `def` line."""
    out: Metrics = {}
    for node in ast.parse(text).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith("_"):
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            _put(out, "py.endpoint_lines", f"{rel}::{node.name}", length)
    return out


def _is_prismoid(node: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return node.level > 0 or module == "prismoid" or module.startswith("prismoid.")
    return any(alias.name == "prismoid" or alias.name.startswith("prismoid.") for alias in node.names)


def _local_imports(node: ast.AST, inside: bool) -> int:
    count = 0
    for child in ast.iter_child_nodes(node):
        if inside and isinstance(child, ast.Import | ast.ImportFrom) and _is_prismoid(child):
            count += 1
        count += _local_imports(child, inside or isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef))
    return count


def local_imports(text: str) -> int:
    """`prismoid` imports (absolute or relative) anywhere inside a function, nested functions included."""
    return _local_imports(ast.parse(text), False)


def measure_python(repo: Path) -> Metrics:
    out = parse_ruff(_run(RUFF_CMD, repo / "backend"), repo)
    for path in [*_py_files(repo), *_test_files(repo)]:
        lines = code_lines(path.read_text(encoding="utf-8").splitlines())
        _put(out, "py.file_lines", _rel(repo, path), lines)
    for path in _py_files(repo):
        _put(out, "py.local_imports", _rel(repo, path), local_imports(path.read_text(encoding="utf-8")))
    for path in sorted((repo / "backend" / "prismoid" / "api" / "endpoints").glob("*.py")):
        _merge(out, endpoint_lines(_rel(repo, path), path.read_text(encoding="utf-8")))
    return out


# ── web ───────────────────────────────────────────────────────────────────────


def _web_skipped(rel: str, *, tests: bool = True) -> bool:
    """Generated files never count; test files count only where `tests` is False."""
    name = rel.rsplit("/", 1)[-1]
    return name.endswith(".gen.ts") or (tests and ".test." in name)


def _oxlint_entry(rel: str, diagnostic: dict[str, Any]) -> tuple[str, str, int] | None:
    """(metric, key, value) for one oxlint diagnostic, or None when it does not count."""
    code = str(diagnostic.get("code"))
    metric = OXLINT_METRICS.get(code)
    if metric is None:
        return None
    text = str(diagnostic.get("message", ""))
    found = OXLINT_VALUE[code].search(text)
    if found is None:
        raise _unparsed(rel, text)
    named = re.search(r"`([^`]+)`", text)
    if named is not None:
        return metric, f"{rel}::{named.group(1)}", int(found.group(1))
    suffix = "depth" if metric == "web.depth" else "<anonymous>"
    return metric, f"{rel}::{suffix}", int(found.group(1)) - CAPS[metric]


def parse_oxlint(raw: str, repo: Path) -> Metrics:
    """oxlint's JSON report -> web.* symbol metrics; diagnostics sharing a key add their excess.

    A key holds its cap plus every diagnostic's excess, so a second over-cap function of one name
    raises the key instead of hiding behind the first, whatever order oxlint reports them in.
    """
    out: Metrics = {}
    for diagnostic in cast(list[dict[str, Any]], json.loads(raw)["diagnostics"]):
        rel = _rel(repo, repo / diagnostic["filename"])
        if _web_skipped(rel):
            continue
        entry = _oxlint_entry(rel, diagnostic)
        if entry is None:
            continue
        metric, key, value = entry
        limit = cap(metric, key)
        if value > limit:
            keys = out.setdefault(metric, {})
            keys[key] = keys.get(key, limit) + value - limit
    return out


def web_files(repo: Path, *, with_tests: bool = False) -> list[Path]:
    found = [p for root in WEB_ROOTS for p in _files(repo / root, (".ts", ".tsx"))]
    return [p for p in found if not _web_skipped(_rel(repo, p), tests=not with_tests)]


def _run_oxlint(repo: Path) -> str:
    """oxlint takes rule options only from a file, so the caps go through a throwaway config."""
    binary = str(repo / "web" / "node_modules" / ".bin" / "oxlint")
    with tempfile.NamedTemporaryFile("w", suffix=".json") as config:
        json.dump(OXLINT_CONFIG, config)
        config.flush()
        return _run([binary, "-c", config.name, "-f", "json", *WEB_ROOTS], repo)


def metric_disables(text: str) -> int:
    """Comments that switch a measured rule off: oxlint would obey them, so each one counts."""
    rules = (found.group(1).split("--")[0].replace("*/", "").strip() for found in LINT_DISABLE.finditer(text))
    return sum(1 for named in rules if not named or re.search(r"complexity|max-depth|max-lines", named))


def measure_web(repo: Path) -> Metrics:
    if not (repo / "web" / "node_modules").is_dir():
        raise SystemExit("quality: run `pnpm install` at the repo root")
    out = parse_oxlint(_run_oxlint(repo), repo)
    for path in web_files(repo):
        text = path.read_text(encoding="utf-8")
        _put(out, "web.file_lines", _rel(repo, path), code_lines(text.splitlines()))
        _put(out, "web.metric_disables", _rel(repo, path), metric_disables(text))
    return out


# ── go ────────────────────────────────────────────────────────────────────────


def _go_name(receiver: str | None, name: str) -> str:
    if not receiver or not receiver.strip():
        return name
    recv = receiver.split()[-1].lstrip("*").split("[", 1)[0]
    return f"{recv}.{name}"


def parse_go(rel: str, text: str) -> Metrics:
    """A Go file's length and its functions' lengths (`func ` line to the next line exactly `}`)."""
    out: Metrics = {}
    lines = text.splitlines()
    _put(out, "go.file_lines", rel, code_lines(lines))
    for start, line in enumerate(lines):
        head = GO_FUNC.match(line)
        if head is None:
            continue
        end = start
        one_liner = line.rstrip().endswith("}") and line.count("{") == line.count("}")
        while not one_liner and end + 1 < len(lines) and lines[end] != "}":
            end += 1
        name = _go_name(head.group(1), head.group(2))
        _put(out, "go.function_lines", f"{rel}::{name}", end - start + 1)
    return out


def _go_files(repo: Path, *, with_tests: bool = False) -> list[Path]:
    found = _files(repo / "sandbar", (".go",))
    return [p for p in found if with_tests or not p.name.endswith("_test.go")]


def measure_go(repo: Path) -> Metrics:
    out: Metrics = {}
    for path in _go_files(repo):
        _merge(out, parse_go(_rel(repo, path), path.read_text(encoding="utf-8")))
    return out


# ── comments and tests ────────────────────────────────────────────────────────


def _scan_line(line: str, in_block: bool) -> tuple[list[str], bool]:
    """The comment text on one line of Go/TS (`//` and `/* ... */`), and whether a block stays open."""
    found: list[str] = []
    rest = line
    while rest:
        if in_block:
            end = rest.find("*/")
            found.append(rest if end < 0 else rest[:end])
            in_block, rest = end < 0, ("" if end < 0 else rest[end + 2 :])
            continue
        opened = COMMENT_OPEN.search(rest)
        if opened is None:
            break
        if opened.group(1) == "//":
            found.append(rest[opened.end() :])
            break
        in_block, rest = True, rest[opened.end() :]
    return found, in_block


def _slash_comments(text: str) -> Iterator[str]:
    in_block = False
    for line in text.splitlines():
        found, in_block = _scan_line(line, in_block)
        if found:
            yield " ".join(found)


def _comments(rel: str, text: str) -> Iterator[str]:
    if rel.endswith(".py"):
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        return (tok.string for tok in tokens if tok.type == tokenize.COMMENT)
    return _slash_comments(text)


def process_refs(rel: str, text: str) -> int:
    """Comment lines citing a process artifact (rulings, review rounds, fix waves, task ids, dated
    incidents) - never ADRs."""
    return sum(1 for comment in _comments(rel, text) if PROCESS_REF.search(comment))


def measure_comments(repo: Path) -> Metrics:
    """Every comment under backend/prismoid, backend/tests, web/app and sandbar, test files included."""
    out: Metrics = {}
    web, go = web_files(repo, with_tests=True), _go_files(repo, with_tests=True)
    for path in [*_py_files(repo), *_test_files(repo), *web, *go]:
        rel = _rel(repo, path)
        _put(out, "comments.process_refs", rel, process_refs(rel, path.read_text(encoding="utf-8")))
    return out


def private_access(text: str) -> int:
    """`reportPrivateUsage` ignores and directives, file-level modes below strict (`basic`, `standard`)
    and blanket ignores: `# pyright: ignore` with no rule list and `# type: ignore` in any form."""
    return (
        text.count("reportPrivateUsage")
        + len(PYRIGHT_LAX_MODE.findall(text))
        + len(BLANKET_IGNORE.findall(text))
    )


def measure_tests(repo: Path) -> Metrics:
    out: Metrics = {}
    total = sum(private_access(p.read_text(encoding="utf-8")) for p in _test_files(repo))
    _put(out, "tests.private_access", "backend/tests", total)
    return out


def measure(repo: Path) -> Metrics:
    """Every metric's offenders today (values over the cap only)."""
    out: Metrics = {}
    for part in (measure_python, measure_web, measure_go, measure_comments, measure_tests):
        _merge(out, part(repo))
    return out


# ── compare and ratchet ───────────────────────────────────────────────────────


def _web_stop(text: str, start: int) -> int:
    """Where a run of source ends: its first `;` outside brackets, or the bracket closing around it."""
    depth = 0
    for token in WEB_TOKEN.finditer(text, start):
        depth += (token.group() in ("(", "[", "{")) - (token.group() in (")", "]", "}"))
        if depth < 0 or (depth == 0 and token.group() == ";"):
            return token.start()
    return len(text)


def _web_function_at(text: str, start: int, *, may_be_type: bool = False) -> bool:
    """Whether a function value starts at `start`; after a `:` the same head can be a function type,
    and only a type member ends in `;` (an object property ends in `,` or its object's `}`)."""
    head = WEB_FUNCTION.match(text, start)
    if head is None or head.group("params") is None:
        return head is not None
    arrow = WEB_ARROW.match(text, _web_stop(text, head.end()) + 1)
    if arrow is None:
        return False
    return not may_be_type or not text.startswith(";", _web_stop(text, arrow.end()))


def _web_declares(text: str, name: str) -> bool:
    """A declaration of `name`: a function, a variable, a method with a body or a function member."""
    n = re.escape(name)
    if re.search(rf"\bfunction\s*\*?\s*{n}\b|\b(?:const|let|var)\s+{n}\s*[=:]", text):
        return True
    member = rf"^\s*{WEB_MODIFIERS}#?{n}\s*"
    methods = re.finditer(rf"{member}{WEB_GENERICS}\(", text, re.MULTILINE)
    if any(WEB_BODY.match(text, _web_stop(text, m.end()) + 1) for m in methods):
        return True
    fields = re.finditer(rf"{member}(?::[^=\n]*)?=", text, re.MULTILINE)
    properties = re.finditer(rf"{member}:", text, re.MULTILINE)
    return any(_web_function_at(text, m.end()) for m in fields) or any(
        _web_function_at(text, m.end(), may_be_type=True) for m in properties
    )


def symbol_exists(repo: Path, key: str) -> bool:
    """Whether the symbol a `<path>::<name>` key names is still declared in its file on disk."""
    rel, _, name = key.partition("::")
    path = repo / rel
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if not name or name == "<module>":
        return True
    if path.suffix == ".py":
        return name in {qualname for _, _, qualname in _def_spans(ast.parse(text), classes=True)}
    if path.suffix == ".go":
        heads = (GO_FUNC.match(line) for line in text.splitlines())
        return any(_go_name(h.group(1), h.group(2)) == name for h in heads if h is not None)
    return _web_declares(text, name)


def _git(repo: Path, *args: str) -> str | None:
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    except OSError:
        return None
    return done.stdout if done.returncode == 0 else None


def file_renames(repo: Path) -> dict[str, str]:
    """Paths git saw renamed (old -> new): committed since the merge-base with main, or staged.

    Without git, a repository, `main` or deep enough history, it falls back to HEAD, then to none.
    """
    bases = (_git(repo, "merge-base", "HEAD", ref) for ref in ("main", "origin/main"))
    base = next((b.strip() for b in bases if b), "HEAD")
    renames: dict[str, str] = {}
    for line in (_git(repo, "diff", "-M", "--name-status", base) or "").splitlines():
        status, *paths = line.split("\t")
        if status.startswith("R") and len(paths) == 2:
            renames[paths[0]] = paths[1]
    for line in (_git(repo, "status", "--porcelain") or "").splitlines():
        if "R" in line[:2] and " -> " in line:
            old, new = line[3:].split(" -> ", 1)
            renames[old] = new
    return renames


def _movable(key: str) -> str | None:
    """The symbol name a key may move under; file keys and per-file keys never move by name."""
    if "::" not in key or key.endswith(UNMOVABLE_SUFFIXES):
        return None
    return key.split("::", 1)[1]


def _renamed(key: str, renames: dict[str, str]) -> str | None:
    """The key a baseline key becomes when git renamed its file, else None."""
    path, sep, rest = key.partition("::")
    new = renames.get(path)
    return None if new is None else f"{new}{sep}{rest}"


def _heirs(
    current: dict[str, int], baseline: dict[str, int], repo: Path, renames: dict[str, str]
) -> dict[str, int]:
    """New keys that inherit a renamed file's or a moved symbol's baseline value.

    A baseline key gone from `current` whose file git renamed goes to the same key under the new
    path. Otherwise it is an orphan when its symbol is also gone from its old file (a symbol that
    merely shrank under the cap leaves nothing to inherit); each orphan goes to at most one new key
    of the same name, largest orphan to largest heir, and any heir left over is new.
    """
    heirs: dict[str, int] = {}
    orphans: dict[str, list[int]] = {}
    for key, value in baseline.items():
        if key in current:
            continue
        new = _renamed(key, renames)
        if new is not None and new in current and new not in baseline and new not in heirs:
            heirs[new] = value
            continue
        name = _movable(key)
        if name is not None and not symbol_exists(repo, key):
            orphans.setdefault(name, []).append(value)
    claimants: dict[str, list[str]] = {}
    for key in current:
        name = _movable(key)
        if name is not None and key not in baseline and key not in heirs:
            claimants.setdefault(name, []).append(key)
    for name, values in orphans.items():
        ranked = sorted(claimants.get(name, []), key=lambda k: (-current[k], k))
        heirs.update(zip(ranked, sorted(values, reverse=True), strict=False))
    return heirs


def _allowances(
    metric: str, current: dict[str, int], baseline: dict[str, int], repo: Path, renames: dict[str, str]
) -> dict[str, int]:
    """Each current key's allowance: its baseline value, a moved key's old value, or the cap."""
    heirs = _heirs(current, baseline, repo, renames)
    allowed: dict[str, int] = {}
    for key in current:
        if key in baseline:
            allowed[key] = baseline[key]
        else:
            allowed[key] = heirs.get(key, cap(metric, key))
    return allowed


def compare(
    current: Metrics, baseline: Metrics, *, repo: Path = REPO, renames: dict[str, str] | None = None
) -> list[Violation]:
    """Every key over its allowance, sorted by metric then key (`renames` defaults to git's)."""
    renames = file_renames(repo) if renames is None else renames
    violations: list[Violation] = []
    for metric in sorted(current):
        keys = current[metric]
        allowed = _allowances(metric, keys, baseline.get(metric, {}), repo, renames)
        for key in sorted(keys):
            if keys[key] > allowed[key]:
                violations.append(Violation(metric, key, keys[key], allowed[key]))
    return violations


def ratchet(
    current: Metrics, baseline: Metrics, *, repo: Path = REPO, renames: dict[str, str] | None = None
) -> Metrics:
    """The next baseline: only still-over keys, never higher than before, moves and renames re-keyed.

    A new offender's allowance is the cap, so `min` brings it down to the cap and it drops out:
    the ratchet never admits a key `compare` would reject.
    """
    renames = file_renames(repo) if renames is None else renames
    out: Metrics = {}
    for metric in sorted(set(current) | set(baseline)):
        keys = current.get(metric, {})
        allowed = _allowances(metric, keys, baseline.get(metric, {}), repo, renames)
        lowered = {key: min(value, allowed[key]) for key, value in keys.items()}
        out[metric] = {key: v for key, v in lowered.items() if v > cap(metric, key)}
    return out


def tightening(
    current: Metrics, baseline: Metrics, *, repo: Path = REPO, renames: dict[str, str] | None = None
) -> list[str]:
    """What `ratchet` would change in the baseline, one `metric key old -> new` line per key."""
    after = ratchet(current, baseline, repo=repo, renames=renames)
    changes: list[str] = []
    for metric in sorted(set(after) | set(baseline)):
        old, new = baseline.get(metric, {}), after.get(metric, {})
        for key in sorted(set(old) | set(new)):
            if old.get(key) != new.get(key):
                was, now = old.get(key, "new"), new.get(key, "gone")
                changes.append(f"{metric:<22} {key}  {was} -> {now}")
    return changes


# ── baseline file and report ──────────────────────────────────────────────────


def load_baseline(path: Path) -> Metrics:
    if not path.exists():
        return {}
    data = cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))
    return cast(Metrics, data.get("metrics", {}))


def write_baseline(path: Path, metrics: Metrics) -> None:
    full = {metric: dict(sorted(metrics.get(metric, {}).items())) for metric in CAPS}
    text = json.dumps({"caps": CAPS, "metrics": full}, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def _excess(metric: str, keys: dict[str, int]) -> int:
    return sum(value - cap(metric, key) for key, value in keys.items())


def report_rows(current: Metrics, baseline: Metrics) -> list[dict[str, int | str]]:
    rows: list[dict[str, int | str]] = []
    for metric in CAPS:
        now, base = current.get(metric, {}), baseline.get(metric, {})
        rows.append(
            {
                "metric": metric,
                "offenders": len(now),
                "baseline_offenders": len(base),
                "worst": max(now.values(), default=0),
                "excess": _excess(metric, now),
                "baseline_excess": _excess(metric, base),
            }
        )
    return rows


def baseline_at(repo: Path, ref: str) -> Metrics | None:
    """The baseline as committed at `ref`, or None when git cannot show it."""
    text = _git(repo, "show", f"{ref}:backend/tools/quality_baseline.json")
    if text is None:
        return None
    return cast(Metrics, cast(dict[str, Any], json.loads(text)).get("metrics", {}))


def release_gaps(current: Metrics, previous: Metrics) -> list[str]:
    """Each metric in `SHRINK_PER_RELEASE` whose excess has not shed its share since `previous`."""
    gaps: list[str] = []
    for metric, share in SHRINK_PER_RELEASE.items():
        if metric not in previous:
            continue
        then, now = _excess(metric, previous[metric]), _excess(metric, current.get(metric, {}))
        allowed = math.floor(then * (1 - share))
        if now > allowed:
            gaps.append(f"{metric:<22} {now} > {allowed} ({then} at the last release, -{share:.0%})")
    return gaps


def run_release(current: Metrics, repo: Path, since: str | None) -> int:
    """`release`: 1 when a metric has not paid down its share since the last release tag."""
    ref = since or (_git(repo, "describe", "--tags", "--abbrev=0") or "").strip()
    previous = baseline_at(repo, ref) if ref else None
    if previous is None:
        print(f"quality: no baseline at {ref or 'any release tag'}; pass --since REF", file=sys.stderr)
        return 2
    gaps = release_gaps(current, previous)
    for line in gaps:
        print(f"  {line}")
    if gaps:
        print(f"quality: {len(gaps)} metric(s) have not paid down their share since {ref}")
        return 1
    print(f"quality: release ok against {ref}")
    return 0


def _print_report(rows: list[dict[str, int | str]]) -> None:
    head = ("metric", "offenders", "(base)", "worst", "excess", "(base)", "delta")
    print(f"{head[0]:<24}{head[1]:>10}{head[2]:>8}{head[3]:>8}{head[4]:>9}{head[5]:>9}{head[6]:>8}")
    for row in rows:
        delta = int(row["excess"]) - int(row["baseline_excess"])
        print(
            f"{row['metric']!s:<24}{row['offenders']:>10}{row['baseline_offenders']:>8}"
            f"{row['worst']:>8}{row['excess']:>9}{row['baseline_excess']:>9}{delta:>+8}"
        )


# ── CLI ───────────────────────────────────────────────────────────────────────


def run_check(
    current: Metrics,
    baseline: Metrics,
    as_json: bool = False,
    *,
    repo: Path = REPO,
    renames: dict[str, str] | None = None,
) -> int:
    """`check`: 1 when a key is over its allowance or the baseline is not tight."""
    renames = file_renames(repo) if renames is None else renames
    violations = compare(current, baseline, repo=repo, renames=renames)
    loose = [] if violations else tightening(current, baseline, repo=repo, renames=renames)
    if as_json:
        print(json.dumps({"violations": [asdict(v) for v in violations], "loose": loose}, indent=2))
        return 1 if violations or loose else 0
    for v in violations:
        print(f"  {v.metric:<22} {v.key}  {v.value} > {v.allowed}")
    if violations:
        print(f"quality: {len(violations)} over (ADR-075: new code meets the caps; offenders only shrink)")
        print("  split the symbol, or after an improvement run `just ratchet`")
        if any("::" not in v.key and v.key not in baseline.get(v.metric, {}) for v in violations):
            print(
                "  a new file over its cap: a renamed file? stage the rename (`git mv`) so the ratchet"
                " can pair it, then `just ratchet`; otherwise split the file"
            )
        return 1
    for line in loose:
        print(f"  {line}")
    if loose:
        print(
            "quality: the baseline is not tight (moves or improvements not yet recorded)"
            " - run `just ratchet` and commit the baseline"
        )
        return 1
    print(f"quality: ok ({sum(len(keys) for keys in current.values())} known offenders, none grew)")
    return 0


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("command", choices=("check", "ratchet", "report", "release"))
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--init", action="store_true", help="ratchet: seed with today's offenders")
    parser.add_argument("--force", action="store_true", help="ratchet --init: replace an existing baseline")
    parser.add_argument(
        "--since", help="release: the ref to measure the pay-down from (default: the last tag)"
    )
    args = parser.parse_args(argv)
    if args.init and args.command != "ratchet":
        parser.error("--init applies to `ratchet` only")
    if args.force and not args.init:
        parser.error("--force applies to `ratchet --init` only")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    path = cast(Path, args.baseline)
    if args.init and path.exists() and not args.force:
        print(
            f"quality: {path} exists; `--init` would re-admit every offender it has shrunk."
            " Use `ratchet` to lower it, or add `--force` to re-seed on purpose.",
            file=sys.stderr,
        )
        return 2
    current, baseline = measure(REPO), load_baseline(path)
    if args.command == "check":
        return run_check(current, baseline, bool(args.json))
    if args.command == "release":
        return run_release(current, REPO, cast(str | None, args.since))
    if args.command == "ratchet":
        write_baseline(path, current if args.init else ratchet(current, baseline))
        print(f"quality: baseline written to {path}")
        return 0
    rows = report_rows(current, baseline)
    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        _print_report(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
