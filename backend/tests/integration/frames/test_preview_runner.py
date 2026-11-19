# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The harvest-time preview + rendition runner, against real files in a real frame (ADR-022/023).

The runner is a static script copied into the frame (the publish-runner hand-off), so it is
tested exactly where it runs: fixtures built in-frame with the frame's own toolchain, one exec,
one JSON file back. Per kind: a workbook's formula cells come back COMPUTED in the grid AND
RECALCULATED in the LibreOffice HTML rendition (a fresh openpyxl file carries no cached values
- both paths must do their own arithmetic or show holes), a docx comes back as styled blocks
plus a genuine PDF rendition, a deck as slides plus PDF, a page-shaped tar as its own HTML, any
other tar as a listing - and a file that will not parse yields NOTHING rather than a failed
harvest.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator

import pytest

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.ai.frames.preview import ARGS_PATH, OUT_PATH, PREVIEW_RUNNER, RUNNER_PATH, preview_args

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-a")]

_BUILD = r"""
import tarfile, os
import openpyxl
import docx
from pptx import Presentation
from pptx.util import Inches

wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Model"
ws["A1"], ws["B1"] = "item", "amount"
for i, v in enumerate([10, 20, 30]): ws[f"A{i+2}"], ws[f"B{i+2}"] = f"row{i}", v
ws["A5"], ws["B5"] = "Total", "=SUM(B2:B4)"
ws["A6"], ws["B6"] = "Grand", "=SUM(B2:B5)"  # a formula over a formula
wb.save("/work/model.xlsx")

d = docx.Document()
d.add_heading("Findings", level=1)
d.add_paragraph("Revenue grew.")
d.add_paragraph("first item", style="List Bullet")
t = d.add_table(rows=1, cols=2); t.rows[0].cells[0].text = "k"; t.rows[0].cells[1].text = "v"
d.save("/work/report.docx")

prs = Presentation()
prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
s = prs.slides.add_slide(prs.slide_layouts[5])
s.shapes.title.text = "One point"
s.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1)).text_frame.text = "a line"
prs.save("/work/deck.pptx")

open("/work/page.html", "w").write("<!doctype html><html lang=\"en\"><head><title>Solo</title></head><body><h1>Standalone</h1></body></html>")

os.makedirs("/work/site", exist_ok=True)
open("/work/site/index.html", "w").write("<!doctype html><html lang=\"en\"><head><title>T</title></head><body><h1>Hi</h1></body></html>")
open("/work/site/test.sh", "w").write("#!/bin/sh\ntrue\n")
with tarfile.open("/work/site.tar", "w") as tf: tf.add("/work/site", arcname="site")

os.makedirs("/work/proj", exist_ok=True)
open("/work/proj/main.py", "w").write("print('hi')\n")
with tarfile.open("/work/proj.tar", "w") as tf: tf.add("/work/proj", arcname="proj")

open("/work/broken.xlsx", "w").write("not a zip")
print("built")
"""

ENTRIES = [
    {"path": "/work/model.xlsx", "type": "spreadsheet"},
    {"path": "/work/report.docx", "type": "document"},
    {"path": "/work/page.html", "type": "document"},
    {"path": "/work/deck.pptx", "type": "deck"},
    {"path": "/work/site.tar", "type": "code"},
    {"path": "/work/proj.tar", "type": "code"},
    {"path": "/work/broken.xlsx", "type": "spreadsheet"},
]

Made = dict[str, dict[str, object]]


@pytest.fixture
async def made() -> AsyncIterator[tuple[DockerFrame, Made]]:
    ensure_image()
    frame = DockerFrame(task_id=f"test-preview-runner-{uuid.uuid4().hex[:8]}")
    await frame.create(allow_domains=[])
    try:
        await frame.write_file("/work/build.py", _BUILD)
        built = await frame.exec("/usr/local/bin/python3 /work/build.py")
        assert built.ok, f"fixture build failed: {built.stderr}"
        await frame.write_bytes(RUNNER_PATH, PREVIEW_RUNNER.encode(), mode=0o444, owner="system")
        await frame.write_bytes(
            ARGS_PATH, json.dumps(preview_args(ENTRIES, "n1")).encode(), mode=0o444, owner="system"
        )
        ran = await frame.exec(f"python3 {RUNNER_PATH} {ARGS_PATH}", timeout=150.0)
        assert ran.ok, f"runner failed: {ran.stderr}"
        parsed = json.loads(await frame.read_bytes(OUT_PATH))
        assert parsed["nonce"] == "n1", "OUT must echo the run's nonce - the staleness guard"
        yield frame, parsed["made"]
    finally:
        await frame.destroy()


def preview_of(out: Made, path: str) -> dict[str, object]:
    preview = out[path]["preview"]
    assert isinstance(preview, dict), f"{path} has no preview"
    return preview


# ── the previews ──────────────────────────────────────────────────────────────────────────────


async def test_sheet_preview_shows_computed_totals(made: tuple[DockerFrame, Made]) -> None:
    sheet = preview_of(made[1], "/work/model.xlsx")
    flat = [c for s in sheet["sheets"] for row in s["rows"] for c in row]  # pyright: ignore[reportGeneralTypeIssues]
    assert sheet["kind"] == "sheet"
    assert 60 in flat, "the =SUM total must appear as its COMPUTED value, not formula text"
    assert 120 in flat, "a formula over another formula computes too (LibreOffice, not a hand evaluator)"


