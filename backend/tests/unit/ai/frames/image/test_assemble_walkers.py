# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The assemble walkers' review seams: fig ids survive into extractable text
(md_to_pptx - the false-REVISE md_to_docx memorializes), mangled sheet tabs keep the full
title in-sheet (md_to_xlsx - qa:sections must be able to find it), and cell coercion keeps
data semantics. The scripts are frame residents, not a package - they load by path; the
parser-heavy pins skip gracefully where python-pptx/openpyxl are absent (they are pinned in
the frame image, not this venv)."""

from __future__ import annotations

import importlib.util
import struct
import sys
import zlib
from pathlib import Path
from typing import Any

import pytest

from tests.support.paths import BACKEND

SCRIPTS = BACKEND / "skills" / "assemble" / "scripts"
HOUSE = BACKEND / "prismoid" / "ai" / "frames" / "image" / "house"


def _load(name: str) -> Any:  # Any on purpose: a by-path frame script, not a typed package
    sys.path.insert(0, str(SCRIPTS))
    if str(HOUSE) not in sys.path:
        sys.path.append(str(HOUSE))  # the walkers import plane_house lazily
    try:
        spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module  # as an import would: a dataclass resolves its annotations there
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


def _png_1px() -> bytes:
    """A genuinely valid 1×1 RGBA PNG, built not pasted - python-pptx reads its header."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)
    idat = zlib.compress(b"\x00\x00\x00\x00\x00")  # filter byte + one RGBA(0,0,0,0) pixel
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


# ── pure seams: no frame parsers needed ───────────────────────────────────────────────────────


def test_cell_keeps_data_semantics() -> None:
    xlsx = _load("md_to_xlsx")
    value, fmt = xlsx._cell("45%")
    assert value == pytest.approx(0.45) and fmt == "0%"  # a percent is a FORMAT, not ×100
    value, fmt = xlsx._cell("4.5%")
    assert value == pytest.approx(0.045) and fmt == "0.0%"
    assert xlsx._cell("007") == ("007", None)  # an identifier, never the number 7
    assert xlsx._cell("1,234") == (1234, None)
    assert xlsx._cell("3.14") == (3.14, None)
    assert xlsx._cell("harbor") == ("harbor", None)
    assert xlsx._cell("  ") == (None, None)
    assert xlsx._cell("0") == (0, None)  # a lone zero is still a number


def test_sheet_name_mangles_within_excel_law() -> None:
    xlsx = _load("md_to_xlsx")
    long_title = "Assumptions and Input Parameters"  # 32 chars - one past the tab limit
    assert xlsx._sheet_name(long_title, set()) == long_title[:31]
    colon = xlsx._sheet_name("Scenario: Inputs", set())
    assert ":" not in colon and len(colon) <= 31


def test_an_emptied_section_file_is_not_a_section(tmp_path: Path) -> None:
    """A Machine whose provider cannot unlink root's files (sandbar) empties an earlier step's
    mounts in place: a zero-byte `NN-*.md` is a mount taken away, never a chapter - and never the
    `-final` that would win over this step's real section."""
    walk = _load("md_walk")
    (tmp_path / "01-market-overview-final.md").write_text("")  # an earlier turn's, emptied
    (tmp_path / "05-risks.md").write_text("")
    (tmp_path / "01-q4-summary.md").write_text("# Q4 summary\n\nThe quarter.\n")
    (tmp_path / "02-q4-revenue.md").write_text("# Q4 revenue\n\nThe money.\n")
    sections, gaps = walk.load_sections(str(tmp_path), 2)
    assert [(s.number, s.title) for s in sections] == [(1, "Q4 summary"), (2, "Q4 revenue")]
    assert gaps == []
    # The markdown target chooses its sections the same way, and an emptied figure is no figure.
    out = tmp_path / "doc.md"
    _load("concat_md").build(str(tmp_path), str(out), "Q4", "", False, 2)
    assert "The quarter." in out.read_text() and "market" not in out.read_text().lower()
    (tmp_path / "fig-q4.png").write_bytes(b"")
    assert walk.figure_path(str(tmp_path), "fig-q4") is None


# ── the review seams, proven on real files (skip where the frame's parsers are absent) ────────


def test_xlsx_mangled_tab_keeps_the_full_title_in_sheet(tmp_path: Path) -> None:
    openpyxl = pytest.importorskip("openpyxl")
    xlsx = _load("md_to_xlsx")
    long_title = "Assumptions and Input Parameters"
    sections = tmp_path / "sections"
    sections.mkdir()
    (sections / "01-assumptions.md").write_text(
        f"# {long_title}\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n", encoding="utf-8"
    )
    out = tmp_path / "model.xlsx"
    xlsx.build(str(sections), str(out), "The Model")
    book = openpyxl.load_workbook(str(out))
    sheet = book.worksheets[0]
    assert len(sheet.title) <= 31
    # qa:sections greps the extracted text for the FULL outline title:
    assert sheet.cell(row=1, column=1).value == long_title


def test_pptx_fig_ids_survive_embed_and_placeholder(tmp_path: Path) -> None:
    pptx_lib = pytest.importorskip("pptx")
    pptx = _load("md_to_pptx")
    sections = tmp_path / "sections"
    sections.mkdir()
    (sections / "01-ports.md").write_text(
        "# Ports\n\n"
        "## Slide: Cranes\n- one idea per slide\n\nFigure (fig-s01-1): Cranes at dusk\n\n"
        "## Slide: Berths\n- another idea\n\nFigure (fig-s01-2): Berths at dawn\n",
        encoding="utf-8",
    )
    images = tmp_path / "images"
    images.mkdir()
    (images / "fig-s01-1.png").write_bytes(_png_1px())  # embeds; fig-s01-2 falls to placeholder

    out = tmp_path / "deck.pptx"
    pptx.build(str(sections), str(out), "T", "", str(images))
    deck = pptx_lib.Presentation(str(out))
    face: list[str] = []
    hidden: list[str] = []
    for slide in deck.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                face.append(shape.text_frame.text)
            descr = shape._element.xpath(".//p:cNvPr/@descr")
            hidden.extend(str(d) for d in descr)
        if slide.has_notes_slide:
            hidden.append(slide.notes_slide.notes_text_frame.text)
    face_text, hidden_text = "\n".join(face), "\n".join(hidden)
    # THE ID LAW: QA's precheck searches face + hidden homes, but a
    # customer never sees `fig-s01-1` on a slide - embedded or placeholder alike:
    assert "fig-s01-1" in hidden_text, "an embedded figure dropped its id from notes/alt text"
    assert "fig-s01-2" in hidden_text, "a placeholder dropped its id from the notes"
    assert "fig-s01-" not in face_text, "an internal figure id leaked onto the slide face"
    assert "figure pending" in face_text, "a failed figure must stand as a visible placeholder"
    assert any(p for s in deck.slides for p in s.shapes if p.shape_type == 13), "no picture embedded"
    assert deck.slide_width / 914400 == pytest.approx(13.333, abs=0.01), "house decks are 16:9"
