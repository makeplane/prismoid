# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The house engine, proven where it runs (ADR-034): three `NN-*.md` fixtures through the
assemble walkers INSIDE the frame, then `plane_house.render` + `geometry` on the results -
the exact pass the author runs before publishing and the QA precheck runs after harvest.
Asserts the customer-facing laws: 16:9, no text/picture collision, density splits, no
internal ids on any face (notes/alt text carry them), page numbers + cover + repeating table
header in the docx, a LibreOffice PDF that rasterizes to PNGs, and the planted-defect
direction (an off-slide shape + a face id are NAMED)."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from prismoid.ai.frames.docker_driver import DockerFrame, ensure_image
from prismoid.operations import qa
from tests.support.paths import BACKEND

# loop_scope: the session default (pyproject) - the old per-module pin predated it and a
# narrower loop than the engine's would re-open the cross-loop asyncpg bug it worked around.
pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-b")]

SCRIPTS = BACKEND / "skills" / "assemble" / "scripts"

_SECTIONS = {
    "01-overview.md": (
        "# Overview\n\n"
        "## Slide: The quarter in one line\n- Services grew 23% while hardware held flat across every region we sell into\n"
        "- Gross margin expanded 140 bps on mix\n- Churn fell to 1.1% monthly\n- Net revenue retention reached 118%\n"
        "- Headcount flat at 412\n- Cash up $3.1M\n- Two enterprise logos signed in EMEA\n- Pipeline coverage 3.2x\n\n"
        "Notes: Lead with the services story.\n\n"
        "## Slide: Revenue by region\n| Region | Q2 | Q3 |\n| --- | --- | --- |\n| EMEA | 1.2 | 1.4 |\n| AMER | 2.0 | 2.3 |\n| APAC | 0.5 | 0.6 |\n"
    ),
    "02-market.md": (
        "# Market\n\n"
        "## Slide: Churn is falling\n- Monthly churn 1.1%, down from 1.6%\n- Driven by onboarding changes\n\n"
        "Figure (fig-s02-1): Monthly churn, trailing twelve months\n\n"
        "## Slide: Pipeline\n- Coverage 3.2x\n\nFigure (fig-s02-2): Pipeline by stage\n"
    ),
    "03-plan.md": "# Plan\n\nWe will double down on services. The plan has three parts and runs through Q4.\n\n- Hire twelve\n- Ship the connector\n",
}

_BUILD = r"""
import struct, zlib, os
def png(w, h, path):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))
    raw = b"".join(b"\x00" + bytes([30, 58, 95, 255]) * w for _ in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    open(path, "wb").write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
os.makedirs("/work/images", exist_ok=True)
png(1600, 900, "/work/images/fig-s02-1.png")   # fig-s02-2 deliberately missing → placeholder
print("fixtures ok")
"""

