# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`plane_house`'s pure halves (ADR-034): the density arithmetic that splits slides, the
page sampler the visual pass reads from, the overlap math, and the image tag that must move
when the house moves. Loaded by PATH - the package is a frame resident, not a worker import."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.support.paths import BACKEND

HOUSE_ROOT = BACKEND / "prismoid" / "ai" / "frames" / "image" / "house"


@pytest.fixture(scope="module")
def house():
    sys.path.insert(0, str(HOUSE_ROOT))
    try:
        split = importlib.import_module("plane_house.split")
        render = importlib.import_module("plane_house.render")
        geometry = importlib.import_module("plane_house.geometry")
        tokens = importlib.import_module("plane_house.tokens")
        return split, render, geometry, tokens
    finally:
        sys.path.remove(str(HOUSE_ROOT))


def test_split_respects_both_caps_and_drops_nothing(house) -> None:  # pyright: ignore[reportMissingParameterType]
    split, *_ = house
    items = [f"point {i} with a few words" for i in range(14)]
    chunks = split.split_bullets(items, max_bullets=6, max_words=70)
    assert [i for c in chunks for i in c] == items
    assert all(len(c) <= 6 for c in chunks)
    assert all(sum(len(i.split()) for i in c) <= 70 for c in chunks)
    long = ["w " * 90, "short"]
    chunks = split.split_bullets(long, 6, 70)
    assert chunks[0] == [long[0]], "an over-long bullet stands alone; it is never truncated"
    assert split.split_bullets([], 6, 70) == []


def test_overflow_estimate_flags_the_obvious_cases(house) -> None:  # pyright: ignore[reportMissingParameterType]
    split, *_ = house
    assert split.estimate_overflow("x" * 400, box_w_in=4, box_h_in=1, pt=18) is True
    assert split.estimate_overflow("x" * 40, box_w_in=4, box_h_in=1, pt=18) is False
    assert split.estimate_overflow("", 4, 1, 18) is False


def test_fit_box_keeps_aspect_and_centres(house) -> None:  # pyright: ignore[reportMissingParameterType]
    split, *_ = house
    left, top, w, h = split.fit_box(1600, 900, (1.0, 2.0, 6.0, 6.0))
    assert w == pytest.approx(6.0) and h == pytest.approx(3.375)
    assert left == pytest.approx(1.0) and top == pytest.approx(2.0 + (6.0 - 3.375) / 2)
    assert split.fit_box(0, 0, (1, 1, 2, 2)) == (1, 1, 2, 2)


def test_sample_pages_first_last_flagged_capped(house) -> None:  # pyright: ignore[reportMissingParameterType]
    _, render, *_ = house
    assert render.sample_pages(40, {17, 23}) == [1, 2, 3, 17, 23, 39, 40]
    assert render.sample_pages(2) == [1, 2]
    assert render.sample_pages(0) == []
    many = render.sample_pages(100, set(range(10, 60)))
    assert len(many) == 12 and many == sorted(set(many)) and many[:3] == [1, 2, 3]


def test_overlap_fraction_is_of_the_smaller_box(house) -> None:  # pyright: ignore[reportMissingParameterType]
    *_, geometry, _ = house
    assert geometry.overlap_fraction((0, 0, 1, 1), (0.5, 0.5, 1, 1)) == pytest.approx(0.25)
    assert geometry.overlap_fraction((0, 0, 4, 4), (1, 1, 1, 1)) == pytest.approx(1.0)
    assert geometry.overlap_fraction((0, 0, 1, 1), (2, 2, 1, 1)) == 0.0


def test_theme_is_widescreen_and_frozen(house) -> None:  # pyright: ignore[reportMissingParameterType]
    *_, tokens = house
    assert (tokens.HOUSE.slide_w, tokens.HOUSE.slide_h) == (13.333, 7.5)
    with pytest.raises(AttributeError):
        tokens.HOUSE.slide_w = 10


