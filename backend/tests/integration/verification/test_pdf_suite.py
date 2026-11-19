# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The document suite against real `.pdf` files, in a real frame (ADR-021).

pdfplumber and reportlab live in the frame image only, so the fixtures are built there and the
extraction runs there - the suite is exercised exactly where it runs in production. The
property: a `.pdf`'s grounding checks run over its EXTRACTED text (it used to early-return
SKIP, and a judge could green-tick a file nothing had read), so a fabricated figure inside a
PDF now fails exactly like one in markdown - and a PDF with no text at all fails extraction
instead of shipping unexamined.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.verification import pipeline
from prismoid.schemas.claims import Grounding
from prismoid.schemas.outputs import VerifierSuite
from prismoid.schemas.verification import CheckStatus

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-c")]

SOURCE = b"Q3 revenue was $41.6M. Headcount reached 312."

_BUILD = r"""
import hashlib, json
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate

out = {}
def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()

def report(name, sentence):
    path = f"/work/{name}.pdf"
    doc = SimpleDocTemplate(path, pagesize=letter)
    styles = getSampleStyleSheet()
    doc.build([Paragraph("Quarterly summary", styles["Title"]),
               Paragraph(sentence, styles["BodyText"])])
    out[name] = sha(path)

report("grounded", "Revenue was $41.6M and headcount was 312.")
report("fabricated", "Revenue was $77.7M this quarter.")

c = canvas.Canvas("/work/textless.pdf", pagesize=letter)
c.rect(100, 500, 200, 100, fill=1)   # pixels-only content: a page with no extractable words
c.showPage(); c.save()
out["textless"] = sha("/work/textless.pdf")

with open("/work/corrupt.pdf", "wb") as fh:   # a PDF header on bytes no parser can open
    fh.write(b"%PDF-1.7 not really a pdf but exactly as unreadable")
out["corrupt"] = sha("/work/corrupt.pdf")
print(json.dumps(out))
"""


@pytest.fixture
async def built() -> AsyncIterator[tuple[DockerFrame, dict[str, str]]]:
    ensure_image()
    frame = DockerFrame(task_id=f"test-pdf-suite-{uuid.uuid4().hex[:8]}")
    await frame.create(allow_domains=[])
    try:
        await frame.write_file("/work/build.py", _BUILD)
        result = await frame.exec("/usr/local/bin/python3 /work/build.py")
        assert result.ok, f"fixture build failed: {result.stderr}"
        shas = json.loads(result.stdout.strip().splitlines()[-1])
        await frame.write_bytes("/task/inputs/brief.md", SOURCE, mode=0o444, owner="system")
        yield frame, shas
    finally:
        await frame.destroy()


async def _run(frame: DockerFrame, name: str, sha: str) -> dict[str, CheckStatus]:
    checks = await pipeline.run_suite(
        frame,
        VerifierSuite.DOCUMENT,
        artifact_path=f"/work/{name}.pdf",
        artifact_sha256=sha,
        sources=["/task/inputs/brief.md"],
        allow_domains=[],
        grounding=Grounding.NUMBERS,  # the suite's own figure grounding, the path the gate can select
    )
    return {c.name: c.status for c in checks}


async def test_a_grounded_pdf_passes_on_its_extracted_text(
    built: tuple[DockerFrame, dict[str, str]],
) -> None:
    frame, shas = built
    status = await _run(frame, "grounded", shas["grounded"])
    assert status["format:extraction"] is CheckStatus.PASS
    assert status["numbers:grounded"] is CheckStatus.PASS, "$41.6M and 312 are in the brief"


async def test_a_fabricated_figure_in_a_pdf_fails_grounding(
    built: tuple[DockerFrame, dict[str, str]],
) -> None:
    frame, shas = built
    status = await _run(frame, "fabricated", shas["fabricated"])
    assert status["numbers:grounded"] is CheckStatus.FAIL, "$77.7M appears in no source"


async def test_a_textless_pdf_fails_extraction_instead_of_shipping_unexamined(
    built: tuple[DockerFrame, dict[str, str]],
) -> None:
    frame, shas = built
    status = await _run(frame, "textless", shas["textless"])
    assert status["format:extraction"] is CheckStatus.FAIL
    assert "numbers:grounded" not in status, "prose checks must not run when nothing extracted"


async def test_a_corrupt_pdf_stream_fails_extraction_with_the_parser_present(
    built: tuple[DockerFrame, dict[str, str]],
) -> None:
    """The author's broken bytes are a FAIL - here, with the parser genuinely installed, so the
    failure is provably the stream's and not a missing extractor (that path is the unit test's:
    a venv without the parser reads as `suite:extractor` SKIP, never an artifact-shaped FAIL)."""
    frame, shas = built
    status = await _run(frame, "corrupt", shas["corrupt"])
    assert status["format:extraction"] is CheckStatus.FAIL
    assert "suite:extractor" not in status, "the parser exists here - the gap check must not fire"
    assert "numbers:grounded" not in status