_PLANTED = r"""
from pptx import Presentation
from pptx.util import Inches
prs = Presentation(); prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
s = prs.slides.add_slide(prs.slide_layouts[5]); s.shapes.title.text = "Leaky"
s.shapes.add_textbox(Inches(12.5), Inches(2), Inches(4), Inches(1)).text_frame.text = "see fig-s01-9 for detail"
s.shapes.add_picture("/work/images/fig-s02-1.png", Inches(1), Inches(2), width=Inches(8))
s.shapes.add_textbox(Inches(1), Inches(2.5), Inches(6), Inches(1)).text_frame.text = "text on top of the picture"
prs.save("/work/planted.pptx")
print("planted")
"""


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def frame() -> AsyncIterator[DockerFrame]:
    ensure_image()
    fr = DockerFrame(task_id=f"test-house-engine-{uuid.uuid4().hex[:8]}")
    await fr.create(allow_domains=[])
    try:
        await fr.exec("mkdir -p /work/sections /skills/assemble/scripts")
        for name, body in _SECTIONS.items():
            await fr.write_file(f"/work/sections/{name}", body)
        for script in SCRIPTS.glob("*.py"):
            await fr.write_bytes(
                f"/skills/assemble/scripts/{script.name}", script.read_bytes(), mode=0o444, owner="system"
            )
        await qa.ship_precheck(fr)
        await fr.write_file("/work/fixtures.py", _BUILD)
        result = await fr.exec("python3 /work/fixtures.py")
        assert result.ok, result.stderr
        w = "/skills/assemble/scripts"
        for cmd in (
            f"python3 {w}/md_to_pptx.py --sections /work/sections --out /work/deck.pptx --title 'Q3 review' --subtitle 'Board pack' --images /work/images",
            f"python3 {w}/md_to_docx.py --sections /work/sections --out /work/report.docx --title 'Q3 review' --toc --images /work/images --total 3",
            f"python3 {w}/md_to_pdf.py --sections /work/sections --out /work/report.pdf --title 'Q3 review' --images /work/images --engine soffice",
        ):
            result = await fr.exec(cmd, timeout=300)
            assert result.ok, f"{cmd}\n{result.stdout}\n{result.stderr}"
            print(result.stdout)
        yield fr
    finally:
        await fr.destroy()


async def _py(frame: DockerFrame, code: str) -> str:
    await frame.write_file("/work/_probe.py", code)
    result = await frame.exec("python3 /work/_probe.py", timeout=300)
    assert result.ok, result.stdout + result.stderr
    return result.stdout


async def test_deck_is_widescreen_collision_free_and_splits(frame: DockerFrame) -> None:
    out = await _py(
        frame,
        "import json\nfrom plane_house import geometry\nfrom pptx import Presentation\n"
        "rows = geometry.deck_layout_checks('/work/deck.pptx')\n"
        "prs = Presentation('/work/deck.pptx')\n"
        "titles = [s.shapes.title.text for s in prs.slides if s.shapes.title is not None]\n"
        "print(json.dumps({'rows': rows, 'titles': titles, 'n': len(prs.slides), 'face': geometry.face_fig_ids('/work/deck.pptx'), 'hidden': geometry.hidden_fig_ids('/work/deck.pptx')}))",
    )
    data = json.loads(out.strip().splitlines()[-1])
    bad = {r["name"]: r["detail"] for r in data["rows"] if not r["ok"]}
    assert not bad, bad
    assert any(t.endswith("(cont.)") for t in data["titles"]), (
        "eight bullets must split onto a continuation slide"
    )
    assert data["face"] == [], "internal ids on the slide face"
    assert {"fig-s02-1", "fig-s02-2"} <= set(data["hidden"]), (
        "notes/alt text must carry every id, embedded or placeholder"
    )


async def test_docx_carries_the_house_furniture(frame: DockerFrame) -> None:
    out = await _py(
        frame,
        "import json, docx\nfrom plane_house import geometry\n"
        "d = docx.Document('/work/report.docx')\n"
        "xml = d.element.xml\n"
        "footer_xml = d.sections[0].footer._element.xml\n"
        "caps = [p.text for p in d.paragraphs if p.style.name == 'Caption']\n"
        "print(json.dumps({'font': d.styles['Normal'].font.name, 'margin': d.sections[0].left_margin / 914400,"
        " 'page_field': 'PAGE' in footer_xml, 'tblHeader': 'w:tblHeader' in xml, 'caps': caps,"
        " 'keywords': d.core_properties.keywords, 'face': geometry.face_fig_ids('/work/report.docx'),"
        " 'rows': geometry.docx_layout_checks('/work/report.docx'), 'breaks': xml.count('w:br w:type=\"page\"')}))",
    )
    data = json.loads(out.strip().splitlines()[-1])
    assert data["font"] == "Inter" and data["margin"] == pytest.approx(0.9)  # the side margin token
    assert data["page_field"], "the footer must carry a real PAGE field"
    assert data["tblHeader"], "the table header row must repeat across pages"
    assert any(c.startswith("Figure 1.") for c in data["caps"]), data["caps"]
    assert any("figure pending" in c for c in data["caps"]), (
        "a missing figure keeps a visible numbered placeholder"
    )
    assert "fig-s02-1" in (data["keywords"] or "") and data["face"] == []
    assert not [r for r in data["rows"] if not r["ok"]], data["rows"]
    assert data["breaks"] <= 2, "no page break per section - only the cover's and the Contents page's"


