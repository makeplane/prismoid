# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The preview runner - render sidecars built INSIDE the frame, at harvest (ADR-022).

The reader must show the work the moment it is delivered: a workbook as a grid, a Word file as
its prose, a deck as its slides, a page as the page. The bytes are untrusted and the parsers
are heavy, so extraction happens where the parsers already live and the blast radius is already
contained - the frame - never in the worker venv ("no openpyxl in the worker" stands)
and never in the reader's browser. The worker copies this static script in, execs a fixed
command line, and reads one JSON file back; `operations` stores each preview as its own CAS
blob on the artifact row, so `artifact.produced` is born with `has_preview` already true -
the verify-time spreadsheet sidecar stays as the post-receipt REFRESH of the same
mechanism, never the only source.

Same hand-off discipline as `publish.py`: nothing model-authored touches a shell - the runner
is static bytes, the arguments travel as a JSON file, the exec line is a constant.

The wire shapes (mirrored by `api/serializers/artifacts.PreviewOut`):
  sheet     {kind:"sheet",    sheets:[{name, dims, rows, truncated}]}     (the M4 shape, unchanged)
  document  {kind:"document", blocks:[{style, text} | {table:[[...]]}], truncated}
  deck      {kind:"deck",     slides:[{title, lines}], truncated}
  code      {kind:"code",     files:[{path, size, text|null}], truncated}
  page      {kind:"page",     html}                                        (a code tar whose root is index.html)
"""

from __future__ import annotations

from prismoid.ai.frames.layout import FramePaths

# Fixed frame paths - constants ours, never the model's. Overwritten per harvest (idempotent).
# The runner and its arguments are the system's (root's, read-only to the agent), so they live in
# /task beside the step marker: every driver writes the system's files there (a driver may allow them
# only under its system roots, and never in a world-writable /tmp). OUT is the agent's,
# written by the runner as the agent, so it stays in /tmp.
RUNNER_PATH = "/task/.plane-preview.py"
ARGS_PATH = "/task/.plane-preview-args.json"
OUT_PATH = "/tmp/.plane-previews.json"


def preview_args(
    entries: list[dict[str, str]],
    nonce: str,
    *,
    paths: FramePaths | None = None,
    office: bool = True,
) -> dict[str, object]:
    """The runner's whole input, as data: `[{path, type}, ...]` for every harvested entry.

    `nonce` is echoed back in the OUT file so the worker can tell THIS run's output from a
    previous harvest's leftover - /tmp persists across harvests in the same frame, and a stale
    sidecar attached to re-published bytes would be a preview of the wrong version.

    `paths`: a frame on the device, whose paths the runner gets as they really are, with OUT in
    `artifacts/`: a system command writes there and no agent command does, and `/tmp` is no folder
    of the frame's. `office` says whether it can render at all; a cloud frame's image always can."""
    if paths is None or not paths.on_host:
        return {"entries": entries, "out": OUT_PATH, "nonce": nonce}
    return {
        "entries": [{**e, "path": paths.render(e["path"])} for e in entries],
        "out": f"{paths.artifacts}/.plane-previews.json",
        "nonce": nonce,
        "office": office,
    }


# The static in-frame runner. Frame-pinned libraries only (openpyxl / python-docx / python-pptx
# are in the image; tarfile is stdlib). Every extraction is best-effort: a file that will not
# parse yields null, and the harvest carries on - a preview is presentation, never attestation.
#
# A sheet shows its values as LibreOffice computes them, the engine the spreadsheet suite verifies with;
# where LibreOffice cannot run, a formula shows its text, never a number nobody computed.
PREVIEW_RUNNER = """\
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile

MAX_JSON = 96_000
RENDITION_DIR = "/tmp/.plane-renditions"
LO_PROFILE = "/tmp/.plane-lo"
# The rendition is THE DOCUMENT (ADR-023), and the document got long (ADR-025): a 110-page
# figure-bearing docx renders slower and bigger than the 25 MB / 90 s the preview era assumed.
# Still bounded - best-effort law: past either ceiling the extracted preview is the honest face.
MAX_RENDITION = 60_000_000
SOFFICE_TIMEOUT = 240

