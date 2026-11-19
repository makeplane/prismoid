# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The spreadsheet suite when LibreOffice cannot compute the workbook: the verifier's gap, never the document's.

Missing, hung or silent, a failed recalc is a structural `suite:recalc` SKIP and no formula is judged, while
the checks that need no computing still do their work. What LibreOffice computes (a chain of formulas,
another sheet, SUMIF/VLOOKUP, a `#REF!`) is proved in-frame, where it lives:
`tests/integration/verification/test_spreadsheet_suite.py`.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import openpyxl
import pytest

from prismoid.ai.verification import pipeline
from prismoid.ai.verification.suites import spreadsheet
from prismoid.schemas.verification import Check, Verdict

# What `soffice` is on PATH: absent, hung past the deadline, or exiting without writing the workbook.
SOFFICES = {"missing": None, "hung": "exec sleep 5", "silent": "exit 0"}


def _checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, soffice: str | None, total: object
) -> dict[str, dict[str, object]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    if soffice is not None:
        (bin_dir / "soffice").write_text(f"#!/bin/sh\n{soffice}\n")
        (bin_dir / "soffice").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/bin:/usr/bin" if soffice else str(bin_dir))
    monkeypatch.setattr(spreadsheet, "RECALC_TIMEOUT", 0.5)
    book = openpyxl.Workbook()
    sheet = book.active
    assert sheet is not None
    for row in [
        ["item", "amount"],
        ["a", 10],
        ["b", 20],
        ["c", 30],
        ["Total", total],
        ["Ratio", "=B5/#REF!"],
    ]:
        sheet.append(row)
    path = tmp_path / "model.xlsx"
    book.save(path)
    checks, computed = spreadsheet.run(
        {"artifact_path": str(path), "artifact_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    )
    assert computed is None
    return {str(c["name"]): c for c in checks}


@pytest.mark.parametrize("soffice", SOFFICES.values(), ids=SOFFICES.keys())
def test_a_failed_recalc_is_the_verifiers_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, soffice: str | None
) -> None:
    checks = _checks(tmp_path, monkeypatch, soffice, "=SUM(B2:B4)")
    assert checks["suite:recalc"]["status"] == "skip", checks
    assert "cells:evaluable" not in checks, "a formula nobody computed is never judged"
    verdict = pipeline.verdict_of([Check.model_validate(c) for c in checks.values()])
    assert verdict is not Verdict.FAIL


def test_the_checks_that_need_no_computing_still_judge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    short = _checks(tmp_path / "short", monkeypatch, None, "=SUM(B2:B3)")
    assert short["cross-foot:totals"]["status"] == "fail"
    assert "off by 30" in str(short["cross-foot:totals"]["actual"])
    hardcoded = _checks(tmp_path / "hardcoded", monkeypatch, None, 60)
    assert hardcoded["hardcode:totals"]["status"] == "fail"