async def test_render_rasterizes_pages_and_pdf_twin_exists(frame: DockerFrame) -> None:
    result = await frame.exec(
        "python3 -m plane_house.render /work/deck.pptx --pages /work/pages-deck", timeout=300
    )
    assert result.exit_code == 0, result.stdout + result.stderr
    manifest = json.loads(await frame.read_file("/work/pages-deck/pages.json"))
    assert manifest["pages"] >= 8 and manifest["images"], manifest
    assert all(p.endswith(".png") for p in manifest["images"])
    head = await _py(frame, "print(open('/work/pages-deck/page-01.png','rb').read(8).hex())")
    assert head.strip().splitlines()[-1].startswith("89504e47"), "page-01.png is a real PNG"
    pdf_head = await frame.exec("head -c 5 /work/report.pdf")
    assert pdf_head.stdout.startswith("%PDF"), "md_to_pdf's soffice engine produced a real PDF"
    pages = await _py(frame, "from pypdf import PdfReader\nprint(len(PdfReader('/work/report.pdf').pages))")
    assert int(pages.strip().splitlines()[-1]) >= 2


async def test_precheck_gains_layout_rows_and_finds_hidden_ids(frame: DockerFrame) -> None:
    job = {
        "deliverable": "/work/deck.pptx",
        "outline": None,
        "kind": "deck",
        "unit": "slides",
        "target_units": 0,
        "target_words": 0,
        "tolerance": 0.4,
        "figure_ids": ["fig-s02-1", "fig-s02-2"],
    }
    await frame.write_file("/work/qa_job.json", json.dumps(job))
    result = await frame.exec("python3 /work/qa_precheck.py < /work/qa_job.json", timeout=330)
    assert result.exit_code == 0, result.stderr
    checks = {c["name"]: c for c in json.loads(result.stdout.strip().splitlines()[-1])["checks"]}
    assert checks["qa:figures"]["ok"], checks["qa:figures"]
    assert checks["qa:layout:face-ids"]["ok"] and checks["qa:layout:overlap"]["ok"], checks
    manifest = json.loads(await frame.read_file("/work/pages/pages.json"))
    assert manifest["images"], "the precheck must leave sampled page PNGs for the reader"


async def test_planted_defects_are_named(frame: DockerFrame) -> None:
    await _py(frame, _PLANTED)
    result = await frame.exec(
        "python3 -m plane_house.render /work/planted.pptx --pages /work/pages-planted", timeout=300
    )
    assert result.exit_code == 1
    out = result.stdout
    assert "off-slide" in out and "overlap" in out and "fig-s01-9" in out, out
    assert "qa:layout:house" in out and "without plane_house" in out, (
        "a default-template deck must be named as such"
    )


async def test_browser_profile_imports_the_house_engine() -> None:
    """The browser image layers on the code image without hiding `/opt/plane`: the house engine
    imports there, and the sidecar still resolves from the env its exec sets."""
    ensure_image("browser")
    fr = DockerFrame(task_id=f"test-house-browser-{uuid.uuid4().hex[:8]}", profile="browser")
    await fr.create(allow_domains=[])
    try:
        result = await fr.exec("python3 -c 'import plane_house.render, plane_house.geometry'")
        assert result.ok, result.stderr
        reply = await fr.browser({"verb": "ping"})
        assert reply.get("ok"), reply
    finally:
        await fr.destroy()
