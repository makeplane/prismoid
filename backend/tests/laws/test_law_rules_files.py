# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Rules files are honest: every folder `CLAUDE.md` is mirrored by an `AGENTS.md` symlink and cites
only ADRs, laws, recipes and quality metrics that exist.

Protects the folder rules files (the root CLAUDE.md's convention, one per important folder): a
rules file that points at a missing ADR, a deleted law, a renamed `just` recipe or a dropped quality
metric teaches the next agent something false. The population is every `CLAUDE.md` found by walking
the repo, minus the root's own (the audit holds that one), `docs/`, `node_modules/` and hidden folders
(`.claude/` worktrees, `.git`, `.venv`, `.superpowers`). Citations are read by grammar: `ADR-NNN`
and `tests/laws/test_*.py` (or `laws/test_*.py`) anywhere; a backticked bare `test_law_*.py`; `just <recipe>` anywhere
inside a backtick span (`cd web && just web-check`); a backticked `quality:<metric>`.
"""

from __future__ import annotations

import functools
import os
import re
from pathlib import Path

from tests.support.paths import REPO
from tools import quality

BACKEND = REPO / "backend"

ADR = re.compile(r"\bADR-(\d+)")
LAW = re.compile(r"(?:backend/)?(tests/laws/test_\w+\.py)")
SHORT_LAW = re.compile(r"(?<![\w/])laws/(test_\w+\.py)")
BARE_LAW = re.compile(r"`(test_law_\w+\.py)`")
SPAN = re.compile(r"`([^`\n]+)`")
RECIPE_CITE = re.compile(r"\bjust ([a-z][\w-]*)")
METRIC = re.compile(r"`quality:([\w.]+)`")
RECIPE_HEADER = re.compile(r"^([a-z][a-z0-9_-]*)\b[^:]*:(?!=)")


@functools.cache
def rules_files() -> tuple[Path, ...]:
    found: list[Path] = []
    for root, dirs, files in os.walk(REPO):
        here = Path(root)
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d != "node_modules" and here / d != REPO / "docs"
        )
        if "CLAUDE.md" in files and here != REPO:
            found.append(here / "CLAUDE.md")
    return tuple(found)


@functools.cache
def recipes() -> frozenset[str]:
    names: set[str] = set()
    for line in (REPO / "justfile").read_text().splitlines():
        if line.startswith(("set ", "export ")):
            continue
        if found := RECIPE_HEADER.match(line):
            names.add(found.group(1))
    return frozenset(names)


@functools.cache
def adrs() -> frozenset[str]:
    return frozenset(p.name.split("-")[0] for p in (REPO / "docs" / "adr").glob("[0-9]*-*.md"))


def citation_problems(text: str) -> list[str]:
    """Every citation in a rules file's `text` that names something that does not exist."""
    problems = [
        f"ADR-{n} has no docs/adr/{int(n):03d}-*.md"
        for n in ADR.findall(text)
        if f"{int(n):03d}" not in adrs()
    ]
    laws = LAW.findall(text) + [
        f"tests/laws/{name}" for name in BARE_LAW.findall(text) + SHORT_LAW.findall(text)
    ]
    problems += [f"{law} does not exist under backend/" for law in laws if not (BACKEND / law).is_file()]
    cited = [r for span in SPAN.findall(text) for r in RECIPE_CITE.findall(span)]
    problems += [f"`just {r}` is not a recipe in the justfile" for r in cited if r not in recipes()]
    problems += [
        f"quality:{m} is not a key of quality.CAPS" for m in METRIC.findall(text) if m not in quality.CAPS
    ]
    return problems


def test_agents_md_is_a_symlink_to_claude_md() -> None:
    wrong = [
        str(path.parent.relative_to(REPO))
        for path in rules_files()
        if not (path.parent / "AGENTS.md").is_symlink()
        or os.readlink(path.parent / "AGENTS.md") != "CLAUDE.md"
    ]
    assert not wrong, f"each needs `ln -s CLAUDE.md AGENTS.md` beside its CLAUDE.md: {wrong}"


def test_rules_files_cite_only_what_exists() -> None:
    problems = [
        f"{path.relative_to(REPO)}: {problem}"
        for path in rules_files()
        for problem in citation_problems(path.read_text())
    ]
    assert not problems, "\n".join(problems)


def test_the_important_folders_have_rules_files() -> None:
    assert len(rules_files()) >= 13


def test_the_justfile_parse_finds_recipes_and_skips_settings() -> None:
    found = recipes()
    assert {"check", "test", "test-unit", "load", "quality", "export-contract"} <= found
    assert not {"set", "COMPOSE", "export"} & found


def test_the_citation_check_bites() -> None:
    bad = "See ADR-999, `tests/laws/test_nope.py`, `just nope` and `quality:py.nope`.\n"
    assert len(citation_problems(bad)) == 4, citation_problems(bad)
    inside = "Run `uv run pytest tests/laws/test_nope.py -q`, then `cd web && just nope`; see `test_law_nope.py`.\n"
    assert len(citation_problems(inside)) == 3, citation_problems(inside)
    good = "ADR-075 holds it: `tests/laws/test_tenancy_map.py`, `just check`, `quality:py.complexity`.\n"
    assert citation_problems(good) == []
    short = (
        "Cite `laws/test_nope.py` and `laws/test_tenancy_map.py`; `tests/laws/test_tenancy_map.py` once.\n"
    )
    assert citation_problems(short) == ["tests/laws/test_nope.py does not exist under backend/"]
    also_good = "`uv run pytest tests/laws/test_law_keyset_only.py`, `cd backend && just test-unit -x`, `test_law_rules_files.py`; just words.\n"
    assert citation_problems(also_good) == []
