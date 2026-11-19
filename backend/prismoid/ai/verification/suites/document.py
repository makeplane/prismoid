# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The document suite - **this file runs inside the frame, not in the worker.**

It is copied into a sandbox and executed there (`ai/verification/pipeline.py`), so it imports
the standard library and the one reader (`extract.py`) and nothing else. Not a style rule: the
spreadsheet suite opens an `.xlsx` the model wrote, and the code suite *builds and runs* a program
the model wrote. You do not do either of those in the process that holds the database connection,
and every suite plays by the same rules.

It reads a job on stdin and writes checks to stdout, both JSON. It has no network, no credentials,
and no way to reach anything but the two things it was handed: the published bytes, and the sources
they claim to be derived from.

────────────────────────────────────────────────────────────────────────────────────────────────

**What it can prove, and what it deliberately cannot.**

Deterministic checks catch what is *mechanically* checkable - a number that appears nowhere in the
source, a quotation nobody said, a link to a page that was never fetched. They cannot catch a
plausible sentence that is simply untrue, and pretending otherwise would be the most dangerous
thing this file could do. That is the cross-family judge's half of the job, and neither
half is verification on its own.

So: this suite is *necessary*, not *sufficient*, and it says so in its own report.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, DivisionByZero, InvalidOperation
from enum import StrEnum
from itertools import permutations
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

PASS, FAIL, SKIP = "pass", "fail", "skip"
STRUCTURAL, SUBSTANTIVE = "structural", "substantive"  # `schemas.verification.CheckKind`, declared per check

# How many source numbers the search for a derivation reads. The search is over ordered pairs, so it
# is quadratic; 400 numbers is 160k pairs × 7 formulas, which runs in well under a second. Beyond
# that we say so in the report rather than quietly checking less - a cap nobody is told about is a
# cap that becomes a lie.
MAX_DERIVATION_POOL = 400


# ── reading a document ────────────────────────────────────────────────────────────────────────


def to_prose(markdown: str) -> str:
    """Markdown -> the words a reader reads: list markers, heading numbers and URLs are structure,
    not figures to check."""
    text = qualify_tables(markdown)  # before anything flattens the columns away
    text = re.sub(r"```.*?```", " ", text, flags=re.S)  # fenced code
    text = re.sub(r"`[^`]*`", " ", text)  # inline code
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)  # links: keep the text, drop the target

    lines: list[str] = []
    for raw in text.splitlines():
        line = re.sub(r"^\s{0,3}#{1,6}\s+", "", raw)  # headings
        line = re.sub(r"^\s*>\s?", "", line)  # blockquotes
        line = re.sub(r"^\s*[-*+]\s+", "", line)  # bullets
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)  # ordered-list markers
        lines.append(line)

    return re.sub(r"[*_~]", "", "\n".join(lines))  # emphasis


# ── tables: the unit lives in the header, and the cell is naked without it ─────────────────────
#
# `| Revenue | 41.6 |` under a `Q3 ($M)` header is **$41.6M**. Read the cell alone and it is the
# bare count 41.6 - a different quantity, in a different dimension, that appears in no source.
#
# Dimensional analysis (see `_candidates`) is the right idea and this is its price: a unit system is
# only as good as its unit *recovery*. A table is the one place in prose where the unit is
# deliberately factored out of the value and stated once, above it - so it has to be put back before
# anything downstream reads the number, or the check punishes the document for being well formatted.

ROW = re.compile(r"^\s*\|(.+)\|\s*$")
DIVIDER = re.compile(r"^\s*\|[\s:|-]+\|\s*$")
BARE = re.compile(r"^\(?-?\d{1,3}(?:,\d{3})*(?:\.\d+)?\)?$|^\(?-?\d+(?:\.\d+)?\)?$")
SCALE_CUE = re.compile(r"\b(k|m|bn|b|thousands?|millions?|billions?)\b", re.I)


def _cells(row: str) -> list[str]:
    inner = ROW.match(row)
    return [c.strip() for c in inner.group(1).split("|")] if inner else []


