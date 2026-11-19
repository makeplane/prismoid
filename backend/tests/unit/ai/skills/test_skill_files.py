# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Skills carry assets (ADR-015): the path law, the budgets, and the two lockstep pins that
keep the organize skill's bundled tools honest - its schema reference equals the product's
exported schema byte for byte, and its validate script agrees with the pydantic model mutation
by mutation (a THIRD definition of "valid collection" is only tolerable pinned to the first)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from prismoid.ai.skills.format import SkillError, parse, parse_dir
from prismoid.schemas.collection import Collection
from prismoid.schemas.skills import MAX_SKILL_BUNDLE_BYTES, MAX_SKILL_FILES
from tests.support.paths import BACKEND
from tests.support.samples import FIXTURE, MUTATIONS

ORGANIZE = BACKEND / "skills" / "organize"

SKILL_MD = (
    "---\ndescription: Load when testing bundled files. Not for anything real.\n---\nA body of judgment.\n"
)
CASES = 'routing:\n  - goal: "bundle test"\n    expect: bundles\n'


# ── the path law and the budgets ──────────────────────────────────────────────────────────────


def test_files_must_live_under_the_three_subdirs() -> None:
    for bad in ("SKILL2.md", "../etc/passwd", "scripts/../../x", "tools/x.py", "/scripts/x.py"):
        with pytest.raises(SkillError):
            parse("bundles", SKILL_MD, CASES, files=[(bad, b"x")])


def test_the_file_count_and_byte_budgets_hold() -> None:
    too_many = [(f"references/f{i}.md", b"x") for i in range(MAX_SKILL_FILES + 1)]
    with pytest.raises(SkillError):
        parse("bundles", SKILL_MD, CASES, files=too_many)
    with pytest.raises(SkillError):
        parse("bundles", SKILL_MD, CASES, files=[("assets/big.bin", b"x" * (MAX_SKILL_BUNDLE_BYTES + 1))])


def test_legal_files_ride_through_parse() -> None:
    parsed = parse("bundles", SKILL_MD, CASES, files=[("scripts/validate.py", b"print()")])
    assert [f.path for f in parsed.files] == ["scripts/validate.py"]


# ── the organize skill is the kind-authoring template, pinned both ways ───────────────────────


def test_organize_bundles_its_schema_and_validator() -> None:
    parsed = parse_dir(ORGANIZE)
    paths = {f.path for f in parsed.files}
    assert "references/collection.schema.json" in paths
    assert "scripts/validate.py" in paths


def test_the_bundled_schema_is_the_exported_schema_byte_for_byte() -> None:
    """One definition (`schemas/collection.Collection`); the skill carries a COPY, so the copy is
    equality-pinned to `model_json_schema()` exactly as the exporter writes it."""
    bundled = (ORGANIZE / "references" / "collection.schema.json").read_text()
    exported = json.dumps(Collection.model_json_schema(), indent=2, sort_keys=True) + "\n"
    assert bundled == exported, "re-run `just export-contract` and copy the schema into the skill"


# ── the document-workshop skills bundle their tools ───────────────────────────────────────────

WORKSHOP_BUNDLES = {
    "model": ("scripts/check_workbook.py", "references/openpyxl.md"),
    "deck": ("scripts/check_deck.py", "references/design.md"),
    "docx": ("scripts/check_docx.py", "references/python-docx.md"),
    "pdf": ("scripts/check_pdf.py", "references/reportlab.md"),
    "page": ("scripts/check_page.py", "references/design.md"),
}


@pytest.mark.parametrize("skill", WORKSHOP_BUNDLES, ids=WORKSHOP_BUNDLES.keys())
def test_workshop_skills_bundle_their_checker_and_reference(skill: str) -> None:
    parsed = parse_dir(BACKEND / "skills" / skill)
    paths = {f.path for f in parsed.files}
    assert set(WORKSHOP_BUNDLES[skill]) <= paths


@pytest.mark.parametrize("skill", WORKSHOP_BUNDLES, ids=WORKSHOP_BUNDLES.keys())
def test_workshop_checkers_at_least_compile(skill: str) -> None:
    """The frame-lib checkers (openpyxl/pptx/docx/pypdf) cannot RUN in this venv - the libraries
    live in the frame image only - but a checker with a syntax error is dead in both worlds.
    `ast.parse`, not `py_compile`: compiling writes `__pycache__` droppings INTO the skill source
    tree, which the path law then (rightly) refuses at the next parse. Functional coverage runs
    in-frame: tests/integration/bots/test_skill_scripts.py."""
    import ast

    script = WORKSHOP_BUNDLES[skill][0]
    ast.parse((BACKEND / "skills" / skill / script).read_text())


# ── check_page.py is stdlib, so its full matrix runs right here ───────────────────────────────

PAGE_CHECK = BACKEND / "skills" / "page" / "scripts" / "check_page.py"

CLEAN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tide tracker</title><style>body { color: #1C2321 }</style></head>
<body><header><h1>Tides at Alsea Bay</h1></header>
<main><p>High water 14:02.</p><a href="https://noaa.gov">source</a></main>
<script>document.title = document.title;</script></body></html>
"""

PAGE_MUTATIONS = {
    "no-doctype": CLEAN_PAGE.replace("<!doctype html>\n", ""),
    "no-title": CLEAN_PAGE.replace("<title>Tide tracker</title>", ""),
    "two-h1": CLEAN_PAGE.replace("</header>", "</header><h1>again</h1>"),
    "no-lang": CLEAN_PAGE.replace(' lang="en"', ""),
    "no-viewport": CLEAN_PAGE.replace(
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n', ""
    ),
    "cdn-script": CLEAN_PAGE.replace(
        "<script>", '<script src="https://cdn.example.com/x.js"></script><script>'
    ),
    "remote-css": CLEAN_PAGE.replace(
        "<style>", '<link href="//fonts.example.com/a.css" rel="stylesheet"><style>'
    ),
    "css-import": CLEAN_PAGE.replace("body { color", "@import 'https://x.example/a.css'; body { color"),
}


def run_page_check(tmp_path: Path, html: str) -> int:
    file = tmp_path / "index.html"
    file.write_text(html)
    done = subprocess.run(
        [sys.executable, str(PAGE_CHECK), str(file)],
        capture_output=True,
        timeout=30,
    )
    return done.returncode


def test_check_page_accepts_a_selfcontained_page(tmp_path: Path) -> None:
    assert run_page_check(tmp_path, CLEAN_PAGE) == 0, "an outbound <a href> is content, not a load"


@pytest.mark.parametrize("html", PAGE_MUTATIONS.values(), ids=PAGE_MUTATIONS.keys())
def test_check_page_refuses_each_violation(tmp_path: Path, html: str) -> None:
    assert run_page_check(tmp_path, html) == 1


def run_validate(tmp_path: Path, doc: object) -> int:
    file = tmp_path / "x.collection.json"
    file.write_text(json.dumps(doc))
    done = subprocess.run(
        [sys.executable, str(ORGANIZE / "scripts" / "validate.py"), str(file)],
        capture_output=True,
        timeout=30,
    )
    return done.returncode


def test_the_validate_script_accepts_what_the_model_accepts(tmp_path: Path) -> None:
    Collection.model_validate(FIXTURE)
    assert run_validate(tmp_path, FIXTURE) == 0


@pytest.mark.parametrize("doc", MUTATIONS.values(), ids=MUTATIONS.keys())
def test_the_validate_script_refuses_what_the_model_refuses(tmp_path: Path, doc: object) -> None:
    assert run_validate(tmp_path, doc) == 1, "the skill's validator let through what the model refuses"
