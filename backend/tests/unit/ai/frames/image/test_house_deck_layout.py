# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`plane_house.geometry.deck_layout_checks`: the deck facts a machine can state, pinned row by
row on decks built here with python-pptx. Loaded by PATH - the package is a frame resident, not a
worker import."""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from pptx import Presentation
from pptx.util import Inches, Pt

from tests.support.paths import BACKEND

HOUSE_ROOT = BACKEND / "prismoid" / "ai" / "frames" / "image" / "house"

ROW_NAMES = [
    "qa:layout:canvas",
    "qa:layout:house",
    "qa:layout:off-slide",
    "qa:layout:overlap",
    "qa:layout:overflow",
    "qa:layout:density",
    "qa:layout:empty-placeholder",
    "qa:layout:face-ids",
]


@pytest.fixture(scope="module")
def house() -> tuple[Any, Any]:
    sys.path.insert(0, str(HOUSE_ROOT))
    try:
        return importlib.import_module("plane_house.geometry"), importlib.import_module("plane_house.tokens")
    finally:
        sys.path.remove(str(HOUSE_ROOT))


def _deck(
    tmp_path: Path,
    house: tuple[Any, Any],
    *slides: Callable[[Any], None],
    widescreen: bool = True,
    stamped: bool = True,
) -> str:
    prs = Presentation()
    prs.slide_width = Inches(13.333) if widescreen else Inches(10)
    prs.slide_height = Inches(7.5)
    if stamped:
        prs.core_properties.category = house[1].HOUSE_STAMP
    for build in slides:
        build(prs)
    path = tmp_path / "deck.pptx"
    prs.save(str(path))
    return str(path)


def _blank(prs: Any) -> Any:
    return prs.slides.add_slide(prs.slide_layouts[6])


def _text(slide: Any, text: str, box: tuple[float, float, float, float], size: int = 18) -> Any:
    left, top, w, h = box
    shape = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(w), Inches(h))
    run = shape.text_frame.paragraphs[0].add_run()
    run.text = text
    run.font.size = Pt(size)
    return shape


def _picture(tmp_path: Path, slide: Any, box: tuple[float, float, float, float]) -> Any:
    png = tmp_path / "pixel.png"
    if not png.exists():
        Image.new("RGB", (4, 4), "white").save(png)
    left, top, w, h = box
    return slide.shapes.add_picture(str(png), Inches(left), Inches(top), Inches(w), Inches(h))


def _rows(geometry: Any, path: str) -> dict[str, dict[str, Any]]:
    rows = geometry.deck_layout_checks(path)
    assert [r["name"] for r in rows] == ROW_NAMES
    return {r["name"].removeprefix("qa:layout:"): r for r in rows}


def test_a_clean_house_deck_passes_every_row(tmp_path: Path, house: tuple[Any, Any]) -> None:
    path = _deck(tmp_path, house, lambda prs: _text(_blank(prs), "A short title", (1, 1, 6, 1)))
    rows = _rows(house[0], path)
    assert all(r["ok"] for r in rows.values())
    assert all(r["detail"] == "" and r["page"] is None for r in rows.values())


def test_canvas_and_house_stamp_rows(tmp_path: Path, house: tuple[Any, Any]) -> None:
    path = _deck(tmp_path, house, widescreen=False, stamped=False)
    rows = _rows(house[0], path)
    assert rows["canvas"] == {
        "name": "qa:layout:canvas",
        "ok": False,
        "detail": "slide canvas is 10.00×7.50in - house decks are 16:9 (13.333×7.5)",
        "page": None,
    }
    assert rows["house"]["ok"] is False
    assert rows["house"]["detail"].startswith("built without plane_house.pptx_theme.HouseDeck")


def test_off_slide_lists_five_and_takes_its_page_from_the_detail(
    tmp_path: Path, house: tuple[Any, Any]
) -> None:
    names: list[str] = []

    def build(prs: Any) -> None:
        _text(_blank(prs), "fine", (1, 1, 3, 1))
        slide = _blank(prs)
        for i in range(7):
            names.append(_text(slide, f"x{i}", (12 + i * 0.1, 1, 3, 0.8)).name)

    rows = _rows(house[0], _deck(tmp_path, house, build))
    off = rows["off-slide"]
    assert off["ok"] is False
    assert off["detail"] == "shapes outside the canvas: " + "; ".join(f"slide 2 '{n}'" for n in names[:5])
    assert off["page"] == 2


def test_overlap_counts_only_collisions_with_a_picture(tmp_path: Path, house: tuple[Any, Any]) -> None:
    names: list[str] = []

    def build(prs: Any) -> None:
        texts = _blank(prs)
        _text(texts, "one", (1, 1, 4, 2))
        _text(texts, "two", (2, 1.5, 4, 2))  # text on text is not a collision
        slide = _blank(prs)
        names.append(_text(slide, "caption", (1, 1, 4, 2)).name)
        names.append(_picture(tmp_path, slide, (1, 1, 2, 2)).name)

    rows = _rows(house[0], _deck(tmp_path, house, build))
    assert rows["overlap"]["ok"] is False
    assert rows["overlap"]["detail"] == f"text and picture collide: slide 2: '{names[0]}' ∩ '{names[1]}' 100%"
    assert rows["overlap"]["page"] == 2


def test_a_picture_alone_is_neither_an_overlap_nor_an_empty_placeholder(
    tmp_path: Path, house: tuple[Any, Any]
) -> None:
    rows = _rows(house[0], _deck(tmp_path, house, lambda prs: _picture(tmp_path, _blank(prs), (1, 1, 2, 2))))
    assert all(r["ok"] for r in rows.values())


def test_overflow_reads_the_run_font_size(tmp_path: Path, house: tuple[Any, Any]) -> None:
    text = "words that will never fit in this box " * 6
    names: list[str] = []
    rows = _rows(
        house[0],
        _deck(tmp_path, house, lambda prs: names.append(_text(_blank(prs), text, (1, 1, 2, 0.5), 40).name)),
    )
    assert rows["overflow"]["ok"] is False
    assert rows["overflow"]["detail"] == (
        f"text will not fit its box: slide 1 '{names[0]}' ({len(text.strip())} chars at 40pt in 2.0×0.5in)"
    )
    assert rows["overflow"]["page"] == 1


def test_density_flags_a_wall_of_text(tmp_path: Path, house: tuple[Any, Any]) -> None:
    def build(prs: Any) -> None:
        slide = _blank(prs)
        _text(slide, "word " * 60, (0.5, 0.5, 12, 3), 8)
        _text(slide, "word " * 60, (0.5, 4, 12, 3), 8)

    rows = _rows(house[0], _deck(tmp_path, house, build))
    assert rows["density"] == {
        "name": "qa:layout:density",
        "ok": False,
        "detail": "walls of text: slide 1 (120 words)",
        "page": 1,
    }


def test_an_empty_placeholder_is_flagged(tmp_path: Path, house: tuple[Any, Any]) -> None:
    rows = _rows(house[0], _deck(tmp_path, house, lambda prs: prs.slides.add_slide(prs.slide_layouts[5])))
    empty = rows["empty-placeholder"]
    assert empty["ok"] is False
    assert empty["detail"].startswith("empty placeholders a viewer sees as 'Click to add': slide 1 '")
    assert empty["page"] == 1


def test_face_ids_name_every_slide_and_page_the_first(tmp_path: Path, house: tuple[Any, Any]) -> None:
    def build(prs: Any) -> None:
        _text(_blank(prs), "clean", (1, 1, 4, 1))
        _text(_blank(prs), "see fig-revenue", (1, 1, 4, 1))
        _text(_blank(prs), "and fig-cost-2", (1, 1, 4, 1))
        _text(_blank(prs), "and fig-cost-3", (1, 1, 4, 1))

    rows = _rows(house[0], _deck(tmp_path, house, build))
    assert rows["face-ids"] == {
        "name": "qa:layout:face-ids",
        "ok": False,
        "detail": "internal figure ids on the face of slide(s) [2, 3, 4]",
        "page": 2,
    }