def _unit_cue(header: str) -> tuple[str, str]:
    """What a bare cell under this header *means*: a prefix and a suffix to put back on it."""
    prefix = next((s for s in MONEY_SIGN if s in header), "")
    scale = SCALE_CUE.search(header)
    suffix = scale.group(1)[0].lower() if scale else ""
    if "%" in header:
        return "", "%"  # a percent column is never also a money column
    return prefix, suffix


def qualify_tables(markdown: str) -> str:
    """Give every bare table cell the unit its column (or its row label) declares."""
    lines = markdown.splitlines()
    out = list(lines)
    i = 0
    while i + 1 < len(lines):
        if not (ROW.match(lines[i]) and DIVIDER.match(lines[i + 1])):
            i += 1
            continue

        columns = [_unit_cue(c) for c in _cells(lines[i])]
        j = i + 2
        while j < len(lines) and ROW.match(lines[j]) and not DIVIDER.match(lines[j]):
            cells = _cells(lines[j])
            label = _unit_cue(cells[0]) if cells else ("", "")  # `| Revenue ($M) | 41.6 |`
            for k, cell in enumerate(cells):
                if not BARE.match(cell):
                    continue
                prefix, suffix = columns[k] if k < len(columns) else ("", "")
                if not (prefix or suffix):
                    prefix, suffix = label
                if prefix or suffix:
                    cells[k] = f"{prefix}{cell}{suffix}"
            out[j] = "| " + " | ".join(cells) + " |"
            j += 1
        i = j
    return "\n".join(out)


# ── the document's own working is not a claim about the world ─────────────────────────────────
#
# A memo that shows `22.7% = ($41.6M - $33.9M) / $33.9M × 100 = 0.2272` is being *more* honest than
# one that does not. Read naively, the `100` (a percent-conversion constant) and the `0.2272` (an
# intermediate) are figures in no source, and the document fails for showing its working - the
# worst lesson a verifier could teach.
#
# So a line that displays arithmetic loses its working (`strip_shown_working`), and the figure it states
# still grounds on its own: `revenue = $41.6M x 4 = $166.4M` drops the operands and checks the $166.4M.

EQUATION = re.compile(r"[+×÷/*]|(?<=\s)[-x](?=\s)")

# The apparatus itself, and it is only ever these two things:
#
#   `× 100`   - the percent conversion. It is a unit change, not a quantity. It appeared in the
#               ungrounded list of *every* failure in two consecutive golden runs.
#   `0.227`   - the intermediate, before the conversion. A ratio in (0, 1) with no unit is never a
#               claim a reader acts on: a real one is written `22.7%` or `$0.23M`, and that written
#               form is checked. This one is the model showing its arithmetic.
CONVERSION = re.compile(r"[×x*/]\s*100\b", re.I)
INTERMEDIATE = re.compile(r"(?<![\w.$€£¥])0\.\d{2,}(?![\d%\w])")

# A term, and the operators that can join two of them into a shown calculation. The ASCII operators
# must be space-padded: without that, `2026-2027` is a subtraction and `3x4` is a product, and the
# strip would start eating dates and dimensions. `×` and `÷` are unambiguous and need no such guard.
TERM = r"\(?\s*[$€£¥]?\d[\d,]*(?:\.\d+)?\s*[KkMmBb]?%?\s*\)?"
OPERATOR = r"(?:\s+[-+*/x]\s+|\s*[×÷−]\s*)"
EXPRESSION = re.compile(rf"{TERM}(?:{OPERATOR}{TERM})+")


def strip_shown_working(prose: str) -> str:
    """Remove the document's displayed arithmetic (operands, `x 100`, raw ratios on an equation line)
    but not its claims; every other figure on the line is still checked."""
    out: list[str] = []
    for line in prose.splitlines():
        if "=" in line and EQUATION.search(line):
            line = EXPRESSION.sub(" ", line)  # the operands and the operators joining them
            line = CONVERSION.sub(" ", line)  # a `× 100` that survived on its own
            line = INTERMEDIATE.sub(" ", line)  # the un-converted ratio, `0.4495`
        out.append(line)
    return "\n".join(out)


