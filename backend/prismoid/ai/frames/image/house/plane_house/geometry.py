# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Layout facts a machine can state (ADR-034): overlap, off-canvas, overflow, density,
orphan pages, internal ids on the face. Rows carry the QA gate's shape -
`{"name": "qa:layout:<sub>", "ok": bool, "detail": str, "page": int|None}` - so the same
function feeds the author-side pre-flight and the system-side precheck.
"""

from __future__ import annotations

import re

from plane_house.split import estimate_overflow
from plane_house.tokens import HOUSE, HOUSE_STAMP, Theme

FIG_TOKEN = re.compile(r"\bfig-[\w-]+\b")
EMU_PER_INCH = 914400


def _row(name: str, ok: bool, detail: str = "", page: int | None = None) -> dict:
    return {"name": f"qa:layout:{name}", "ok": bool(ok), "detail": detail[:500], "page": page}


def _overlap_area(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    w = min(ax + aw, bx + bw) - max(ax, bx)
    h = min(ay + ah, by + bh) - max(ay, by)
    return w * h if w > 0 and h > 0 else 0.0


def overlap_fraction(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """Overlap as a fraction of the SMALLER box - pure."""
    inter = _overlap_area(a, b)
    smaller = min(a[2] * a[3], b[2] * b[3])
    return inter / smaller if smaller > 0 else 0.0


# ── decks ───────────────────────────────────────────────────────────────────────────────────
def _shape_box(shape) -> tuple[float, float, float, float] | None:
    vals = [getattr(shape, k, None) for k in ("left", "top", "width", "height")]
    if any(v is None for v in vals):
        return None
    return tuple(v / EMU_PER_INCH for v in vals)


def _shape_text(shape) -> str:
    if getattr(shape, "has_text_frame", False):
        return "\n".join(p.text for p in shape.text_frame.paragraphs).strip()
    return ""


def _font_pt(shape, default: float) -> float:
    try:
        for para in shape.text_frame.paragraphs:
            for run in para.runs:
                if run.font.size is not None:
                    return run.font.size.pt
    except Exception:
        pass
    return default


def deck_layout_checks(path: str, theme: Theme = HOUSE) -> list[dict]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(path)
    sw, sh = prs.slide_width / EMU_PER_INCH, prs.slide_height / EMU_PER_INCH
    rows = _deck_stamp_rows(prs, sw, sh)
    found = _DeckFindings()
    for n, slide in enumerate(prs.slides, 1):
        _scan_slide(n, slide, canvas=(sw, sh), theme=theme, picture=MSO_SHAPE_TYPE.PICTURE, found=found)
    rows.extend(_deck_finding_rows(found))
    _page_from_detail(rows)
    return rows


def _deck_stamp_rows(prs, sw: float, sh: float) -> list[dict]:  # type: ignore[no-untyped-def, type-arg]
    """The two whole-deck facts: a 16:9 canvas, and the house builders' stamp."""
    rows: list[dict] = []  # type: ignore[type-arg]
    widescreen = abs(sw / sh - 16 / 9) < 0.02
    rows.append(
        _row(
            "canvas",
            widescreen,
            "" if widescreen else f"slide canvas is {sw:.2f}×{sh:.2f}in - house decks are 16:9 (13.333×7.5)",
        )
    )
    housed = (prs.core_properties.category or "") == HOUSE_STAMP
    rows.append(
        _row(
            "house",
            housed,
            ""
            if housed
            else "built without plane_house.pptx_theme.HouseDeck - a default-template deck; rebuild on the house builders",
        )
    )
    return rows


class _DeckFindings:
    """What the slide walk collected, one list per row it feeds."""

    def __init__(self) -> None:
        self.off: list[str] = []
        self.overlaps: list[str] = []
        self.overflow: list[str] = []
        self.dense: list[str] = []
        self.empties: list[str] = []
        self.face: list[int] = []


def _scan_slide(  # type: ignore[no-untyped-def]
    n: int, slide, *, canvas: tuple[float, float], theme: Theme, picture, found: _DeckFindings
) -> None:
    """One slide's shapes into `found`, then the slide-wide facts: collisions and density."""
    boxes: list[tuple[str, tuple[float, float, float, float], bool]] = []  # (name, box, is_picture)
    words = 0
    for shape in slide.shapes:
        words += _scan_shape(
            n, shape, canvas=canvas, theme=theme, is_pic=shape.shape_type == picture, found=found, boxes=boxes
        )
    found.overlaps.extend(_slide_overlaps(n, boxes))
    if words > theme.max_words_per_slide * 1.5:
        found.dense.append(f"slide {n} ({words} words)")