async def test_document_preview_carries_styled_blocks_and_tables(made: tuple[DockerFrame, Made]) -> None:
    doc = preview_of(made[1], "/work/report.docx")
    assert doc["kind"] == "document"
    styles = [(b.get("style"), b.get("text")) for b in doc["blocks"]]  # pyright: ignore[reportGeneralTypeIssues]
    assert ("h1", "Findings") in styles
    assert ("li", "first item") in styles
    assert any(b.get("table") == [["k", "v"]] for b in doc["blocks"])  # pyright: ignore[reportGeneralTypeIssues]


async def test_deck_preview_carries_titles_and_lines(made: tuple[DockerFrame, Made]) -> None:
    deck = preview_of(made[1], "/work/deck.pptx")
    assert deck["kind"] == "deck"
    assert deck["slides"][0]["title"] == "One point"  # pyright: ignore[reportIndexIssue]
    assert "a line" in deck["slides"][0]["lines"]  # pyright: ignore[reportIndexIssue]


async def test_a_page_shaped_tar_previews_as_its_own_html(made: tuple[DockerFrame, Made]) -> None:
    page = preview_of(made[1], "/work/site.tar")
    assert page["kind"] == "page"
    assert str(page["html"]).startswith("<!doctype html>")


async def test_an_html_document_previews_as_its_own_page(made: tuple[DockerFrame, Made]) -> None:
    # A single-file .html DOCUMENT rides the same sandboxed-cage shape the code
    # tar's index.html always did - the page is the presentation.
    page = preview_of(made[1], "/work/page.html")
    assert page["kind"] == "page"
    assert "Standalone" in str(page["html"])


async def test_a_plain_code_tar_previews_as_a_listing(made: tuple[DockerFrame, Made]) -> None:
    code = preview_of(made[1], "/work/proj.tar")
    assert code["kind"] == "code"
    files = {f["path"]: f for f in code["files"]}  # pyright: ignore[reportGeneralTypeIssues]
    assert files["main.py"]["text"] == "print('hi')\n"


async def test_unparseable_bytes_never_fail_and_never_fake_a_preview(
    made: tuple[DockerFrame, Made],
) -> None:
    """openpyxl refuses the fake workbook → no preview, and the runner carried on (the other
    eight tests prove that). LibreOffice, famously lenient, MAY still import the bytes as text
    and mint a rendition - that is its honest read of the file, not a failure; what must never
    happen is a parsed-preview conjured from bytes the parser refused."""
    entry = made[1].get("/work/broken.xlsx")
    assert entry is None or entry.get("preview") is None


# ── the renditions: LibreOffice's faithful render, made beside the preview ────────────────────


async def _rendition_bytes(frame: DockerFrame, out: Made, path: str) -> tuple[bytes, str]:
    rendition = out[path]["rendition"]
    assert isinstance(rendition, dict), f"{path} has no rendition"
    data = await frame.read_bytes(str(rendition["file"]))
    return data, str(rendition["media"])


async def test_docx_rendition_is_a_real_pdf(made: tuple[DockerFrame, Made]) -> None:
    data, media = await _rendition_bytes(made[0], made[1], "/work/report.docx")
    assert media == "application/pdf"
    assert data.startswith(b"%PDF"), "the rendition must be a genuine PDF stream"


async def test_deck_rendition_is_a_real_pdf(made: tuple[DockerFrame, Made]) -> None:
    data, media = await _rendition_bytes(made[0], made[1], "/work/deck.pptx")
    assert media == "application/pdf"
    assert data.startswith(b"%PDF")


async def test_xlsx_rendition_is_styled_html_with_recalculated_formulas(
    made: tuple[DockerFrame, Made],
) -> None:
    """THE recalc proof: an openpyxl-written workbook carries no cached values, so the =SUM
    total appears in LibreOffice's HTML export only if the forced-recalc profile actually
    recalculated. '60' present = the rendition shows numbers, not holes."""
    data, media = await _rendition_bytes(made[0], made[1], "/work/model.xlsx")
    assert media == "text/html"
    text = data.decode("utf-8", errors="replace")
    assert "<table" in text.lower(), "Calc's HTML export is a styled table"
    # The total must be CELL CONTENT (>60<, whatever whitespace LO wraps it in) - a bare "60"
    # would also match attribute soup (width="60") and let a broken recalc stay green.
    assert re.search(r">\s*60\s*<", text), "the =SUM total must be RECALCULATED into the render"


# ── the staleness guard: OUT is reborn per run, named by its nonce ────────────────────────────


async def test_out_is_reborn_per_run_with_its_nonce(made: tuple[DockerFrame, Made]) -> None:
    """/tmp persists across harvests in a long-lived frame. A second run must replace OUT under
    its OWN nonce - the worker discards any OUT whose nonce is not the one it just sent, so a
    re-published path can never wear the previous version's sidecar."""
    frame = made[0]
    args2 = "/tmp/.plane-preview-args-2.json"
    await frame.write_bytes(
        args2,
        json.dumps(preview_args([{"path": "/work/model.xlsx", "type": "spreadsheet"}], "n2")).encode(),
        mode=0o444,
        owner="system",
    )
    ran = await frame.exec(f"python3 {RUNNER_PATH} {args2}", timeout=150.0)
    assert ran.ok, f"second run failed: {ran.stderr}"
    parsed = json.loads(await frame.read_bytes(OUT_PATH))
    assert parsed["nonce"] == "n2"
    assert set(parsed["made"]) == {"/work/model.xlsx"}, "OUT is THIS run's output, nothing stale"
