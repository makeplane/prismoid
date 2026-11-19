# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The QA gate's deterministic pre-checks - run INSIDE the frame (ADR-026). Stdlib + the one reader
(`suites/extract.py`, shipped beside this file at that relative path), whose parsers are frame-pinned.

Job JSON on stdin:
    {"deliverable": "/work/deliverable.docx", "rendition": "/work/rendition.pdf", "outline": "/work/outline.md",
     "kind": "document", "unit": "pages", "target_units": 15, "target_words": 4500,
     "tolerance": 0.3, "figure_ids": ["fig-s01-1", ...]}
Checks JSON on stdout: {"checks": [{"name": "qa:sections", "ok": true, "detail": ""}, ...]}

Exit codes: 0 = checks ran (each row is a DOCUMENT fact); **3 = the runner itself could not
process this deliverable** (unsupported extension, missing parser) - the composer converts a
non-zero exit into INFRA (PASS_UNREAD with the note visible), because "we could not read it"
must never bill the user a forced rebuild the way a real document fact does.

Side effect: writes the full extracted text to /work/extracted.md - one extraction, consumed
by both these checks and the model read. Every check name carries the `qa:` prefix (the
namespace law: these rows may never enter a VerificationReport).

The visual half: a binary deliverable is rendered to PDF and rasterized into
`/work/pages/page-NN.png` through the frame-resident `plane_house.render`; the geometry facts
ride as `qa:layout:*` rows and `/work/pages/pages.json` names the sampled pages the composer
hands the reader as images. A render that cannot happen costs the visual rows (they are
simply absent), never the gate - the text checks stand on their own.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, cast

# The one reader, loaded by exact path (the frame runs this file alone); registered before it runs because its
# dataclasses look their own module up.
_SPEC = importlib.util.spec_from_file_location("extract", Path(__file__).parent / "suites" / "extract.py")
assert _SPEC is not None and _SPEC.loader is not None
_extract = sys.modules["extract"] = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_extract)
extract, UnsupportedFormat = _extract.extract, _extract.UnsupportedFormat

TERMINAL = (*".!?:’”)»]", '"', "'", "|")  # sentence enders + straight quotes + table/list shapes
_WORK = os.environ.get("PRISMOID_WORK_DIR", "/work")  # a device frame's is a real folder
EXTRACTED = f"{_WORK}/extracted.md"
PAGES_DIR = f"{_WORK}/pages"

# Whole connector words a paragraph should not END on - a word-boundary test, never str.endswith
# ("homeland"/"roof"/"breathe" end in and/of/the by LETTERS, not by meaning).
_CONNECTORS = frozenset({"and", "the", "of", "or", "but", "with", "to", "a", "an"})
_WORD = re.compile(r"[A-Za-z']+")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip().lower()


def _dangles(para: str) -> bool:
    """A long paragraph that ends mid-clause: a trailing connector WORD or a comma/dash."""
    para = para.strip()
    if len(para) <= 200:
        return False
    # The em dash rides as an escape, so no punctuation sweep can rewrite the character it names.
    if para.endswith((",", ";", "-", "\u2014")):
        return True
    words = _WORD.findall(para)
    return bool(words) and words[-1].lower() in _CONNECTORS


def hidden_ids(path: str) -> str:
    """The figure ids' proper homes on a binary: notes and alt text (pptx), alt text and core keywords
    (docx); `qa:figures` searches the face text plus this."""
    try:
        from plane_house import (  # pyright: ignore[reportMissingImports] - frame-only
            geometry,  # pyright: ignore[reportUnknownVariableType]  # untyped outside the frame
        )
    except ImportError:
        return ""
    ids = cast("list[str]", geometry.hidden_fig_ids(path))
    return " ".join(ids)


def layout_rows(
    path: str, kind: str, unit: str, target_units: int, tolerance: float, rendition: str | None = None
) -> list[dict[str, Any]]:
    """Render + geometry, writing /work/pages/*.png and pages.json; never raises (a missing renderer
    means no rows, never a skipped gate)."""
    try:
        from plane_house import (  # pyright: ignore[reportMissingImports] - frame-only
            render,  # pyright: ignore[reportUnknownVariableType]  # untyped outside the frame
        )
    except ImportError:
        return []
    try:
        manifest = cast(
            "Any",
            render.render(
                path,
                PAGES_DIR,
                kind=kind,
                target_units=target_units if unit == "pages" else 0,
                tolerance=tolerance,
                pdf=rendition if rendition and os.path.isfile(rendition) else None,
            ),
        )
    except Exception as failed:  # the visual pass is best-effort on this side of the law
        print(f"visual pass skipped: {failed}", file=sys.stderr)
        return []
    return [
        {"name": f["name"], "ok": bool(f["ok"]), "detail": str(f.get("detail", ""))[:500]}
        for f in manifest.get("findings", [])
    ]