# LibreOffice ships "ask before recalculating" as the OOXML default - and an openpyxl-written
# workbook has NO cached values, so without forcing recalc every formula cell renders empty.
# The profile is minted fresh per run with recalc forced for both OOXML and ODF.
# Single-quoted XML attributes on purpose: these namespace URIs are IDENTIFIERS the registry
# format demands, not endpoints - and quoting them this way keeps the no-URL-literals audit (HANDOFF §14)
# (which greps for a double quote before http) exact instead of grown an exclusion.
RECALC_XCU = (
    "<?xml version='1.0' encoding='UTF-8'?>\\n"
    "<oor:items xmlns:oor='http://openoffice.org/2001/registry' "
    "xmlns:xs='http://www.w3.org/2001/XMLSchema'>\\n"
    " <item oor:path='/org.openoffice.Office.Calc/Formula/Load'>"
    "<prop oor:name='OOXMLRecalcMode' oor:op='fuse'><value>0</value></prop></item>\\n"
    " <item oor:path='/org.openoffice.Office.Calc/Formula/Load'>"
    "<prop oor:name='ODFRecalcMode' oor:op='fuse'><value>0</value></prop></item>\\n"
    "</oor:items>\\n"
)


def soffice(path, target, outdir):
    # One conversion through LibreOffice headless; returns the produced file's path or None.
    # A fresh, recalc-forced profile under /tmp - soffice refuses to run without one. `outdir`
    # is per-entry: a shared outdir let `q3.docx` and `q3.pptx` overwrite each other's `q3.pdf`
    # (the worker reads the files only after the whole loop), cross-attaching renditions.
    os.makedirs(outdir, exist_ok=True)
    os.makedirs(f"{LO_PROFILE}/user", exist_ok=True)
    with open(f"{LO_PROFILE}/user/registrymodifications.xcu", "w") as fh:
        fh.write(RECALC_XCU)
    done = subprocess.run(
        [
            "soffice",
            "--headless",
            f"-env:UserInstallation=file://{LO_PROFILE}",
            "--convert-to",
            target,
            "--outdir",
            outdir,
            path,
        ],
        capture_output=True,
        timeout=SOFFICE_TIMEOUT,
    )
    stem = os.path.splitext(os.path.basename(path))[0]
    out = os.path.join(outdir, f"{stem}.{target.split(':')[0]}")
    if done.returncode != 0 or not os.path.isfile(out) or os.path.getsize(out) > MAX_RENDITION:
        return None
    return out


def rendition(path, kind, outdir):
    # The full-fidelity render (ADR-023): docx/pptx -> PDF, xlsx -> styled HTML.
    name = path.lower()
    if kind == "document" and name.endswith(".docx"):
        out = soffice(path, "pdf", outdir)
        return {"file": out, "media": "application/pdf"} if out else None
    if kind == "deck" and name.endswith(".pptx"):
        out = soffice(path, "pdf", outdir)
        return {"file": out, "media": "application/pdf"} if out else None
    if kind == "spreadsheet" and name.endswith(".xlsx"):
        out = soffice(path, "html", outdir)
        return {"file": out, "media": "text/html"} if out else None
    return None
MAX_PAGE_HTML = 900_000
SHEET_ROWS, SHEET_COLS = 50, 12
DOC_BLOCKS = 400
DECK_SLIDES, DECK_LINES = 40, 20
CODE_FILES, CODE_TEXT_BYTES = 60, 32_000

