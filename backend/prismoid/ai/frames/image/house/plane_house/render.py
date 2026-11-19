# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Render a deliverable to pages and LOOK at it (ADR-034) - read-only on the deliverable.

    python3 -m plane_house.render /work/deck.pptx --pages /work/pages [--dpi 80]

docx/pptx/html → PDF through LibreOffice headless (the preview runner's conversion, same
fresh recalc-forced profile), pdf passthrough; then `pdftoppm` rasterizes the sampled pages to
PNGs the agent reads with `read_file` (images come back as pixels). Prints the geometry
findings and writes `<pages>/pages.json`. NEVER writes to the deliverable: the one-save-then-
publish hash law holds because this module only ever opens it for reading.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

LO_PROFILE = "/tmp/.plane-lo-render"
SOFFICE_TIMEOUT = 240
MAX_RENDITION = 60_000_000
DEFAULT_DPI = 80
MAX_SAMPLED_PAGES = 12

RECALC_XCU = (
    "<?xml version='1.0' encoding='UTF-8'?>\n"
    "<oor:items xmlns:oor='http://openoffice.org/2001/registry' "
    "xmlns:xs='http://www.w3.org/2001/XMLSchema'>\n"
    " <item oor:path='/org.openoffice.Office.Calc/Formula/Load'>"
    "<prop oor:name='OOXMLRecalcMode' oor:op='fuse'><value>0</value></prop></item>\n"
    " <item oor:path='/org.openoffice.Office.Calc/Formula/Load'>"
    "<prop oor:name='ODFRecalcMode' oor:op='fuse'><value>0</value></prop></item>\n"
    "</oor:items>\n"
)

CONVERTIBLE = (".docx", ".pptx", ".html", ".htm", ".xlsx", ".odt", ".odp")


def to_pdf(path: str, outdir: str, timeout: int = SOFFICE_TIMEOUT) -> str | None:
    """The deliverable as a PDF: itself if it is one, else LibreOffice's conversion. None when
    the conversion fails - the caller degrades, never raises for the user. On a device whose fence
    cannot run LibreOffice (PRISMOID_NO_RENDITIONS) it is never tried: it would hang out its timeout."""
    if path.lower().endswith(".pdf"):
        return path
    if os.environ.get("PRISMOID_NO_RENDITIONS") or not path.lower().endswith(CONVERTIBLE):
        return None
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(f"{LO_PROFILE}/user", exist_ok=True)
    with open(f"{LO_PROFILE}/user/registrymodifications.xcu", "w") as fh:
        fh.write(RECALC_XCU)
    try:
        done = subprocess.run(
            [
                "soffice",
                "--headless",
                f"-env:UserInstallation=file://{LO_PROFILE}",
                "--convert-to",
                "pdf",
                "--outdir",
                outdir,
                path,
            ],
            capture_output=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    stem = os.path.splitext(os.path.basename(path))[0]
    out = os.path.join(outdir, f"{stem}.pdf")
    if done.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) > MAX_RENDITION:
        return None
    return out


def page_count(pdf: str) -> int:
    try:
        from pypdf import PdfReader

        return len(PdfReader(pdf).pages)
    except Exception:
        return 0


def sample_pages(
    n_pages: int, flagged: set[int] | frozenset[int] | None = None, cap: int = MAX_SAMPLED_PAGES
) -> list[int]:
    """Which pages a reviewer sees: the first three, the last two, every flagged page (figures,
    tables, findings), sorted unique, capped - the flagged pages win over the tail when the cap
    bites. Pure."""
    if n_pages <= 0:
        return []
    wanted: list[int] = []
    for p in (1, 2, 3):
        if p <= n_pages:
            wanted.append(p)
    for p in sorted(flagged or ()):
        if 1 <= p <= n_pages:
            wanted.append(p)
    for p in (n_pages - 1, n_pages):
        if p >= 1:
            wanted.append(p)
    unique = sorted(set(wanted))
    if len(unique) <= cap:
        return unique
    keep = {p for p in (1, 2, 3) if p <= n_pages}
    for p in sorted(flagged or ()):
        if len(keep) >= cap:
            break
        if 1 <= p <= n_pages:
            keep.add(p)
    for p in (n_pages, n_pages - 1):
        if len(keep) >= cap:
            break
        if p >= 1:
            keep.add(p)
    return sorted(keep)


