# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The deck suite as it really runs: isolated like the device (`-I`), `document.py` beside it."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from pptx import Presentation

from prismoid.ai.verification import pipeline

SOURCE = "Q3 revenue was **$41.6M**, against $28.4M in Q3 last year."


def _job(tmp_path: Path, slide: str) -> dict[str, object]:
    for script in ("deck.py", *pipeline.COMPANIONS["deck.py"]):
        shutil.copy(pipeline.SUITES / script, tmp_path / script)
    deck, source = tmp_path / "deck.pptx", tmp_path / "brief.md"
    source.write_text(SOURCE)
    prs = Presentation()
    prs.slides.add_slide(prs.slide_layouts[5]).shapes.title.text = slide  # pyright: ignore[reportOptionalMemberAccess]
    prs.save(str(deck))
    return {
        "artifact_path": str(deck),
        "artifact_sha256": hashlib.sha256(deck.read_bytes()).hexdigest(),
        "sources": [str(source)],
        "extract_path": str(tmp_path / "extracted.md"),
        "extract_chars": pipeline.EXTRACT_CHARS,
        "extract_cut": pipeline.EXTRACT_CUT,
    }


def _checks(job: dict[str, object], *argv: Path) -> dict[str, dict[str, object]]:
    out = subprocess.run(
        [sys.executable, "-I", *map(str, argv)], input=json.dumps(job), capture_output=True, text=True
    )
    return {c["name"]: c for c in json.loads(out.stdout)["checks"]}


def _grounded(tmp_path: Path, slide: str) -> dict[str, object]:
    return _checks(_job(tmp_path, slide), tmp_path / "deck.py")["numbers:grounded"]


def test_a_correct_calculation_on_a_slide_is_derived_and_names_its_working(tmp_path: Path) -> None:
    check = _grounded(tmp_path, "Revenue $41.6M, up $13.2M or 46.5% on last year")
    assert check["status"] == "pass"
    assert "2 derived" in str(check["detail"]) and "46.5% = " in str(check["detail"])


@pytest.mark.parametrize("slide", ["Revenue reached $55.0M", "Revenue grew 71.3%"])
def test_an_invented_figure_still_fails_the_deck(tmp_path: Path, slide: str) -> None:
    assert _grounded(tmp_path, slide)["status"] == "fail"


def test_the_judge_gets_each_slides_text_under_its_number(tmp_path: Path) -> None:
    job = _job(tmp_path, "Revenue $41.6M")
    _checks(job, tmp_path / "deck.py")
    assert Path(str(job["extract_path"])).read_text() == "## Slide 1\n\nRevenue $41.6M"