def normalise(text: str) -> str:
    """Whitespace collapsed, unicode folded. Two documents that read identically must compare
    identically, or the check fails on a line wrap."""
    folded = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", folded).strip()


# ── numbers ───────────────────────────────────────────────────────────────────────────────────

# A numeral, and only a numeral. The lookbehind keeps us out of `v1.2`, `FY2026` and `2026-07-14`'s
# tail; there is no trailing guard, so `$41.6M` yields 41.6 (the scale letter is prose, and the
# source spells it the same way).
NUMERAL = re.compile(r"(?<![\w/.\-])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")

# What a number *is*. This is the load-bearing idea in the file - see `derive`.
MONEY_SIGN = "$€£¥"
PERCENT = re.compile(r"^(?:\.{2,3}|…)?\s*(%|per ?cent\b|percentage point|pp\b|basis points\b|bps\b)", re.I)

# The magnitude a numeral is written at. **Not decoration.** `$3.1M × 20% = $620K` is correct
# arithmetic, and a verifier that keeps `3.1` and `620` and throws away the `M` and the `K` cannot
# see it - it reports a fabrication and fails a document that was right. A verifier that fails
# correct work is a verifier somebody switches off.
SCALE = re.compile(r"^\s{0,1}(k|m|bn|b|thousand|million|billion)\b", re.I)
SCALES: dict[str, Decimal] = {
    "k": Decimal(1_000),
    "thousand": Decimal(1_000),
    "m": Decimal(1_000_000),
    "million": Decimal(1_000_000),
    "b": Decimal(1_000_000_000),
    "bn": Decimal(1_000_000_000),
    "billion": Decimal(1_000_000_000),
}

# A bare integer at or below this is a **count of the document's own parts**, not a claim about the
# world: "3 key risks", "the 2 customers", "5 findings". The one carve-out in the suite, and it is
# named out loud in every report that uses it.
BARE_COUNT_MAX = Decimal(12)

# **A number's own notation says how much rounding it is admitting to, and that is the whole rule.**
#
# `23%` claims the truth lies in [22.5, 23.5). `46.5%` claims [46.45, 46.55). `$620K` claims half a
# thousand either way. Take the author at their word and you neither fail correct work nor wave
# through a fabrication - you enforce exactly the precision they claimed.
#
# The consequence is worth stating, because it is a feature: a memo that writes `47%` for a true
# 46.48% now **fails**, and should. That rounds to 46. A board paper that says 47 when it means 46.5
# is wrong, and the retry hands the model the arithmetic and lets it fix its own memo.


# A year, and the cue that tells you it is one. `Q3 2025` and `July 2027` are **labels**, not
# quantities - arithmetic on them is meaningless, and grounding them means adjudicating an
# inference, not checking a sum. A live memo wrote "$28.4M in Q3 2025" from a brief headed "Q3
# FY2026" that says "Q3 last year": correct, contextual, and utterly beyond a regex. **That is the
# judge's half of the job**, and this suite says so instead of guessing.
YEAR_CUE = re.compile(
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+$"
    r"|q[1-4]\s+$|fy\s*$|\b(?:in|since|during|until|through|year)\s+$",
    re.I,
)
YEAR_RANGE = (Decimal(1900), Decimal(2100))


class Unit(StrEnum):
    """What kind of quantity a number is. **Dimensional analysis is what makes `derive` a check
    rather than a coincidence generator** - see there."""

    MONEY = "money"
    PERCENT = "percent"
    COUNT = "count"  # people, units, anything unqualified but large enough to be a datum
    DATE = "date"  # a year. A label, not a quantity - see YEAR_CUE.
    WORD = "word"  # a bare small integer: prose, not data


@dataclass(frozen=True)
class Number:
    value: Decimal  # canonical: `$41.6M` is 41_600_000, so it can be compared with `$41,600,000`
    unit: Unit
    text: str = ""  # as written, for the report - nobody wants to read `41600000 − 22900000`
    places: int = 0  # decimals the author actually wrote; how much rounding they admit to

    @property
    def datum(self) -> bool:
        """A figure a reader would act on, so one this suite must ground: not a `WORD` or a `DATE`, which
        are reported and left to the judge."""
        return self.unit not in (Unit.WORD, Unit.DATE)

    @property
    def scale(self) -> Decimal:
        """The magnitude the author wrote at - `620K`'s tolerance is half a *thousand*, not half."""
        return SCALES.get(self.text.rstrip("%").lstrip("0123456789.,").strip().lower(), Decimal(1))