def rasterize(pdf: str, out_dir: str, dpi: int = DEFAULT_DPI, pages: list[int] | None = None) -> list[str]:
    """PNG per requested page via pdftoppm → `<out_dir>/page-NN.png`. Missing poppler or a
    failed run returns [] - the caller's checks still stand on the PDF."""
    os.makedirs(out_dir, exist_ok=True)
    targets = pages if pages is not None else list(range(1, page_count(pdf) + 1))
    made: list[str] = []
    for p in targets:
        out_stem = os.path.join(out_dir, f"page-{p:02d}")
        try:
            done = subprocess.run(
                [
                    "pdftoppm",
                    "-r",
                    str(dpi),
                    "-png",
                    "-singlefile",
                    "-f",
                    str(p),
                    "-l",
                    str(p),
                    pdf,
                    out_stem,
                ],
                capture_output=True,
                timeout=60,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return made
        if done.returncode == 0 and os.path.isfile(out_stem + ".png"):
            made.append(out_stem + ".png")
    return made


def render(
    path: str,
    pages_dir: str,
    dpi: int = DEFAULT_DPI,
    *,
    kind: str = "",
    target_units: int = 0,
    tolerance: float = 0.0,
    pdf: str | None = None,
) -> dict:
    """The whole pass: PDF (`pdf` when the caller already has one - the harvest rendition),
    geometry findings, sampled PNGs, a manifest."""
    from plane_house import geometry

    os.makedirs(pages_dir, exist_ok=True)
    if not kind:  # the caller's kind wins; the extension is the honest default
        kind = "deck" if path.lower().endswith(".pptx") else "document"
    findings: list[dict] = []
    flagged: set[int] = set()
    if path.lower().endswith(".pptx"):
        findings.extend(geometry.deck_layout_checks(path))
    elif path.lower().endswith(".docx"):
        findings.extend(geometry.docx_layout_checks(path))
    pdf = pdf or to_pdf(path, os.path.join(pages_dir, "_pdf"))  # a ready rendition skips soffice
    n = page_count(pdf) if pdf else 0
    if pdf:
        findings.extend(
            geometry.pdf_layout_checks(pdf, kind=kind, target_units=target_units, tolerance=tolerance)
        )
    for f in findings:
        if f.get("page"):
            flagged.add(int(f["page"]))
    sampled = sample_pages(n, flagged)
    pngs = rasterize(pdf, pages_dir, dpi, sampled) if pdf else []
    manifest = {
        "deliverable": path,
        "pdf": pdf,
        "pages": n,
        "sampled": sampled,
        "images": pngs,
        "findings": findings,
        "face_ids": geometry.face_fig_ids(path),
    }
    with open(os.path.join(pages_dir, "pages.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest


def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("deliverable")
    ap.add_argument("--pages", default="/work/pages")
    ap.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    args = ap.parse_args(argv)
    m = render(args.deliverable, args.pages, args.dpi)
    if not m["pdf"]:
        if os.environ.get("PRISMOID_NO_RENDITIONS"):
            print("no PDF on this computer: geometry checks only")
        else:
            print("could not render to PDF (LibreOffice failed or unsupported type); geometry checks only")
    print(f"{m['pages']} page(s); sampled {m['sampled']} → {len(m['images'])} PNG(s) in {args.pages}")
    for png in m["images"]:
        print(f"  look: {png}")
    bad = [f for f in m["findings"] if not f.get("ok", True)]
    if m["face_ids"]:
        print(f"NOT CLEAN - internal figure ids on the face: {', '.join(m['face_ids'][:5])}")
    if bad:
        print(f"NOT CLEAN - {len(bad)} layout problem(s):")
        for f in bad:
            print(f"  - {f['name']}: {f.get('detail', '')}")
        return 1
    if m["face_ids"]:
        return 1
    print("layout clean: read the PNGs before you publish")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