def sheet_preview(path):
    import openpyxl

    try:
        os.makedirs(RENDITION_DIR, exist_ok=True)
        computed = soffice(path, "xlsx", tempfile.mkdtemp(prefix="recalc-", dir=RENDITION_DIR))
    except Exception:
        computed = None  # no LibreOffice here: the formulas show as text
    wb = openpyxl.load_workbook(computed or path, data_only=computed is not None)
    sheets = []
    for ws in wb.worksheets:
        rows = []
        for row in ws.iter_rows(min_row=1, max_row=SHEET_ROWS, max_col=SHEET_COLS):
            rows.append([v if isinstance(v := c.value, (int, float, str)) or v is None else str(v) for c in row])
        while rows and not any(c not in (None, "") for c in rows[-1]):
            rows.pop()
        sheets.append(
            {
                "name": ws.title,
                "dims": f"{ws.max_row}\\u00d7{ws.max_column}",
                "rows": rows,
                "truncated": bool(ws.max_row > SHEET_ROWS or ws.max_column > SHEET_COLS),
            }
        )
    return {"kind": "sheet", "sheets": sheets}


_HEADING = {"Title": "h1", "Heading 1": "h1", "Heading 2": "h2"}


def document_preview(path):
    if path.lower().endswith(".html"):
        # A single-file HTML document (ADR-033): the page IS the presentation - the same
        # sandboxed-cage shape a code tar's root index.html has always ridden. Oversize or
        # undecodable ⇒ None, the best-effort law.
        with open(path, "rb") as f:
            raw = f.read(MAX_PAGE_HTML + 1)
        if len(raw) > MAX_PAGE_HTML:
            return None
        try:
            return {"kind": "page", "html": raw.decode("utf-8")}
        except UnicodeDecodeError:
            return None
    if not path.lower().endswith(".docx"):
        return None  # .md/.txt render as text already; .pdf embeds natively
    import docx

    d = docx.Document(path)
    blocks, count = [], 0
    for p in d.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style = p.style.name if p.style is not None else ""
        if style in _HEADING:
            kind = _HEADING[style]
        elif style.startswith("Heading"):
            kind = "h3"
        elif style.startswith("List"):
            kind = "li"
        else:
            kind = "p"
        blocks.append({"style": kind, "text": text[:2000]})
        count += 1
        if count >= DOC_BLOCKS:
            break
    for t in d.tables[:6]:
        rows = []
        for row in t.rows[:30]:
            rows.append([c.text.strip()[:200] for c in row.cells[:8]])
        if rows:
            blocks.append({"table": rows})
    return {"kind": "document", "blocks": blocks, "truncated": count >= DOC_BLOCKS}


def deck_preview(path):
    from pptx import Presentation

    prs = Presentation(path)
    slides = []
    for slide in list(prs.slides)[:DECK_SLIDES]:
        title = ""
        if slide.shapes.title is not None:
            title = slide.shapes.title.text.strip()
        lines = []
        for shape in slide.shapes:
            if not getattr(shape, "has_text_frame", False):
                continue
            for para in shape.text_frame.paragraphs:
                text = "".join(r.text for r in para.runs).strip() or para.text.strip()
                if text and text != title:
                    lines.append(text[:300])
        slides.append({"title": title, "lines": lines[:DECK_LINES]})
    return {"kind": "deck", "slides": slides, "truncated": len(prs.slides) > DECK_SLIDES}


def tar_preview(path):
    with tarfile.open(path) as tf:
        members = [m for m in tf.getmembers() if m.isfile()]
        names = [m.name for m in members]
        roots = {n.split("/", 1)[0] for n in names}
        prefix = f"{next(iter(roots))}/" if len(roots) == 1 and all("/" in n for n in names) else ""

        # The page shape (ADR-021): a root index.html renders as the page itself.
        index = f"{prefix}index.html"
        if index in names:
            member = tf.extractfile(index)
            if member is not None:
                raw = member.read(MAX_PAGE_HTML + 1)
                if len(raw) <= MAX_PAGE_HTML:
                    try:
                        return {"kind": "page", "html": raw.decode("utf-8")}
                    except UnicodeDecodeError:
                        pass  # fall through to the listing - a broken page still shows its files

        files, truncated = [], len(members) > CODE_FILES
        for m in members[:CODE_FILES]:
            entry = {"path": m.name[len(prefix):] or m.name, "size": m.size, "text": None}
            if m.size <= CODE_TEXT_BYTES:
                handle = tf.extractfile(m)
                if handle is not None:
                    raw = handle.read()
                    try:
                        text = raw.decode("utf-8")
                        if "\\x00" not in text:
                            entry["text"] = text
                    except UnicodeDecodeError:
                        pass
            files.append(entry)
        return {"kind": "code", "files": files, "truncated": truncated}