def numbers_in(prose: str) -> list[Number]:
    """Every numeral with its canonical value, unit and magnitude (`$41.6M` is 41,600,000); a count of
    things (`3 key risks`) is a word, not a figure."""
    out: list[Number] = []
    for match in NUMERAL.finditer(prose):
        literal = match.group(1)
        try:
            value = Decimal(literal.replace(",", ""))
        except InvalidOperation:  # pragma: no cover - the regex cannot produce this
            continue

        before = prose[max(0, match.start() - 2) : match.start()]  # `$41.6` and `$ 41.6` both count
        after = prose[match.end() : match.end() + 18]

        exponent = value.as_tuple().exponent
        places = -int(exponent) if isinstance(exponent, int) and exponent < 0 else 0

        scale_match = SCALE.match(after)
        scale = SCALES[scale_match.group(1).lower()] if scale_match else Decimal(1)
        suffix = scale_match.group(1) if scale_match else ""

        if PERCENT.match(after):
            unit, scale, suffix = Unit.PERCENT, Decimal(1), "%"  # 20% is 20, never 20 million
        elif any(sign in before for sign in MONEY_SIGN):
            unit = Unit.MONEY
        elif _is_a_year(literal, value, prose[: match.start()]):
            unit = Unit.DATE
        elif "." in literal or "," in literal or value * scale > BARE_COUNT_MAX:
            unit = Unit.COUNT
        else:
            unit = Unit.WORD

        out.append(Number(value * scale, unit, f"{literal}{suffix}", places))
    return out


def _is_a_year(literal: str, value: Decimal, preceding: str) -> bool:
    """Whether the numeral is a year: the value is in a plausible year range AND the words before it
    introduce a year (`Q3 2025`, `in 2026`; never `2000 units`)."""
    return (
        len(literal) == 4
        and literal.isdigit()
        and YEAR_RANGE[0] <= value <= YEAR_RANGE[1]
        and bool(YEAR_CUE.search(preceding[-14:]))
    )


# The floor under the tolerance.
#
# Half-a-ulp assumes the author *rounded*. Models **truncate**: `44.9%` for a true gross margin of
# 44.9519% is off by 0.05 of a point, and half-a-ulp rejects it *by 0.002*. Failing a memo over that
# teaches the reader that the verifier is broken, and a verifier nobody believes is a verifier
# nobody reads.
#
# 0.2% relative is a floor, never a cap - and the distinction is everything. It rescues a truncated
# `44.9%` (0.1% off) and still catches a fabricated `47%` for a true 46.48% (1.1% off), which is the
# one this suite exists to catch. Relative slack as a *cap* would do the opposite: reject correct
# roundings and let nothing extra through.
RELATIVE_FLOOR = Decimal("0.002")


def tolerance_for(number: Number) -> Decimal:
    """Half a unit in the last place written, at its magnitude, or 0.2% of the value, whichever is
    looser (models truncate rather than round)."""
    written = Decimal(1).scaleb(-number.places) / 2 * number.scale
    return max(written, abs(number.value) * RELATIVE_FLOOR)


