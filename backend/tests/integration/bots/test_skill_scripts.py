# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The document-workshop skill checkers, against real files in a real frame (ADR-021).

Each skill bundles a pre-publish checker (`scripts/check_*.py`) that runs INSIDE the frame with
the frame-pinned toolchain - openpyxl, python-pptx, python-docx, pypdf live there and not in
this venv, so the fixtures are built in-frame and the checkers are exercised exactly where they
run in production (the `test_docx_suite` pattern). Two directions per checker, same as the
verifier suites they mirror: a planted defect must be NAMED (exit 1), and a clean file must
pass (exit 0) - a checker that flags correct work is one an agent learns to skip.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from tests.support.paths import BACKEND

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-b")]

SKILLS = BACKEND / "skills"

# Builds every fixture in one exec: xlsx (clean + hardcoded total + short range), pptx (clean +
# off-slide shape), docx (structured + empty), pdf (real + not-a-pdf).
_BUILD = r"""
import openpyxl
from pptx import Presentation
from pptx.util import Inches
import docx
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate

def sheet(name, total):
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Model"
    ws["A1"], ws["B1"] = "item", "amount"
    for i, v in enumerate([10, 20, 30]): ws[f"A{i+2}"], ws[f"B{i+2}"] = f"row{i}", v
    ws["A5"], ws["B5"] = "Total", total
    wb.save(f"/work/{name}.xlsx")
sheet("wb-clean", "=SUM(B2:B4)")
sheet("wb-hardcoded", 60)
sheet("wb-short", "=SUM(B2:B3)")

def deck(name, left_in):
    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "One point"
    slide.shapes.add_textbox(Inches(left_in), Inches(2), Inches(4), Inches(1)) \
        .text_frame.text = "supporting line"
    prs.save(f"/work/{name}.pptx")
deck("deck-clean", 1)
deck("deck-offslide", 12.5)

d = docx.Document()
d.add_heading("Findings", level=1)
d.add_paragraph("Revenue grew.")
d.add_paragraph("first item", style="List Bullet")
d.save("/work/doc-clean.docx")
docx.Document().save("/work/doc-empty.docx")

doc = SimpleDocTemplate("/work/pdf-clean.pdf", pagesize=letter)
doc.build([Paragraph("Revenue reached $4.2M in Q3.", getSampleStyleSheet()["BodyText"])])
open("/work/pdf-broken.pdf", "w").write("not a pdf at all")
print("built")
"""

CHECKERS = {
    "model": "check_workbook.py",
    "deck": "check_deck.py",
    "docx": "check_docx.py",
    "pdf": "check_pdf.py",
}


@pytest.fixture
async def frame() -> AsyncIterator[DockerFrame]:
    ensure_image()
    fr = DockerFrame(task_id=f"test-skill-scripts-{uuid.uuid4().hex[:8]}")
    await fr.create(allow_domains=[])
    try:
        await fr.write_file("/work/build.py", _BUILD)
        result = await fr.exec("/usr/local/bin/python3 /work/build.py")
        assert result.ok, f"fixture build failed: {result.stderr}"
        for skill, script in CHECKERS.items():
            body = (SKILLS / skill / "scripts" / script).read_bytes()
            await fr.exec(f"mkdir -p /skills/{skill}/scripts")
            await fr.write_bytes(f"/skills/{skill}/scripts/{script}", body, mode=0o444, owner="system")
        yield fr
    finally:
        await fr.destroy()


async def _run(frame: DockerFrame, skill: str, target: str) -> tuple[int, str]:
    script = CHECKERS[skill]
    result = await frame.exec(f"/usr/local/bin/python3 /skills/{skill}/scripts/{script} {target}")
    return result.exit_code, result.stdout + result.stderr


async def test_check_workbook_passes_a_footing_model(frame: DockerFrame) -> None:
    code, out = await _run(frame, "model", "/work/wb-clean.xlsx")
    assert code == 0, out


async def test_check_workbook_names_a_hardcoded_total(frame: DockerFrame) -> None:
    code, out = await _run(frame, "model", "/work/wb-hardcoded.xlsx")
    assert code == 1
    assert "hardcoded" in out


async def test_check_workbook_names_a_short_changed_range(frame: DockerFrame) -> None:
    code, out = await _run(frame, "model", "/work/wb-short.xlsx")
    assert code == 1
    assert "stops short" in out


async def test_check_deck_passes_a_clean_deck_and_dumps_its_text(frame: DockerFrame) -> None:
    code, out = await _run(frame, "deck", "/work/deck-clean.pptx")
    assert code == 0, out
    assert "One point" in out, "the per-slide text dump is the read-your-own-deck half"


async def test_check_deck_names_an_offslide_shape(frame: DockerFrame) -> None:
    code, out = await _run(frame, "deck", "/work/deck-offslide.pptx")
    assert code == 1
    assert "outside the slide" in out


async def test_check_docx_passes_a_structured_document(frame: DockerFrame) -> None:
    code, out = await _run(frame, "docx", "/work/doc-clean.docx")
    assert code == 0, out
    assert "Findings" in out, "the outline dump names the headings"


async def test_check_docx_refuses_an_empty_body(frame: DockerFrame) -> None:
    code, out = await _run(frame, "docx", "/work/doc-empty.docx")
    assert code == 1
    assert "empty" in out


async def test_check_pdf_passes_a_text_pdf(frame: DockerFrame) -> None:
    code, out = await _run(frame, "pdf", "/work/pdf-clean.pdf")
    assert code == 0, out
    assert "$4.2M" in out, "the extracted head proves the text is real"


async def test_check_pdf_refuses_bytes_that_are_not_a_pdf(frame: DockerFrame) -> None:
    code, _out = await _run(frame, "pdf", "/work/pdf-broken.pdf")
    assert code == 1
