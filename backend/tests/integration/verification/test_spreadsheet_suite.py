# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The spreadsheet suite, against real `.xlsx` in a real frame.

The suite runs *in the frame* (openpyxl is frame-only), so it is tested there too: fixtures are
built inside a container using its own openpyxl, then the **real pipeline** copies the suite in and
runs it. This is the honest test - the suite is exercised exactly where it runs in production - and it
needs no frame-only library in the worker venv.

Two directions, as the document suite learned the hard way: a planted-wrong model must FAIL (a total
that does not foot, a hardcoded total, a formula that computes to an error), and a clean model must PASS.
A verifier that fails correct work is one somebody switches off, so the clean case is not an afterthought:
a 12-month cash flow of chained, cross-sheet formulas (the shape that false-FAILed 52 formulas live) and a
SUMIF/VLOOKUP golden are computed by LibreOffice and pass.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.verification import pipeline
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.verification import Check, CheckStatus

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-c")]

# Builds the fixtures in /work and prints {name: sha256}. A column B2:B4 of 10/20/30 (sums to 60):
#   clean     - B5 = =SUM(B2:B4)         (foots)
#   short     - B5 = =SUM(B2:B3)         (the planted bad formula: misses B4, sums to 30)
#   hardcoded - B5 = 60 as a literal     (right number, but nothing recomputes it)
#   ref, text - clean plus B6 = =B5/#REF!, or =A1*12 over the text "item"
#   golden    - SUMIF and VLOOKUP on a Summary sheet over an Inputs sheet
#   cashflow  - 12 months of chained formulas over an Assumptions sheet, 65 in all
_BUILD = r"""
import openpyxl, hashlib, json
out = {}
def save(name, wb):
    path = f"/work/{name}.xlsx"; wb.save(path)
    out[name] = hashlib.sha256(open(path,"rb").read()).hexdigest()
def model(total, extra=None):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Model"
    ws["A1"], ws["B1"] = "item", "amount"
    for i, v in enumerate([10, 20, 30]): ws[f"A{i+2}"], ws[f"B{i+2}"] = f"row{i}", v
    ws["A5"] = "Total"; ws["B5"] = total
    if extra: ws["A6"], ws["B6"] = "Ratio", extra
    return wb
save("clean", model("=SUM(B2:B4)"))
save("short", model("=SUM(B2:B3)"))
save("hardcoded", model(60))
save("ref", model("=SUM(B2:B4)", "=B5/#REF!"))
save("text", model("=SUM(B2:B4)", "=A1*12"))
wb = openpyxl.Workbook(); inputs = wb.active; inputs.title = "Inputs"
inputs.append(["Region", "Revenue"])
for row in [["North", 10], ["South", 20], ["East", 30], ["West", 40]]: inputs.append(row)
summary = wb.create_sheet("Summary")
summary.append(["South", '=SUMIF(Inputs!A2:A5,"South",Inputs!B2:B5)'])
summary.append(["East", '=VLOOKUP("East",Inputs!$A$2:$B$5,2,FALSE)'])
save("golden", wb)
wb = openpyxl.Workbook(); a = wb.active; a.title = "Assumptions"
for row in [["Growth", 0.02], ["COGS ratio", 0.3], ["Opex", 400], ["Start revenue", 1000]]: a.append(row)
cash = wb.create_sheet("Cash")
cash.append(["", *[f"M{m}" for m in range(1, 13)], "Year"])
for label in ["Revenue", "COGS", "Gross", "Opex", "Net"]: cash.append([label])
for m in range(12):
    c, p = openpyxl.utils.get_column_letter(m + 2), openpyxl.utils.get_column_letter(m + 1)
    cash[f"{c}2"] = "=Assumptions!$B$4" if m == 0 else f"={p}2*(1+Assumptions!$B$1)"
    cash[f"{c}3"], cash[f"{c}4"] = f"={c}2*Assumptions!$B$2", f"={c}2-{c}3"
    cash[f"{c}5"], cash[f"{c}6"] = "=Assumptions!$B$3", f"={c}4-{c}5"
for r in range(2, 7): cash[f"N{r}"] = f"=SUM(B{r}:M{r})"
save("cashflow", wb)
print(json.dumps(out))
"""


@pytest.fixture
async def built() -> AsyncIterator[tuple[DockerFrame, dict[str, str]]]:
    ensure_image()
    frame = DockerFrame(task_id=f"test-spreadsheet-suite-{uuid.uuid4().hex[:8]}")
    await frame.create(allow_domains=[])
    try:
        await frame.write_file("/work/build.py", _BUILD)
        result = await frame.exec("/usr/local/bin/python3 /work/build.py")
        assert result.ok, f"fixture build failed: {result.stderr}"
        shas = json.loads(result.stdout.strip().splitlines()[-1])
        yield frame, shas
    finally:
        await frame.destroy()