def _candidates(target_unit: Unit, a: Number, b: Number) -> list[tuple[Decimal, str]]:
    """The dimensionally legal formulas for this target from two numbers (percent of like quantities,
    money with money): without the units every target finds a spurious fit."""
    out: list[tuple[Decimal, str]] = []
    x, y = a.text or a.value, b.text or b.value  # the report speaks the author's notation, not ours
    like = a.unit is b.unit and a.unit in (Unit.MONEY, Unit.COUNT)

    if target_unit is Unit.PERCENT:
        if like and b.value:
            # A **growth rate**. Not also `(a/b − 1) × 100`: the same formula, a phantom "+1 other fit".
            out.append(((a.value - b.value) / b.value * 100, f"({x} − {y}) / {y} × 100"))
            out.append(((a.value / b.value) * 100, f"({x} / {y}) × 100"))  # a share
        if like and a.value:
            # (a − b) / a - a **margin**. Gross margin is (revenue − cost) / *revenue*, the single
            # most common ratio in any financial document, and leaving it out cost a false FAIL.
            out.append(((a.value - b.value) / a.value * 100, f"({x} − {y}) / {x} × 100"))
        if a.unit is Unit.PERCENT and b.unit is Unit.PERCENT:
            out.append((a.value - b.value, f"{x} − {y} (percentage points)"))
            out.append((a.value + b.value, f"{x} + {y} (percentage points)"))

    elif target_unit in (Unit.MONEY, Unit.COUNT):
        if a.unit is target_unit and b.unit is target_unit:
            out.append((a.value - b.value, f"{x} − {y}"))
            out.append((a.value + b.value, f"{x} + {y}"))
        if a.unit is target_unit and b.unit is Unit.PERCENT:
            # "a 20% increase on a $3.1M lease" → $620K. Only findable because both sides are
            # canonicalised: 3_100_000 × 20 / 100 = 620_000, which is exactly what `$620K` means.
            out.append((a.value * b.value / 100, f"{x} × {y}"))
            out.append((a.value * (100 + b.value) / 100, f"{x} × (1 + {y})"))

    return out


def derive(target: Number, pool: list[Number]) -> str | None:
    """The closest dimensionally legal one-step derivation of `target` and how many others fit; a
    derivation is evidence, not proof, so the working goes to the judge."""
    slack = tolerance_for(target)
    if target.unit is Unit.PERCENT:  # "the remaining 69%": one percentage, not a pair to search
        for p in (n for n in pool if n.unit is Unit.PERCENT and abs(100 - n.value - target.value) <= slack):
            return f"{target.text} = 100% − {p.text}"
    fits: list[tuple[Decimal, str]] = []

    for a, b in permutations(pool[:MAX_DERIVATION_POOL], 2):
        try:
            for value, working in _candidates(target.unit, a, b):
                error = abs(value - target.value)
                if error <= slack:
                    fits.append((error, working))
        except (DivisionByZero, InvalidOperation):  # pragma: no cover
            continue

    if not fits:
        return None

    _, best = min(fits, key=lambda f: f[0])
    others = f" [+{len(fits) - 1} other fits]" if len(fits) > 1 else ""
    return f"{target.text or target.value} = {best}{others}"


def _restated_as_percent(number: Number, claimed: list[Number]) -> bool:
    """Whether a unitless ratio is the same claim as a percentage the document states (`0.20` beside
    `20%`, a factor `1.4648` beside `+46.5%`); the percentage itself is still checked."""
    if number.unit is not Unit.COUNT or not (0 < number.value < 10):
        return False
    forms = [number.value * 100] if number.value < 1 else []
    forms += [abs(number.value - 1) * 100] if number.places >= 3 else []
    return any(
        p.unit is Unit.PERCENT and abs(p.value - f) <= tolerance_for(p) for p in claimed for f in forms
    )