def outline_sections(outline_md: str) -> list[str]:
    """Section titles from outline.md's `### NN · Title  (words)` lines (render_outline_md)."""
    titles: list[str] = []
    for line in outline_md.splitlines():
        match = re.match(r"^###\s+\d{2}\s*·\s*(.+?)\s*(?:\(\d+ words\))?\s*$", line.strip())
        if match:
            titles.append(match.group(1).strip())
    return titles


Row = dict[str, Any]


def _row(name: str, ok: bool, detail: str = "") -> Row:
    return {"name": name, "ok": bool(ok), "detail": detail[:500]}


@dataclass(frozen=True, slots=True)
class _Doc:
    """What every check reads, derived once: the job, the extracted text and unit count, the
    outline, and the size unit (slides or sheets when the job says so, else words)."""

    job: dict[str, Any]
    text: str
    counted: int
    outline_md: str
    unit: str


def _sections(doc: _Doc) -> list[Row]:
    # Every outline section title appears in the document BODY (not merely echoed in the
    # generated Contents), and in order. The extracted text INCLUDES the TOC, so a bare find()
    # would pass a section whose body was dropped but whose title sits in Contents: when a TOC
    # is present the title must appear TWICE (Contents + body), and order is read on the LAST
    # occurrence (the body copy, since the TOC sits at the top).
    titles = outline_sections(doc.outline_md)
    if not titles:
        return []
    haystack = _norm(doc.text)
    has_toc = "contents" in haystack[:3000]  # the walkers' TOC heading is literally "Contents"
    need = 2 if has_toc else 1
    missing = [t for t in titles if haystack.count(_norm(t)) < need]
    present = [t for t in titles if haystack.count(_norm(t)) >= need]
    positions = [haystack.rfind(_norm(t)) for t in present]
    ordered = positions == sorted(positions)
    return [
        _row(
            "qa:sections",
            not missing and ordered,
            (f"missing from the body (Contents-only or absent): {', '.join(missing[:5])}" if missing else "")
            + ("" if ordered else " sections appear out of outline order"),
        )
    ]


def _size(doc: _Doc) -> list[Row]:
    # The kind's own unit against the bucket band: words for documents, slides for decks,
    # content sheets for workbooks. NO target means an EXPLICIT skip row - a silently dropped
    # check leaves the record unable to tell "passed" from "never ran".
    job, unit = doc.job, doc.unit
    tolerance = float(job.get("tolerance") or 0.3)
    if unit == "words":
        measured, target = len(doc.text.split()), int(job.get("target_words") or 0)
    else:
        measured, target = doc.counted, int(job.get("target_units") or 0)
    if not target:
        return [_row("qa:size", True, f"skipped - no size target on the task ({measured:,} {unit} measured)")]
    low, high = int(target * (1 - tolerance)), int(target * (1 + tolerance))
    return [
        _row(
            "qa:size",
            low <= measured <= high,
            f"{measured:,} {unit} vs target {target:,} ±{int(tolerance * 100)}% ({low:,}–{high:,})",
        )
    ]


def _figures(doc: _Doc) -> list[Row]:
    # Every planned figure is either embedded (unknowable from text alone) or at least present
    # as its caption; an orphaned `Figure (fig-…)` MARKER means assembly never replaced it AND
    # never placed the placeholder language.
    figure_ids = list(doc.job.get("figure_ids") or [])
    if not figure_ids:
        return []
    homes = doc.text + " " + hidden_ids(doc.job["deliverable"])
    absent = [f for f in figure_ids if f not in homes]
    return [
        _row("qa:figures", not absent, f"no caption or marker for: {', '.join(absent[:5])}" if absent else "")
    ]


def _tail_ok(tail: str) -> bool:
    """The document's last line closes like a sentence, or is a known clean
    ending: a doc ending on a value, a year, a URL, a quotation mark or a list item."""
    last_token = tail.rsplit(None, 1)[-1] if tail.split() else ""
    return (
        tail.endswith(TERMINAL)
        or tail.startswith(("#", "[", "- ", "* "))  # a heading, a note, or a closing list item
        or tail[-1:].isdigit()  # a year, a table value, a figure number - not a broken clause
        or "://" in last_token
        or last_token.startswith("www.")  # a trailing URL is not a truncation
    )


