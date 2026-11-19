# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The quote-match suite - **does the chair's table come from the answers?** (ADR-010).

Runs inside the frame (stdlib only, like the document suite), reading a job on stdin and writing checks
to stdout. A comparison table is where a synthesis can lie in two damaging ways, and this suite catches
each with a deterministic check:

1. **A manufactured consensus.** The chair writes "both answers agree margin is thin" and the reader
   trusts it *more* for carrying every model's weight - so if no answer said it, the table has
   laundered one model's guess into three models' agreement. `quotes:trace` catches it: **every span
   the report puts in quotation marks must appear, verbatim, in one of the answers** (its sources).
   A quote in no answer is named, with the answer it should have come from.

2. **A silently dropped seat.** A fleet answers with N models; a table that quietly tabulates N−1 of
   them looks complete and is a lie of omission - the reader chooses among answers they were never
   shown all of. `answers:covered` catches it: **the table has exactly one data row per answer the
   chair was given.** This is a fleet table's real, always-checkable honesty claim (a paraphrased
   table has nothing to quote, so `quotes:trace` skips - coverage is what a clean table passes on).

`quotes:trace` also serves any single answer: its sources are the task's brief and the pages the
task read (kept by the source store), and a quote it attributes to either must be in one of them. `answers:covered` fires only when the sources are fleet
answers (`answer-<model>.md`), so a plain document is untouched by it.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import cast

PASS, FAIL, SKIP = "pass", "fail", "skip"
STRUCTURAL, SUBSTANTIVE = "structural", "substantive"  # `schemas.verification.CheckKind`, declared per check
PAGE = "page-"  # a page the task read and kept, as `operations/sources.py` names it in the frame

# A quoted span worth checking: 8–400 chars inside straight or curly double quotes. Below 8 chars a
# "quote" is a word or a label ("A", "yes") - checking those is noise; above 400 it is a block the
# author is unlikely to have transcribed by hand.
_QUOTE = re.compile(r'[“"”]([^“"”]{8,400}?)[“"”]')


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
    """Fold unicode and collapse whitespace so a quote matches its source across a line wrap or a
    curly-vs-straight apostrophe - the differences a transcription picks up but a reader would not."""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip().lower()


def _table_row_count(report: str) -> int:
    """Data rows in the report's first markdown table (header and `|---|` rule excluded); a stray
    later table does not change the count."""
    rows: list[str] = []
    in_table = False
    for line in report.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            in_table = True
            if set(stripped) <= set("|:- "):  # the `|---|:--|` separator, not a data row
                continue
            rows.append(stripped)
        elif in_table:
            break  # the first table ended; count what we have
    return max(0, len(rows) - 1)  # minus the header row


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

    report = raw.decode("utf-8", errors="replace")
    present = {p: _normalise(Path(p).read_text(errors="replace")) for p in sources if Path(p).exists()}

    # **Coverage - a fleet table represents every seat it was given, and invents none**.
    # Substantive (not a `bytes:`/`sources:`/`suite:` name), so an honest table passes on it even
    # when it quotes nothing - which is what a paraphrased comparison legitimately does. Fires only
    # for fleet answers, so a plain document (whose source is a brief) never sees this check.
    fleet_answers = [p for p in sources if Path(p).name.startswith("answer-")]
    if fleet_answers:
        n, got = len(fleet_answers), _table_row_count(report)
        if got == n:
            checks.append(_check("answers:covered", SUBSTANTIVE, PASS, f"one row per answer ({n})"))
        else:
            checks.append(
                _check(
                    "answers:covered",
                    SUBSTANTIVE,
                    FAIL,
                    "the table drops or invents a seat",
                    expected=f"{n} row(s), one per answer given",
                    actual=f"{got} data row(s)",
                )
            )

    # Strip the punctuation English style tucks *inside* a closing quote - `"margin is thin,"` quotes
    # `margin is thin`, and the comma is the writer's, not the source's. Matching with it attached is a
    # false FAIL on faithful quotes, which is the failure that gets a verifier switched off.
    quotes = [
        q for q in (_normalise(m).strip(" ,.;:!?\u2014-") for m in _QUOTE.findall(report)) if len(q) >= 8
    ]

    if not present:
        # An answer to a question with no source brief has nothing to trace to - a SKIP,
        # not a fail. The step still ships (an all-skip suite is `unverified`, which the ladder does
        # not reject) and the judge weighs it. The FAIL is reserved for a *report* that has sources
        # and quotes one nobody made - a false FAIL here would sink every brief-less fleet.
        checks.append(
            _check("quotes:trace", SUBSTANTIVE, SKIP, "no sources to trace against - the judge weighs this")
        )
        return checks

    # A chair quotes the answers it was given; any other report may quote a page the task read too.
    traced = {p: body for p, body in present.items() if not (fleet_answers and Path(p).name.startswith(PAGE))}
    ungrounded = [q for q in quotes if not any(q in body for body in traced.values())]
    if not quotes:
        # A synthesis with no quotes is legal - the chair may paraphrase throughout - so this is a SKIP,
        # not a fail. The judge weighs an unquoted synthesis; this suite only polices what is quoted.
        checks.append(_check("quotes:trace", SUBSTANTIVE, SKIP, "the report quotes no source verbatim"))
    elif ungrounded:
        checks.append(
            _check(
                "quotes:trace",
                SUBSTANTIVE,
                FAIL,
                f"{len(ungrounded)} of {len(quotes)} quote(s) are in no source",
                expected="every quoted claim is verbatim from a source",
                actual="; ".join(f'"{q[:60]}…"' for q in ungrounded[:5]),
            )
        )
    else:
        checks.append(
            _check("quotes:trace", SUBSTANTIVE, PASS, f"{len(quotes)} quote(s), each traced to a source")
        )
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
