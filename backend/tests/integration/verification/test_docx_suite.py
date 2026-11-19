# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The document suite against a real `.docx`, in a real frame (ADR-015).

python-docx lives in the frame image only, so both the fixture build and the extraction run
there - the suite is exercised exactly where it runs in production. The property: a `.docx`'s
grounding checks run over its EXTRACTED words (they used to run over zip bytes and pass
vacuously), so a fabricated figure inside a Word document now fails exactly like one in
markdown.
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

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-a")]

SOURCE = b"Q3 revenue was $41.6M. Headcount reached 312."

_BUILD = r"""
import docx, hashlib, json
out = {}
def save(name, sentence):
    d = docx.Document()
    d.add_paragraph("Quarterly summary")
    d.add_paragraph(sentence)
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text = "Revenue"
    t.rows[0].cells[1].text = "$41.6M"
    path = f"/work/{name}.docx"
    d.save(path)
    out[name] = hashlib.sha256(open(path, "rb").read()).hexdigest()
save("grounded", "Revenue was $41.6M and headcount was 312.")
save("fabricated", "Revenue was 99.9 this quarter.")
print(json.dumps(out))
"""


@pytest.fixture
async def built() -> AsyncIterator[tuple[DockerFrame, dict[str, str]]]:
    ensure_image()
    frame = DockerFrame(task_id=f"test-docx-suite-{uuid.uuid4().hex[:8]}")
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
        artifact_path=f"/work/{name}.docx",
        artifact_sha256=sha,
        sources=["/task/inputs/brief.md"],
        allow_domains=[],
        grounding=Grounding.NUMBERS,  # the suite's own figure grounding, the path the gate can select
    )
    return {c.name: c.status for c in checks}


async def test_a_grounded_docx_passes_on_its_extracted_words(
    built: tuple[DockerFrame, dict[str, str]],
) -> None:
    frame, shas = built
    status = await _run(frame, "grounded", shas["grounded"])
    assert status["format:extraction"] is CheckStatus.PASS
    assert status["numbers:grounded"] is CheckStatus.PASS, "$41.6M and 312 are in the brief"


async def test_a_fabricated_figure_in_a_docx_fails_grounding(
    built: tuple[DockerFrame, dict[str, str]],
) -> None:
    frame, shas = built
    status = await _run(frame, "fabricated", shas["fabricated"])
    assert status["numbers:grounded"] is CheckStatus.FAIL, "99.9 appears in no source"


# The interleaved golden: paragraph, table, list item, table - read back by the one reader, in order.
_GOLDEN = r"""
import sys, docx
sys.path.insert(0, "/verify")
from extract import extract
d = docx.Document()
d.add_heading("Q3 breakdown", 1)
d.add_paragraph("Revenue came from four regions.")
t = d.add_table(rows=2, cols=2)
t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Region", "Revenue ($M)"
t.rows[1].cells[0].text, t.rows[1].cells[1].text = "North", "12.4"
d.add_paragraph("Costs were led by payroll.", style="List Bullet")
t = d.add_table(rows=1, cols=2)
t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Payroll", "$14.2M"
d.save("/work/golden.docx")
print(extract("/work/golden.docx").markdown)
"""


async def test_the_reader_keeps_a_docx_in_document_order(built: tuple[DockerFrame, dict[str, str]]) -> None:
    frame, _ = built
    await frame.write_bytes(
        "/verify/extract.py", (pipeline.SUITES / "extract.py").read_bytes(), owner="system"
    )
    await frame.write_file("/work/golden.py", _GOLDEN)
    result = await frame.exec("/usr/local/bin/python3 /work/golden.py")
    assert result.ok, result.stderr
    assert result.stdout.strip() == (
        "## Q3 breakdown\n\n"
        "Revenue came from four regions.\n\n"
        "| Region | Revenue ($M) |\n| --- | --- |\n| North | 12.4 |\n\n"
        "- Costs were led by payroll.\n\n"
        "| Payroll | $14.2M |\n| --- | --- |"
    )