def _scan_shape(  # type: ignore[no-untyped-def]
    n: int,
    shape,
    *,
    canvas: tuple[float, float],
    theme: Theme,
    is_pic: bool,
    found: _DeckFindings,
    boxes: list[tuple[str, tuple[float, float, float, float], bool]],
) -> int:
    """One shape's facts into `found` (and its box into `boxes` when it can collide); returns
    the words it carries."""
    box = _shape_box(shape)
    text = _shape_text(shape)
    words = len(text.split()) if text else 0
    if text and FIG_TOKEN.search(text):
        found.face.append(n)
    if box is None:
        return words
    if _off_canvas(box, canvas):
        found.off.append(f"slide {n} '{shape.name}'")
    if text or is_pic:
        boxes.append((shape.name, box, is_pic))
    if text and not is_pic:
        note = _overflow_note(n, shape, text, box, theme)
        if note:
            found.overflow.append(note)
    if getattr(shape, "is_placeholder", False) and not text and not is_pic:
        found.empties.append(f"slide {n} '{shape.name}'")
    return words


def _off_canvas(box: tuple[float, float, float, float], canvas: tuple[float, float]) -> bool:
    left, top, w, h = box
    sw, sh = canvas
    return left < -0.01 or top < -0.01 or left + w > sw + 0.01 or top + h > sh + 0.01


def _overflow_note(n: int, shape, text: str, box: tuple[float, float, float, float], theme: Theme) -> str:  # type: ignore[no-untyped-def]
    """The overflow line for a text box whose text will not fit it; "" when it fits or has no area."""
    _, _, w, h = box
    if w <= 0 or h <= 0:
        return ""
    pt = _font_pt(shape, theme.deck.body)
    if not estimate_overflow(text, w, h, pt):
        return ""
    return f"slide {n} '{shape.name}' ({len(text)} chars at {pt:.0f}pt in {w:.1f}×{h:.1f}in)"


def _slide_overlaps(n: int, boxes: list[tuple[str, tuple[float, float, float, float], bool]]) -> list[str]:
    out: list[str] = []
    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            if not (a[2] or b[2]):  # only text↔picture and picture↔picture collisions matter
                continue
            frac = overlap_fraction(a[1], b[1])
            if frac > 0.05:
                out.append(f"slide {n}: '{a[0]}' ∩ '{b[0]}' {int(frac * 100)}%")
    return out


def _deck_finding_rows(found: _DeckFindings) -> list[dict]:  # type: ignore[type-arg]
    off, overlaps, overflow, dense, empties, face = (
        found.off,
        found.overlaps,
        found.overflow,
        found.dense,
        found.empties,
        found.face,
    )
    return [
        _row("off-slide", not off, "shapes outside the canvas: " + "; ".join(off[:5]) if off else ""),
        _row(
            "overlap",
            not overlaps,
            "text and picture collide: " + "; ".join(overlaps[:5]) if overlaps else "",
        ),
        _row(
            "overflow",
            not overflow,
            "text will not fit its box: " + "; ".join(overflow[:5]) if overflow else "",
        ),
        _row("density", not dense, "walls of text: " + "; ".join(dense[:5]) if dense else ""),
        _row(
            "empty-placeholder",
            not empties,
            "empty placeholders a viewer sees as 'Click to add': " + "; ".join(empties[:5])
            if empties
            else "",
        ),
        _row(
            "face-ids",
            not face,
            f"internal figure ids on the face of slide(s) {sorted(set(face))[:8]}" if face else "",
            min(face) if face else None,
        ),
    ]


def _page_from_detail(rows: list[dict]) -> None:  # type: ignore[type-arg]
    """A row with no page of its own takes the first slide its detail names."""
    for r in rows:
        if r["page"] is None:
            m = re.search(r"slide (\d+)", r["detail"])
            r["page"] = int(m.group(1)) if m else None


# ── documents ───────────────────────────────────────────────────────────────────────────────
def docx_layout_checks(path: str, theme: Theme = HOUSE) -> list[dict]:
    import docx

    d = docx.Document(path)
    rows: list[dict] = []
    section = d.sections[0]
    usable_w = (section.page_width - section.left_margin - section.right_margin) / EMU_PER_INCH
    usable_h = (section.page_height - section.top_margin - section.bottom_margin) / EMU_PER_INCH
    too_big = [
        i + 1
        for i, s in enumerate(d.inline_shapes)
        if s.width / EMU_PER_INCH > usable_w + 0.01 or s.height / EMU_PER_INCH > usable_h + 0.01
    ]
    rows.append(
        _row(
            "image-fit",
            not too_big,
            f"picture(s) {too_big[:5]} exceed the printable area ({usable_w:.1f}×{usable_h:.1f}in)"
            if too_big
            else "",
        )
    )
    narrow = section.left_margin / EMU_PER_INCH < 0.5 or section.right_margin / EMU_PER_INCH < 0.5
    rows.append(_row("margins", not narrow, "margins under 0.5in" if narrow else ""))
    housed = (d.core_properties.category or "") == HOUSE_STAMP
    rows.append(
        _row(
            "house",
            housed,
            ""
            if housed
            else "built without plane_house.docx_theme.house_document - a default-template file (no cover, no page numbers, default styles); rebuild on the house builders",
        )
    )
    face = [p.text[:60] for p in d.paragraphs if FIG_TOKEN.search(p.text)]
    rows.append(
        _row("face-ids", not face, "internal figure ids in body text: " + "; ".join(face[:3]) if face else "")
    )
    unstyled_lists = sum(1 for p in d.paragraphs if p.text.lstrip().startswith(("• ", "- ", "* ")))
    rows.append(
        _row(
            "fake-lists",
            unstyled_lists == 0,
            f"{unstyled_lists} typed-glyph list line(s) - use List Bullet" if unstyled_lists else "",
        )
    )
    return rows


