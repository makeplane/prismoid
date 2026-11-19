# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The deck suite - **do the slides' numbers come from the model?**.

Runs inside the frame (python-pptx is frame-only), reading a job on stdin and writing checks to
stdout. A deck is where a fabricated figure hides in plain sight: a slide says "revenue up 42%" and
nobody opens the model to check. This suite opens the `.pptx`, pulls every **substantial** number off
the slides - money, percentages, figures a reader would act on - and checks each one appears in the
sources the deck claims to be built from. A number on a slide that is in no source is a number the
author invented, and it is caught here.

**Only substantial numbers are grounded, on purpose.** A slide number ("3"), a bullet count, a year -
these are not claims a reader acts on, and holding them to a source produces false FAILs that get a
verifier switched off. So the grounded set is the figures that carry weight: `$`-money, `%`, and
magnitudes a person quotes. A figure is grounded by a normalised substring match, or by `derive`: one
dimensionally-legal step from the sources, the document suite's rule and code (`document.py` ships
beside this file - `COMPANIONS` in pipeline.py). A growth rate on a slide is a correct figure no
source prints; the working goes in the report, for the judge to confirm.

Deterministic: it does not judge whether the *story* is right (that is the cross-family judge), only
whether the numbers on the slides trace to the numbers in the sources.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import cast

# `document.py` rides beside this file, loaded by exact path: the device runs suites with `-I`, so the
# script's folder is not on sys.path, and nothing else may answer to the name. Registered before it
# runs because its dataclasses look their own module up.
_SPEC = importlib.util.spec_from_file_location("document", Path(__file__).with_name("document.py"))
assert _SPEC is not None and _SPEC.loader is not None
_document = sys.modules["document"] = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_document)
derive, numbers_in, qualify_tables = _document.derive, _document.numbers_in, _document.qualify_tables
extract, write_extract = _document.extract, _document.write_extract

PASS, FAIL, SKIP = "pass", "fail", "skip"
STRUCTURAL, SUBSTANTIVE = "structural", "substantive"  # `schemas.verification.CheckKind`, declared per check

# A "substantial" number: money ($1,234 / $4.2M), a percentage (42% / 42.0%), or a bare magnitude a
# reader quotes (>= 1000, or a decimal with a scale suffix). Slide ordinals and small counts are not.
_MONEY = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s?[kKmMbB]?")
_PERCENT = re.compile(r"\d+(?:\.\d+)?\s?%")
_SCALED = re.compile(r"\d+(?:\.\d+)?\s?[kKmMbB]\b")
_BIG = re.compile(r"\b\d{1,3}(?:,\d{3})+(?:\.\d+)?\b|\b\d{4,}(?:\.\d+)?\b")


def _check(
    name: str,
    kind: str,
    status: str,
    detail: str = "",
    *,
    expected: str | None = None,
    actual: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "kind": kind,
        "status": status,
        "detail": detail,
        "expected": expected,
        "actual": actual,
    }


def _normalise(text: str) -> str:
    """Fold width/accents and strip the separators a number wears differently in a slide than a source
    ($, commas, spaces, the scale letter's case) so `$4.2M` on a slide matches `4.2m` in a model."""
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"[\s,$%]", "", text).lower()


def _figures(text: str) -> list[str]:
    """The substantial numbers in a blob of text, as written."""
    seen: dict[str, str] = {}  # by normal form: `$13.2M` also matches _SCALED as `13.2M`, one figure
    for pattern in (_MONEY, _PERCENT, _SCALED, _BIG):
        for match in pattern.findall(text):
            seen.setdefault(_normalise(match), match.strip())
    return list(seen.values())


def _ground(figures: list[str], source: str) -> tuple[list[str], list[str]]:
    """The workings of the figures a source implies, and the figures nothing accounts for."""
    verbatim, pool = _normalise(source), numbers_in(source)
    derived: list[str] = []
    ungrounded: list[str] = []
    for figure in figures:
        if _normalise(figure) in verbatim:
            continue
        working = next((w for n in numbers_in(figure) if (w := derive(n, pool))), None)
        if working:
            derived.append(working)
        else:
            ungrounded.append(figure)
    return derived, ungrounded


def run(job: dict[str, object]) -> list[dict[str, object]]:
    artifact_path = str(job["artifact_path"])
    expected_sha = str(job["artifact_sha256"])
    sources = [str(s) for s in cast("list[object]", job.get("sources", []))]
    checks: list[dict[str, object]] = []

    raw = Path(artifact_path).read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha != expected_sha:
        return [
            _check(
                "bytes:sha256",
                STRUCTURAL,
                FAIL,
                "published bytes do not match their digest",
                expected=expected_sha,
                actual=actual_sha,
            )
        ]
    checks.append(_check("bytes:sha256", STRUCTURAL, PASS, "matches the manifest digest"))

    deck = extract(artifact_path)
    if deck.units == 0:
        # `format:` prefix deliberately: having slides is STRUCTURE, not substance. Named `slides:present`
        # it counted as a substantive pass, and a sourceless deck rode "it has slides" to a PASS.
        return [*checks, _check("format:slides", STRUCTURAL, FAIL, "the deck has no slides")]
    checks.append(_check("format:slides", STRUCTURAL, PASS, f"{deck.units} slide(s)"))
    write_extract(job, deck.markdown)
    # The face a reader sees, charts included: a column's unit cue (`Revenue ($M)`) goes back on its cells.
    face = qualify_tables("\n\n".join(b.text for b in deck.blocks if b.kind not in ("slide", "notes")))

    present = [p for p in sources if Path(p).exists()]
    if not present:
        # Say it out loud: no source means the grounding check proved nothing, so it does not
        # get to report a pass it never earned.
        checks.append(
            _check("numbers:grounded", SUBSTANTIVE, SKIP, "no readable sources - nothing to ground against")
        )
        return checks

    figures = _figures(face)
    derived, ungrounded = _ground(figures, "\n".join(Path(p).read_text(errors="replace") for p in present))

    if not figures:
        checks.append(_check("numbers:grounded", SUBSTANTIVE, SKIP, "no substantial figures on the slides"))
    elif ungrounded:
        checks.append(
            _check(
                "numbers:grounded",
                SUBSTANTIVE,
                FAIL,
                f"{len(ungrounded)} of {len(figures)} figure(s) are in no source",
                expected="every figure on a slide is in a source, or derived from it in one step",
                actual="; ".join(ungrounded[:8]),
            )
        )
    else:
        detail = f"{len(figures) - len(derived)} verbatim, {len(derived)} derived"
        if derived:
            detail += " · derived (plausible; the judge confirms the working): " + "; ".join(derived[:8])
        checks.append(_check("numbers:grounded", SUBSTANTIVE, PASS, detail))
    return checks


def main() -> None:
    job = json.loads(sys.stdin.read())
    try:
        checks = run(job)
    except Exception as exc:  # a suite that crashed did not pass - say which, loudly
        checks = [_check("suite:ran", STRUCTURAL, FAIL, f"{type(exc).__name__}: {exc}")]
    json.dump({"checks": checks}, sys.stdout)


if __name__ == "__main__":
    main()