def check_numbers(prose: str, source_prose: str) -> dict[str, object]:
    """The anti-fabrication check: every figure a reader acts on is verbatim in a source or derived
    from it in one dimensionally legal step."""
    claimed = numbers_in(strip_shown_working(prose))
    data = [n for n in claimed if n.datum and not _restated_as_percent(n, claimed)]
    words = [n.text for n in claimed if n.unit is Unit.WORD]
    dates = [n.text for n in claimed if n.unit is Unit.DATE]

    if not data:
        return _check("numbers:grounded", SUBSTANTIVE, SKIP, "the document states no figures")

    pool = numbers_in(source_prose)
    verbatim = {n.value for n in pool}

    grounded, derived, ungrounded = 0, list[str](), list[str]()
    for number in data:
        if number.value in verbatim:
            grounded += 1
        elif working := derive(number, pool):
            derived.append(working)
        else:
            ungrounded.append(number.text or str(number.value))

    detail = f"{grounded} verbatim, {len(derived)} derived, {len(ungrounded)} ungrounded"
    if derived:
        # **Named as evidence, not proof.** A derivation is a hypothesis that fits; the judge
        # reads the working and says whether the quantities mean anything. Printing it is what makes
        # that possible - and what made this suite's own coincidences visible in the first place.
        detail += " · derived (plausible; the judge confirms the working): " + "; ".join(derived[:8])

    # **Say what was NOT checked, every time.** A limit nobody is told about is a limit that becomes
    # a lie - and these two are precisely where a fabrication could still hide, so they are
    # handed to the judge by name rather than swept under a green tick.
    if words:
        detail += f" · not checked (bare counts): {', '.join(words[:8])}"
    if dates:
        detail += f" · not checked (dates - an inference, not a sum): {', '.join(dates[:8])}"
    if len(pool) > MAX_DERIVATION_POOL:
        detail += f" · derivation searched the first {MAX_DERIVATION_POOL} of {len(pool)} source numbers"

    if ungrounded:
        return _check(
            "numbers:grounded",
            SUBSTANTIVE,
            FAIL,
            detail,
            expected="every figure verbatim in a source, or derived from it in one step",
            actual="in no source: " + ", ".join(sorted(set(ungrounded))[:12]),
        )
    return _check("numbers:grounded", SUBSTANTIVE, PASS, detail)


# ── quotations ────────────────────────────────────────────────────────────────────────────────

# **Newlines are inside the quote, not outside it.** A quotation that wraps across a line is the
# single most ordinary thing a quotation does; a pattern that stops at a newline makes a fabricated
# multi-line quote invisible, a false negative in the one check whose whole job is catching invention.
#
# Allowing newlines risks an unbalanced `"` swallowing a paragraph, so a candidate spanning a blank
# line is dropped - real quotations do not cross paragraph breaks.
# **Length is a filter on which quotes to CHECK. It is not a rule about where quotes ARE.**
#
# One regex doing both jobs (`"([^"]{12,400})"`) cannot work: a quotation shorter than the minimum
# (`no "Q2 COGS" line`) does not match, so the scan skips its opening mark and pairs its *closing*
# mark with the *next* quotation's *opening* mark. Every quotation after it slips by one, and what
# the check extracts is the **glue between citations**:
#
#     ', atlas-q3.md).\nEnd-of-Q2 employees: 287 ('
#
# reported as a quotation in no source - accusing a clean document of the exact sin this suite
# exists to catch.
#
# So the marks are paired **positionally** - first with second, third with fourth - and the length
# rule is applied afterwards, to decide what is worth checking. A scanner that skips some pairs
# cannot correctly pair the rest.
QUOTE_MARKS = re.compile(r'"|“|”')
MIN_QUOTE_CHARS = 12
MAX_QUOTE_CHARS = 400
EDGE_PUNCTUATION = " .,;:!?\u2014-"


def quotations(prose: str) -> list[str]:
    """Every quoted span, paired in document order. Odd trailing mark ⇒ dropped, not mis-paired."""
    marks = [m.start() for m in QUOTE_MARKS.finditer(prose)]
    spans = [prose[a + 1 : b] for a, b in zip(marks[0::2], marks[1::2], strict=False)]
    return [s for s in spans if MIN_QUOTE_CHARS <= len(s) <= MAX_QUOTE_CHARS]


def check_quotes(prose: str, source_prose: str) -> dict[str, object]:
    """Every quotation appears in a source: edge punctuation stripped and case ignored, so a quote
    spliced into a sentence still matches."""
    haystack = normalise(source_prose).casefold()
    quotes = [q for q in quotations(prose) if len(q.split()) >= 4 and "\n\n" not in q]
    if not quotes:
        return _check("quotes:verbatim", SUBSTANTIVE, SKIP, "the document quotes nobody")

    missing: list[str] = []
    for quote in quotes:
        needle = normalise(quote).strip(EDGE_PUNCTUATION).casefold()
        if needle and needle not in haystack:
            missing.append(quote[:80])

    if missing:
        return _check(
            "quotes:verbatim",
            SUBSTANTIVE,
            FAIL,
            f"{len(quotes) - len(missing)} of {len(quotes)} found in the sources",
            expected="every quotation appears verbatim in a source",
            actual="not in any source: " + " | ".join(missing[:3]),
        )
    return _check(
        "quotes:verbatim", SUBSTANTIVE, PASS, f"all {len(quotes)} quotation(s) appear in the sources"
    )