def pdf_layout_checks(
    pdf: str, *, kind: str = "", target_units: int = 0, tolerance: float = 0.0
) -> list[dict]:
    """Facts on the RENDERED pages (what the customer opens): page count vs band, near-empty
    interior pages (the orphan tell), internal ids on the face."""
    from pypdf import PdfReader

    rows: list[dict] = []
    try:
        reader = PdfReader(pdf)
        texts = [(page.extract_text() or "") for page in reader.pages]
        pictured = [_has_image(page) for page in reader.pages]
    except Exception as broken:
        return [_row("render", False, f"the rendered PDF could not be read: {broken}")]
    n = len(texts)
    rows.append(_row("render", n > 0, "" if n else "the rendition has zero pages"))
    if kind in ("document",) and target_units and tolerance:
        low, high = int(target_units * (1 - tolerance)), int(target_units * (1 + tolerance))
        rows.append(
            _row(
                "pages",
                low <= n <= high,
                f"{n} rendered page(s) vs target {target_units} ±{int(tolerance * 100)}% ({low}–{high})",
            )
        )
    # Orphans: an interior page (not the cover, not the last) carrying almost nothing - a
    # DOCUMENT rule; a slide is meant to carry few words, so decks skip it. A page that
    # carries a picture is a figure page, and a contents page is short by nature: neither is
    # a stray break (both were false REVISE floors on the walkers' own output).
    orphans = (
        [
            i + 1
            for i, t in enumerate(texts)
            if 1 < i + 1 < n and len(t.split()) < 40 and not pictured[i] and not _is_contents(t)
        ]
        if kind != "deck"
        else []
    )
    rows.append(
        _row(
            "orphan-pages",
            not orphans,
            f"near-empty interior page(s) {orphans[:6]} - a stray page break or a figure pushed alone"
            if orphans
            else "",
            orphans[0] if orphans else None,
        )
    )
    face = [i + 1 for i, t in enumerate(texts) if FIG_TOKEN.search(t)]
    rows.append(
        _row(
            "face-ids",
            not face,
            f"internal figure ids visible on page(s) {face[:6]}" if face else "",
            face[0] if face else None,
        )
    )
    return rows


def _has_image(page) -> bool:
    try:
        return bool(page.images)
    except Exception:
        return False


def _is_contents(text: str) -> bool:
    return text.lstrip()[:20].lower().startswith(("contents", "table of contents"))


def face_fig_ids(path: str) -> list[str]:
    """Every `fig-…` token a READER would see: slide text for pptx, body text for docx, page
    text for pdf. Speaker notes, alt text and keywords are the ids' proper homes and are not
    searched here."""
    found: list[str] = []
    low = path.lower()
    try:
        if low.endswith(".pptx"):
            from pptx import Presentation

            for slide in Presentation(path).slides:
                for shape in slide.shapes:
                    found.extend(FIG_TOKEN.findall(_shape_text(shape)))
        elif low.endswith(".docx"):
            import docx

            d = docx.Document(path)
            for p in d.paragraphs:
                found.extend(FIG_TOKEN.findall(p.text))
            for t in d.tables:
                for row in t.rows:
                    for cell in row.cells:
                        found.extend(FIG_TOKEN.findall(cell.text))
        elif low.endswith(".pdf"):
            from pypdf import PdfReader

            for page in PdfReader(path).pages:
                found.extend(FIG_TOKEN.findall(page.extract_text() or ""))
    except Exception:
        return found
    return sorted(set(found))


def hidden_fig_ids(path: str) -> list[str]:
    """The ids in their proper homes - notes + alt text (pptx), alt text + keywords (docx).
    The precheck's `qa:figures` searches face text PLUS these."""
    found: list[str] = []
    low = path.lower()
    try:
        if low.endswith(".pptx"):
            from pptx import Presentation

            for slide in Presentation(path).slides:
                if slide.has_notes_slide:
                    found.extend(FIG_TOKEN.findall(slide.notes_slide.notes_text_frame.text))
                for shape in slide.shapes:
                    descr = (
                        shape._element.xpath(".//p:cNvPr/@descr") if hasattr(shape._element, "xpath") else []
                    )
                    for d in descr:
                        found.extend(FIG_TOKEN.findall(str(d)))
        elif low.endswith(".docx"):
            import docx

            d = docx.Document(path)
            found.extend(FIG_TOKEN.findall(d.core_properties.keywords or ""))
            for s in d.inline_shapes:
                found.extend(FIG_TOKEN.findall(s._inline.docPr.get("descr") or ""))
    except Exception:
        return found
    return sorted(set(found))