def _truncation(doc: _Doc) -> list[Row]:
    # The document and its paragraphs end like sentences. BOTH a paragraph ending on a bare
    # connector word AND a final line that does not close like a sentence are the hard floor -
    # a truncated document must never merely warn. Known clean endings are exempted in
    # `_tail_ok`, so the floor blocks broken documents without punishing clean ones.
    text = doc.text
    tail = text.rstrip().splitlines()[-1].rstrip() if text.rstrip() else ""
    tail_ok = _tail_ok(tail)
    dangling = sum(1 for para in text.split("\n\n") if _dangles(para))
    return [
        _row(
            "qa:truncation",
            dangling == 0 and tail_ok,
            (f"{dangling} paragraph(s) end mid-clause. " if dangling else "")
            + (
                "" if tail_ok else f"the document's last line does not close like a sentence: …{tail[-80:]!r}"
            ),
        )
    ]


def _headings(doc: _Doc) -> list[Row]:
    # One title, no duplicate H1/H2 headings. Level-3+ headings legitimately repeat (a
    # legislated "### Key takeaways" per chapter is by design), so duplicate detection is
    # confined to # and ## - the section-level seams where a doubled title is the real defect.
    headings = [ln.strip() for ln in doc.text.splitlines() if ln.strip().startswith("#")]
    if not headings:
        return []
    h1 = [h for h in headings if h.startswith("# ") and not h.startswith("## ")]
    top2 = [h for h in headings if h.startswith(("# ", "## "))]
    duplicates = {h for h in top2 if top2.count(h) > 1}
    return [
        _row(
            "qa:headings",
            len(h1) <= 1 and not duplicates,
            (f"{len(h1)} top-level titles " if len(h1) > 1 else "")
            + (f"duplicated: {', '.join(sorted(duplicates)[:3])}" if duplicates else ""),
        )
    ]


def _links(doc: _Doc) -> list[Row]:
    # Well-formed markdown links (resolution stays verification's business).
    empty_targets = len(re.findall(r"\[[^\]]+\]\(\s*\)", doc.text))
    return [
        _row(
            "qa:links",
            empty_targets == 0,
            f"{empty_targets} link(s) with empty targets" if empty_targets else "",
        )
    ]


def _layout(doc: _Doc) -> list[Row]:
    # What the RENDERED pages show. Binary deliverables only; a markdown answer has no page.
    # The size unit rides along as the size check resolved it (words, slides or sheets).
    job = doc.job
    deliverable = job["deliverable"]
    if not deliverable.endswith((".docx", ".pptx", ".pdf", ".html")):
        return []
    rows = layout_rows(
        deliverable,
        str(job.get("kind") or ""),
        doc.unit,
        int(job.get("target_units") or 0),
        float(job.get("tolerance") or 0),
        job.get("rendition"),
    )
    return [_row(row["name"], row["ok"], row["detail"]) for row in rows]


# The document checks, in the order their rows appear.
_CHECKS: Final[tuple[Callable[[_Doc], list[Row]], ...]] = (
    _sections,
    _size,
    _figures,
    _truncation,
    _headings,
    _links,
    _layout,
)


def _read_outline(job: dict[str, Any]) -> str:
    if job.get("outline") and os.path.isfile(job["outline"]):
        with open(job["outline"], encoding="utf-8") as handle:
            return handle.read()
    return ""


def run(job: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        read = extract(job["deliverable"])
    except (UnsupportedFormat, ImportError) as gap:
        # The RUNNER cannot read this (an unknown extension, a frame-image gap): never a document fact,
        # so exit 3 = INFRA - billing a rebuild for it is the loop this closes.
        print(f"the runner cannot read this deliverable: {gap}", file=sys.stderr)
        raise SystemExit(3) from None
    except Exception as failed:
        return [_row("qa:extraction", False, f"the deliverable could not be read: {failed}")]
    text = read.markdown
    readable = bool(text.strip())
    cut = "read up to the reader's caps; the rest was not read" if read.truncated else ""
    checks = [_row("qa:extraction", readable, cut if readable else "extracted text is empty")]
    if not readable:
        return checks
    with open(EXTRACTED, "w", encoding="utf-8") as handle:
        handle.write(text)

    unit = str(job.get("unit") or "words")
    doc = _Doc(
        job=job,
        text=text,
        counted=read.units,
        outline_md=_read_outline(job),
        unit=unit if unit in ("slides", "sheets") else "words",
    )
    for check in _CHECKS:
        checks.extend(check(doc))
    return checks


if __name__ == "__main__":
    job_in = json.loads(sys.stdin.read() or "{}")
    print(json.dumps({"checks": run(job_in)}))