# ── links ─────────────────────────────────────────────────────────────────────────────────────

# One level of balanced parentheses stays in a target, as CommonMark keeps it: `wiki/Go_(lang)`.
LINK = re.compile(r"\[[^\]]*\]\(\s*<?((?:[^()\s<>]|\([^()\s<>]*\))+)")
READ_CLAMP = 160  # the journal's label clamp (`labels.LABEL_MAX`): a page spelled this long was cut


def check_links(
    markdown: str, sources: list[str], allow_domains: list[str], read_pages: list[str] | None = None
) -> dict[str, object]:
    """Every link resolves to a declared source, a page the task read (host and path), or an egress
    allowlist host; parsed with `urlsplit`, never by hand."""
    targets: list[str] = [t for t in LINK.findall(markdown) if not t.startswith("#")]
    if not targets:
        return _check("links:resolve", SUBSTANTIVE, SKIP, "the document links to nothing")

    read = {p.rstrip("/") for p in read_pages or []}
    cut = [p for p in read_pages or [] if len(p) >= READ_CLAMP]
    unreachable: list[str] = []
    for target in targets:
        parts = urlsplit(target)
        if parts.scheme:
            host = (parts.hostname or "").lower()
            page = f"{host}{parts.path}".rstrip("/")
            # Anchored on a dot, exactly as the egress proxy anchors it: `example.com` must never
            # admit `evil-example.com`. Two definitions of one rule is how an allowlist rots.
            allowed = any(host == d or host.endswith(f".{d}") for d in allow_domains)
            if not (allowed or page in read or any(page.startswith(p) for p in cut)):
                unreachable.append(target)
        elif target not in sources:
            unreachable.append(target)

    if unreachable:
        return _check(
            "links:resolve",
            SUBSTANTIVE,
            FAIL,
            f"{len(targets) - len(unreachable)} of {len(targets)} resolve",
            expected="every link is a declared source, a page this task read, or an allowed domain",
            actual="never read: " + ", ".join(unreachable[:5]),
        )
    return _check("links:resolve", SUBSTANTIVE, PASS, f"all {len(targets)} link(s) resolve")


# ── the frame around the checks ───────────────────────────────────────────────────────────────


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


def _unextracted(what: str, bad: Exception) -> dict[str, object]:
    return _check("format:extraction", STRUCTURAL, FAIL, what, actual=f"{type(bad).__name__}: {bad}")


def _extracted(kind: str, text: str) -> dict[str, object]:
    return _check(
        "format:extraction",
        STRUCTURAL,
        PASS,
        f"{kind} text extracted ({len(text):,} chars) - prose checks run on it",
    )


# The one reader rides beside this file, loaded by exact path: the device runs suites with `-I`. Registered
# before it runs because its dataclasses look their own module up.
_SPEC = importlib.util.spec_from_file_location("extract", Path(__file__).with_name("extract.py"))
assert _SPEC is not None and _SPEC.loader is not None
_extract = sys.modules["extract"] = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_extract)
extract, write_extract = _extract.extract, _extract.write_extract