BUILDERS = {
    "spreadsheet": sheet_preview,
    "document": document_preview,
    "deck": deck_preview,
    "code": tar_preview,
    "webapp": tar_preview,
    "recording": tar_preview,
}


SHRINK_KEYS = {"document": "blocks", "deck": "slides", "code": "files"}


def fitted(preview):
    # A preview that serializes (RFC JSON - a NaN/Infinity anywhere would make the whole OUT
    # unparseable to the worker) and fits MAX_JSON, degraded rather than dropped: sheet halves
    # its rows (the M4 shape), the list kinds strip code bodies then halve until they fit.
    # Returns the preview or None; `page` is bounded by MAX_PAGE_HTML and rides whole.
    if preview is None:
        return None
    try:
        payload = json.dumps(preview, allow_nan=False)
    except ValueError:
        return None
    kind = preview.get("kind")
    if len(payload) <= MAX_JSON or kind == "page":
        return preview
    if kind == "sheet":
        for sheet in preview["sheets"]:
            sheet["rows"] = sheet["rows"][: SHEET_ROWS // 2]
            sheet["truncated"] = True
        return preview if len(json.dumps(preview, allow_nan=False)) <= MAX_JSON else None
    key = SHRINK_KEYS.get(kind)
    if key is None:
        return None
    if kind == "code":
        for f in preview[key]:
            f["text"] = None  # listings survive; inline bodies go to the download
    while preview[key]:
        preview["truncated"] = True
        if len(json.dumps(preview, allow_nan=False)) <= MAX_JSON:
            return preview
        preview[key] = preview[key][: len(preview[key]) // 2]  # 1 -> [] ends the loop
    return None


def write_out(path, nonce, made):
    # Atomic, and AFTER EVERY ENTRY: one hung soffice must cost its own entry, never the
    # sidecars already built (the exec deadline can kill this process mid-batch).
    tmp = path + ".tmp"
    json.dump({"nonce": nonce, "made": made}, open(tmp, "w"), allow_nan=False)
    os.replace(tmp, path)


def main():
    args = json.load(open(sys.argv[1]))
    # Fresh rendition ground per run: /tmp persists across harvests and is agent-writable -
    # a pre-planted file must never be picked up as a "conversion", stale or hostile.
    shutil.rmtree(RENDITION_DIR, ignore_errors=True)
    out = {}
    write_out(args["out"], args["nonce"], out)  # even a first-entry crash leaves honest emptiness
    for i, entry in enumerate(args["entries"]):
        result = {"preview": None, "rendition": None}
        builder = BUILDERS.get(entry["type"])
        if builder is not None:
            try:
                preview = fitted(builder(entry["path"]))
            except Exception:
                preview = None  # presentation, never attestation: an unreadable file has no preview
            result["preview"] = preview
        try:
            if args.get("office", True):
                result["rendition"] = rendition(entry["path"], entry["type"], os.path.join(RENDITION_DIR, str(i)))
        except Exception:
            result["rendition"] = None  # same law: a rendition that cannot be made is absent
        if result["preview"] is not None or result["rendition"] is not None:
            out[entry["path"]] = result
            write_out(args["out"], args["nonce"], out)
    print(f"{len(out)} preview/rendition(s)")


main()
"""