async def _checks(frame: DockerFrame, name: str, sha: str) -> dict[str, Check]:
    checks = await pipeline.run_suite(
        frame,
        VerifierSuite.SPREADSHEET,
        artifact_path=f"/work/{name}.xlsx",
        artifact_sha256=sha,
        sources=[],
        allow_domains=[],
    )
    return {c.name: c for c in checks}


async def _run(frame: DockerFrame, name: str, sha: str) -> dict[str, CheckStatus]:
    return {check: c.status for check, c in (await _checks(frame, name, sha)).items()}


async def test_a_clean_model_passes(built: tuple[DockerFrame, dict[str, str]]) -> None:
    frame, shas = built
    status = await _run(frame, "clean", shas["clean"])
    assert status["bytes:sha256"] is CheckStatus.PASS
    assert status["cross-foot:totals"] is CheckStatus.PASS, "a =SUM over its whole column must foot"
    assert status["cells:evaluable"] is CheckStatus.PASS
    assert status["hardcode:totals"] is CheckStatus.PASS


async def test_a_short_changed_sum_fails_to_foot(built: tuple[DockerFrame, dict[str, str]]) -> None:
    # The planted bad formula: =SUM(B2:B3) over a B2:B4 column. Looks honest, sums to the wrong number.
    frame, shas = built
    status = await _run(frame, "short", shas["short"])
    assert status["cross-foot:totals"] is CheckStatus.FAIL, "a SUM that misses a row must be caught"


async def test_a_hardcoded_total_is_flagged(built: tuple[DockerFrame, dict[str, str]]) -> None:
    # Right number (60), but a literal - nothing recomputes it, which is where a fabrication hides.
    frame, shas = built
    status = await _run(frame, "hardcoded", shas["hardcoded"])
    assert status["hardcode:totals"] is CheckStatus.FAIL


async def test_a_tampered_digest_is_caught(built: tuple[DockerFrame, dict[str, str]]) -> None:
    # bytes:sha256 is the plumbing check - the wrong bytes make every other check meaningless.
    frame, _shas = built
    status = await _run(frame, "clean", "0" * 64)
    assert status["bytes:sha256"] is CheckStatus.FAIL


async def test_the_suite_leaves_a_computed_preview_sidecar(
    built: tuple[DockerFrame, dict[str, str]],
) -> None:
    """The suite writes /work/preview.json with COMPUTED values - the numbers the
    preview shows are the numbers the verifier re-derived, which is the whole reason the sidecar
    is generated here and not in the worker."""
    import json as jsonlib

    frame, shas = built
    await _run(frame, "clean", shas["clean"])
    raw = await frame.read_bytes("/work/preview.json")
    preview = jsonlib.loads(raw)
    assert preview["kind"] == "sheet"
    [sheet] = preview["sheets"]
    assert sheet["name"] == "Model"
    flat = [c for row in sheet["rows"] for c in row]
    assert 60 in flat or 60.0 in flat, "the =SUM total must appear as its COMPUTED value"


async def test_a_sumif_vlookup_golden_is_computed_not_skipped(
    built: tuple[DockerFrame, dict[str, str]],
) -> None:
    frame, shas = built
    checks = await _checks(frame, "golden", shas["golden"])
    assert checks["cells:evaluable"].status is CheckStatus.PASS, checks["cells:evaluable"]
    assert checks["cells:evaluable"].detail == "2 formula(s) compute without an error"
    summary = next(
        s
        for s in json.loads(await frame.read_bytes("/work/preview.json"))["sheets"]
        if s["name"] == "Summary"
    )
    assert [row[:2] for row in summary["rows"]] == [["South", 20], ["East", 30]], "LibreOffice's values"


async def test_a_chained_cross_sheet_cash_flow_passes(built: tuple[DockerFrame, dict[str, str]]) -> None:
    # The live false FAIL: formulas reading formulas, and an Assumptions sheet read from another sheet.
    frame, shas = built
    checks = await _checks(frame, "cashflow", shas["cashflow"])
    assert checks["cells:evaluable"].status is CheckStatus.PASS, checks["cells:evaluable"]
    assert checks["cells:evaluable"].detail == "65 formula(s) compute without an error"
    assert checks["hardcode:totals"].status is CheckStatus.PASS


@pytest.mark.parametrize(("name", "error"), [("ref", "#REF!"), ("text", "#VALUE!")])
async def test_a_formula_that_computes_to_an_error_fails(
    built: tuple[DockerFrame, dict[str, str]], name: str, error: str
) -> None:
    frame, shas = built
    evaluable = (await _checks(frame, name, shas[name]))["cells:evaluable"]
    assert evaluable.status is CheckStatus.FAIL
    assert evaluable.actual is not None and evaluable.actual.endswith(error), evaluable.actual