def test_image_tag_moves_when_the_house_moves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.ai.frames import images

    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    house = tmp_path / "house" / "plane_house"
    house.mkdir(parents=True)
    (house / "tokens.py").write_text("A = 1\n")
    monkeypatch.setitem(images.RECIPES, "base", (tmp_path, None))
    before = images.image_digest()
    (house / "tokens.py").write_text("A = 2\n")
    assert images.image_digest() != before
    (house / "__pycache__").mkdir()
    (house / "__pycache__" / "x.pyc").write_bytes(b"junk")
    assert images.image_digest() == images.image_digest()


def test_captions_and_numeric_columns_are_one_shared_law(house) -> None:  # pyright: ignore[reportMissingParameterType]
    sys.path.insert(0, str(HOUSE_ROOT))
    try:
        text = importlib.import_module("plane_house.text")
    finally:
        sys.path.remove(str(HOUSE_ROOT))
    assert text.bare_caption("Figure 2. Computational complexity") == "Computational complexity"
    assert text.bare_caption("fig 3: Butterfly") == "Butterfly"
    assert text.bare_caption("Complexity of 2 algorithms") == "Complexity of 2 algorithms"
    assert text.figure_caption(3, "Figure 3. Churn.") == "Figure 3. Churn."
    assert text.figure_caption(1, "") == "Figure 1"
    assert text.figure_caption(2, "Pipeline", pending=True) == "Figure 2. Pipeline [figure pending]"
    rows = [["Region", "Q3", "Note", "Empty"], ["EMEA", "1.4", "up", ""], ["APAC", "$0.6M", "flat", ""]]
    # the header follows its numbers; an all-empty column is NOT numeric (the vacuous all())
    assert text.numeric_columns(rows, 4) == [False, True, False, False]


_NO_PDF = {"pdf": None, "pages": 0, "sampled": [], "images": [], "findings": [], "face_ids": []}


def _render_module() -> Any:
    sys.path.insert(0, str(HOUSE_ROOT))
    try:
        return importlib.import_module("plane_house.render")
    finally:
        sys.path.remove(str(HOUSE_ROOT))


def _fake_soffice(folder: Path, marker: Path) -> None:
    tool = folder / "soffice"
    tool.write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    tool.chmod(0o755)


def test_a_device_never_starts_soffice_though_it_is_on_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    render = _render_module()
    marker = tmp_path / "ran"
    _fake_soffice(tmp_path, marker)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.setenv("PRISMOID_NO_RENDITIONS", "1")
    assert render.to_pdf("/x/memo.docx", str(tmp_path / "out")) is None
    assert not marker.exists()


def test_a_cloud_frame_still_converts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    render = _render_module()
    marker = tmp_path / "ran"
    _fake_soffice(tmp_path, marker)
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    monkeypatch.delenv("PRISMOID_NO_RENDITIONS", raising=False)
    monkeypatch.setattr(render, "LO_PROFILE", str(tmp_path / "lo"))
    assert render.to_pdf("/x/memo.docx", str(tmp_path / "out")) is None
    assert marker.exists()


@pytest.mark.parametrize(
    ("no_renditions", "said"),
    [
        (None, "could not render to PDF (LibreOffice failed or unsupported type); geometry checks only"),
        ("1", "no PDF on this computer: geometry checks only"),
    ],
    ids=["cloud, word for word", "the device"],
)
def test_what_a_render_without_a_pdf_says(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    no_renditions: str | None,
    said: str,
) -> None:
    render = _render_module()
    monkeypatch.setattr(render, "to_pdf", lambda *a, **k: None)
    monkeypatch.setattr(sys.modules["plane_house.render"], "render", lambda *a, **k: _NO_PDF)
    if no_renditions:
        monkeypatch.setenv("PRISMOID_NO_RENDITIONS", no_renditions)
    else:
        monkeypatch.delenv("PRISMOID_NO_RENDITIONS", raising=False)
    render.main([str(tmp_path / "x.docx"), "--pages", str(tmp_path / "pages")])
    assert said in capsys.readouterr().out.splitlines()