def run(job: dict[str, object]) -> list[dict[str, object]]:
    artifact_path = str(job["artifact_path"])
    expected_sha = str(job["artifact_sha256"])
    sources = [str(s) for s in cast("list[object]", job.get("sources", []))]
    allow_domains = [str(d) for d in cast("list[object]", job.get("allow_domains", []))]
    read_pages = [str(p) for p in cast("list[object]", job.get("read_pages", []))]
    checks: list[dict[str, object]] = []

    # 1. Are we even looking at the right bytes? Everything downstream is worthless if not.
    raw = Path(artifact_path).read_bytes()
    actual_sha = hashlib.sha256(raw).hexdigest()
    if actual_sha == expected_sha:
        checks.append(_check("bytes:sha256", STRUCTURAL, PASS, f"{actual_sha[:16]}… matches the manifest"))
    else:
        checks.append(
            _check(
                "bytes:sha256",
                STRUCTURAL,
                FAIL,
                "these are not the bytes that were published",
                expected=expected_sha,
                actual=actual_sha,
            )
        )

    # 2. The sources have to *be there*. A missing source is not a passed check - it is a check
    #    that could not run, and the grounding checks below would then vacuously pass.
    present, missing = list[str](), list[str]()
    for path in sources:
        file = Path(path)
        (present if file.is_file() and file.stat().st_size else missing).append(path)

    if not sources:
        checks.append(
            _check(
                "sources:present",
                STRUCTURAL,
                SKIP,
                "the document declares no sources - nothing to check it against",
            )
        )
    elif missing:
        checks.append(
            _check(
                "sources:present",
                STRUCTURAL,
                FAIL,
                f"{len(present)} of {len(sources)} present",
                expected="every declared source readable",
                actual="missing: " + ", ".join(missing),
            )
        )
    else:
        checks.append(_check("sources:present", STRUCTURAL, PASS, f"{len(present)} source(s), all readable"))

    # 3. A `.docx`, `.pdf` or `.html` is not UTF-8 prose (decoding one "verified" mojibake): the shared reader
    #    takes its words in document order, and the SAME prose pipeline runs over them.
    name = artifact_path.rsplit("/", 1)[-1].lower()
    kind = name.rsplit(".", 1)[-1]
    if kind in ("pdf", "docx", "html"):
        try:
            markdown = extract(artifact_path).markdown
        except ImportError as absent:
            # A missing parser is the SUITE's gap (a stale frame image), never the document's: SKIP
            # says nobody read it - an honest UNVERIFIED, where a FAIL would blame the artifact.
            reason = f"the .{kind} reader is not in this frame image - its text cannot be examined"
            checks.append(
                _check(
                    "suite:extractor", STRUCTURAL, SKIP, reason, actual=f"{type(absent).__name__}: {absent}"
                )
            )
            return checks
        except Exception as bad:  # a broken file is the author's failure, not the suite's
            checks.append(_unextracted(f"the .{kind} could not be read", bad))
            return checks
        if not markdown.strip():
            # Zero readable text is not "nothing to check": a reader-facing document must carry words,
            # and a SKIP here would let a rasterized fabrication ship unexamined.
            checks.append(_check("format:extraction", STRUCTURAL, FAIL, f"the .{kind} has no readable text"))
            return checks
        checks.append(_extracted(kind, markdown))
    else:
        markdown = raw.decode("utf-8", errors="replace")
    prose = to_prose(markdown)
    source_prose = to_prose("\n\n".join(Path(p).read_text(errors="replace") for p in present))

    numbers = bool(job.get("numbers", True))  # else the worker grounds claims
    if present:
        checks += [check_numbers(prose, source_prose)] if numbers else []
        checks.append(check_quotes(prose, source_prose))
    else:
        # Say it out loud: a check skipped silently is how a suite reports `pass` while proving nothing.
        reason = "no readable sources - nothing to ground against"
        checks += [_check("numbers:grounded", SUBSTANTIVE, SKIP, reason)] if numbers else []
        checks.append(_check("quotes:verbatim", SUBSTANTIVE, SKIP, reason))

    # Links are checked on what CARRIES them: the raw markup for .html (extraction drops
    # attributes, so `href`s only exist there), the extracted/decoded text otherwise.
    link_text = raw.decode("utf-8", errors="replace") if name.endswith(".html") else markdown
    checks.append(check_links(link_text, sources, allow_domains, read_pages))
    write_extract(job, markdown)
    return checks


def main() -> None:
    job = json.loads(sys.stdin.read())
    try:
        checks = run(job)
    except Exception as exc:
        checks = [_check("suite:ran", STRUCTURAL, FAIL, f"{type(exc).__name__}: {exc}")]
    json.dump({"checks": checks}, sys.stdout)


if __name__ == "__main__":
    main()
