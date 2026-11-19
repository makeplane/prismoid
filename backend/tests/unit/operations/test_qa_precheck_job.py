# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The QA gate's pure decisions: which artifact it reads, the size band it hands the precheck, the
job file the frame runs, and how a precheck run's output becomes checks - or an infra failure that
costs the gate instead of a rebuild."""

from __future__ import annotations

import orjson
import pytest

from prismoid.ai.frames.driver import ExecResult
from prismoid.db.models.artifact import Artifact
from prismoid.operations.qa import pick_deliverable, precheck_checks, precheck_job, qa_size_band
from prismoid.schemas.qa import QACheck


def _artifact(role: str) -> Artifact:
    return Artifact(role=role)


def test_qa_reads_the_deliverable_else_the_first_artifact() -> None:
    notes, deliverable = _artifact("supporting"), _artifact("deliverable")
    assert pick_deliverable([notes, deliverable]) is deliverable
    assert pick_deliverable([notes, _artifact("supporting")]) is notes
    assert pick_deliverable([]) is None


@pytest.mark.parametrize(
    ("deliverable", "scale", "target_pages", "band"),
    [
        # no kind, auto scale, no target: a standard document, nothing to size against
        (None, "auto", None, ("document", "pages", 0, 0, 0.4)),
        ("document", "long", 12, ("document", "pages", 12, 3600, 0.25)),
        ("deck", "brief", 8, ("deck", "slides", 8, 1040, 0.5)),
        ("spreadsheet", None, 3, ("spreadsheet", "sheets", 3, 1800, 0.4)),
        # a kind with no words per unit falls back to a page's worth of words
        ("code", "standard", 2, ("code", "scope", 2, 600, 1.0)),
    ],
)
def test_the_size_band_comes_from_the_one_table(
    deliverable: str | None,
    scale: str | None,
    target_pages: int | None,
    band: tuple[str, str, int, int, float],
) -> None:
    kind, unit, target_units, target_words, tolerance = band
    assert qa_size_band(deliverable, scale, target_pages) == {
        "kind": kind,
        "unit": unit,
        "target_units": target_units,
        "target_words": target_words,
        "tolerance": tolerance,
    }


def test_an_unknown_deliverable_kind_raises() -> None:
    with pytest.raises(ValueError, match="nonsense"):
        qa_size_band("nonsense", "auto", None)


def _spell(path: str) -> str:
    return "/real" + path


SIZE: dict[str, object] = {
    "kind": "document",
    "unit": "pages",
    "target_units": 4,
    "target_words": 1200,
    "tolerance": 0.4,
}


def test_the_job_names_every_present_file_in_frame_paths() -> None:
    job = precheck_job(
        _spell, filename="report.docx", has_rendition=True, has_outline=True, figure_ids=["fig-a"], size=SIZE
    )
    assert job == {
        "deliverable": "/real/work/report.docx",
        "rendition": "/real/work/rendition.pdf",
        "outline": "/real/work/outline.md",
        **SIZE,
        "figure_ids": ["fig-a"],
    }
    assert list(job) == [
        "deliverable",
        "rendition",
        "outline",
        "kind",
        "unit",
        "target_units",
        "target_words",
        "tolerance",
        "figure_ids",
    ]


def test_the_job_leaves_absent_files_as_none() -> None:
    job = precheck_job(
        _spell, filename="deck.pptx", has_rendition=False, has_outline=False, figure_ids=[], size=SIZE
    )
    assert job["rendition"] is None
    assert job["outline"] is None
    assert job["deliverable"] == "/real/work/deck.pptx"


def test_a_successful_run_reports_the_checks_on_its_last_line() -> None:
    last = orjson.dumps({"checks": [{"name": "qa:sections", "ok": False, "detail": "two missing"}]}).decode()
    result = ExecResult(exit_code=0, stdout=f"converting...\n{last}\n", stderr="noise")
    assert precheck_checks(result) == [QACheck(name="qa:sections", ok=False, detail="two missing")]
    assert precheck_checks(ExecResult(exit_code=0, stdout="{}", stderr="")) == []


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (ExecResult(exit_code=1, stdout='{"checks": []}', stderr="boom"), "boom"),
        (ExecResult(exit_code=0, stdout="  \n", stderr=""), "qa precheck did not run"),
        (ExecResult(exit_code=2, stdout="", stderr=""), "qa precheck did not run"),
        (ExecResult(exit_code=1, stdout="", stderr="x" * 900), "x" * 400),
    ],
)
def test_a_precheck_that_could_not_run_is_infra(result: ExecResult, message: str) -> None:
    with pytest.raises(RuntimeError) as raised:
        precheck_checks(result)
    assert str(raised.value) == message
