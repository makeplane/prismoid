# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The document suite's logic - the half of verification that needs no model and no sandbox.

Imported directly, which is the one legitimate exception to `suites/__init__.py`'s "do not import
these": the module is stdlib-only *precisely* so it can run in a frame, and that same property
makes it unit-testable in microseconds. Application code still goes through `pipeline.run_suite()`.

**Two failure modes, and they are not symmetric.** A check that misses a fabricated number makes
the product a liar. A check that flags a legitimate one makes the product useless - and a verifier
that fails correct work is a verifier somebody switches off, which lands you back at the first
failure mode by a longer road. Both directions are tested here, and the second one harder.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from prismoid.ai.verification import pipeline
from prismoid.ai.verification.suites.document import (
    Number,
    Unit,
    check_links,
    check_numbers,
    check_quotes,
    derive,
    numbers_in,
    to_prose,
)

SOURCE = """
# Atlas Robotics - Q3 FY2026

Q3 revenue was **$41.6M**, against $33.9M in Q2 and $28.4M in Q3 last year.
Services accounted for $9.2M; hardware for the remaining $32.4M.
Cost of goods sold was $22.9M. Operating expenses were $14.2M.

We ended the quarter with 312 employees, up from 287 at the end of Q2. Attrition was 4.1%.

The VP of Operations, Dana Whitfield, said:

> "We are shipping more units with fewer escalations than at any point since the
> Series B. The constraint is no longer the line - it is the field engineers."

Two customers represent 31% of revenue.
"""


# ── the prose, not the markup ─────────────────────────────────────────────────────────────────


def values(prose: str) -> list[Decimal]:
    return [n.value for n in numbers_in(prose)]


def data(prose: str) -> list[Decimal]:
    """Only the figures a reader would act on - not the document's counts of its own parts."""
    return [n.value for n in numbers_in(prose) if n.datum]


def test_structure_is_not_data() -> None:
    """A `1.` list marker is not the number one. Extract numbers from raw markdown and you fail the
    artifact for containing its own formatting - which is how a verifier gets switched off."""
    markdown = """
# Findings
1. Revenue grew.
2. Costs fell.
- See [the report](https://example.com/2024/q3/report-7.pdf)
```python
x = 999
```
"""
    assert values(to_prose(markdown)) == []


def test_a_number_in_prose_survives() -> None:
    assert Decimal("41600000") in data(to_prose("Revenue was **$41.6M** this quarter."))
    assert Decimal("312") in data(to_prose("We ended with 312 employees."))
    assert Decimal("4.1") in data(to_prose("Attrition was 4.1%."))


def test_a_magnitude_is_part_of_the_number() -> None:
    """**`$41.6M` is 41,600,000 - not 41.6.** The `M` is not decoration.

    This is what lets `$3.1M`, `$620K` and `$41,600,000` be compared with one another at all, and
    it is the difference between catching a fabricated figure and failing a correct one. A live run
    failed a memo that said "a 20% increase on the $3.1M lease signals a $620K annual increase" -
    which is exactly right, and unprovable to a verifier that has thrown the `M` and the `K` away.
    """
    assert data(to_prose("Revenue was $41.6M.")) == [Decimal(41_600_000)]
    assert data(to_prose("The increase is $620K.")) == [Decimal(620_000)]
    assert data(to_prose("Revenue was $41,600,000.")) == [Decimal(41_600_000)]  # …and they agree
    assert data(to_prose("A $2.4 billion market.")) == [Decimal(2_400_000_000)]

    # A percentage is never scaled. `20%` is twenty, not twenty million, whatever follows it.
    assert data(to_prose("Margin was 20% in millions of dollars of revenue.")) == [Decimal(20)]


def test_the_scale_case_that_started_all_this() -> None:
    """`$3.1M × 20% = $620K`. Correct arithmetic across two magnitudes."""
    pool = numbers_in(to_prose("The lease costs $3.1M a year and the landlord wants 20% more."))
    working = derive(numbers_in(to_prose("That is a $620K increase."))[0], pool)
    assert working is not None
    assert "3.1M" in working and "20%" in working  # …and it reads in the author's own notation


def test_thousands_separators_and_currency_are_notation_not_value() -> None:
    assert Decimal("1234567") in data(to_prose("Costs were $1,234,567 last year."))


def test_a_bare_count_is_a_word_not_a_figure() -> None:
    """**The one carve-out in the suite, and it is stated in every report that uses it.**

    "3 key risks" and "three key risks" are the same sentence. Failing the first would teach the
    model to spell numbers out - not to stop inventing them. That is a check optimising for the
    wrong thing, and the wrong thing is exactly what it would get.

    A figure carries a unit ($, %, M) or a magnitude. A count does not.
    """
    assert data(to_prose("There are 3 key risks and 2 open questions.")) == []
    assert values(to_prose("There are 3 key risks and 2 open questions.")) == [Decimal(3), Decimal(2)]

    # …but the moment it carries a unit or a magnitude, it is a claim about the world.
    assert data(to_prose("Margin fell 3%.")) == [Decimal(3)]
    assert data(to_prose("We lost $3M.")) == [Decimal(3_000_000)]
    assert data(to_prose("We shipped 3.0 units.")) == [Decimal("3.0")]
    assert data(to_prose("We hired 40 engineers.")) == [Decimal(40)]


# ── numbers: the anti-fabrication check ───────────────────────────────────────────────────────


def test_a_verbatim_figure_is_grounded() -> None:
    result = check_numbers(to_prose("Revenue was $41.6M."), to_prose(SOURCE))
    assert result["status"] == "pass"


def test_a_fabricated_figure_is_caught() -> None:
    """**The reason this milestone exists.** The figure is plausible, well-formed, and in nothing."""
    result = check_numbers(to_prose("Revenue was $99.7M this quarter."), to_prose(SOURCE))
    assert result["status"] == "fail"
    assert "99.7" in str(result["actual"])


def test_arithmetic_the_writer_did_is_not_a_fabrication() -> None:
    """A memo that computes a growth rate is doing its job. Flagging it would make the check
    useless, and a useless check gets turned off - which is how you end up with no check at all."""
    memo = "Revenue grew 46.5% year on year, and headcount rose by 25."
    result = check_numbers(to_prose(memo), to_prose(SOURCE))
    assert result["status"] == "pass", result
    assert "46.5" in str(result["detail"])  # the working is shown, so a human can reject it


# `places` is not optional in these fixtures: it is what `tolerance_for` reads to decide how much
# rounding the author admitted to. A helper that forgets it makes `46.5%` claim the slack of `46%`,
# and every tolerance assertion below would then be testing the wrong number.
def pct(x: str) -> Number:
    return Number(Decimal(x), Unit.PERCENT, f"{x}%", places=len(x.partition(".")[2]))


def usd(x: str) -> Number:
    return Number(Decimal(x), Unit.MONEY, f"${x}", places=len(x.partition(".")[2]))


def test_the_derivation_is_shown_not_just_asserted() -> None:
    """A coincidence among 160k candidate pairs is a real risk. The defence is not to hide the
    search - it is to print what it found, so a human (and the judge) can call it nonsense."""
    working = derive(pct("46.5"), numbers_in(to_prose(SOURCE)))
    assert working is not None
    assert "41.6M" in working and "28.4M" in working


def test_rounding_is_not_lying() -> None:
    """41.6 / 28.4 − 1 = 46.478…%. A memo that writes 46.5% has rounded. One that writes 52% has
    not - it is claiming something the sources do not support."""
    pool = numbers_in(to_prose(SOURCE))
    assert derive(pct("46.5"), pool) is not None
    assert derive(pct("46.48"), pool) is not None
    assert derive(pct("52.0"), pool) is None


def test_a_margin_is_derivable_because_the_denominator_may_be_the_first_term() -> None:
    """Gross margin is (revenue − cost) / **revenue** - the single most common ratio in any
    financial document. Leaving `(a − b) / a` out of the formula set cost a false FAIL on it."""
    working = derive(pct("45"), numbers_in(to_prose(SOURCE)))  # (41.6 − 22.9) / 41.6 × 100 = 44.95
    assert working is not None and "41.6" in working and "22.9" in working


def test_a_growth_rate_over_counts_is_derivable() -> None:
    """(312 − 287) / 287 × 100 = 8.71%. Two counts make a percentage; that is dimensionally sound."""
    assert derive(pct("8.7"), numbers_in(to_prose(SOURCE))) is not None


# ── dimensional analysis: the thing that makes this a check rather than a coincidence machine ──


def test_you_cannot_add_a_percentage_to_a_dollar() -> None:
    """**The bug that a live run found, and the reason this file has units at all.**

    The verifier passed a real memo containing a fabricated figure - "implied 46% in Q2", which needs
    a Q2 cost the source never gives - because the unconstrained search found `(9.2 / 20) × 100`:
    dollars divided by a percentage. It also "derived" a 45% margin as `14.2 + 31` - dollars plus a
    percentage - and an 8.7% growth rate as `22.9 − 14.2`, two dollar amounts making a percentage.

    Every one of those is nonsense a physicist would catch in a second, and every one of them was
    used to launder a number that came from nowhere. **A search that always succeeds is not a check.**
    """
    pool = [usd("14.2"), pct("31"), usd("9.2"), usd("22.9"), pct("20")]

    assert derive(pct("45"), pool) is None  # was: 14.2 + 31  → $ + %
    assert derive(pct("46"), pool) is None  # was: (9.2 / 20) × 100  → $ / %
    assert derive(pct("8.7"), pool) is None  # was: 22.9 − 14.2  → $ − $ is not a %


def test_money_still_adds_to_money() -> None:
    """Killing the nonsense must not kill the arithmetic. $9.2M + $32.4M = $41.6M is a real sum."""
    pool = [usd("9.2"), usd("32.4")]
    working = derive(usd("41.6"), pool)
    assert working is not None and "+" in working


def test_a_coincidence_is_not_a_derivation() -> None:
    """`9.2M − 4.1% = 5.1`, and 5.1 rounds to 5. **Dimensional analysis is what kills it now** - you
    cannot subtract a percentage from a sum of money - so the tolerance no longer has to.

    That matters, because the tolerance guard was doing collateral damage: it also failed a memo
    that wrote `23%` for a true 22.71%, which is a *correct* rounding. A guard that fails correct
    work has stopped being a guard.
    """
    pool = numbers_in(to_prose(SOURCE))
    assert Decimal(9_200_000) in {n.value for n in pool}  # the tempting pair really is in the source
    assert derive(Number(Decimal(5), Unit.COUNT, "5"), pool) is None


def test_a_correct_rounding_passes() -> None:
    """22.71% written as `23%` is a correct rounding. `46.48%` written as `46.5%` is too.

    An earlier tolerance rule capped the slack at 0.5% of the value and failed `23%` for it - a
    guard that fails correct work has stopped being a guard.
    """
    pool = numbers_in(to_prose(SOURCE))  # 41.6M vs 33.9M → 22.71% · 41.6M vs 28.4M → 46.48%
    assert derive(pct("23"), pool) is not None
    assert derive(pct("46"), pool) is not None
    assert derive(pct("46.5"), pool) is not None


def test_the_best_fit_is_reported_and_the_rivals_are_counted() -> None:
    """**The honest limit of the deterministic half, made visible instead of hidden.**

    A two-significant-figure percentage carries about six bits, and a search over a few hundred
    dimensionally-legal candidates lands inside a one-point window by luck. It did: a real memo's
    fabricated "47% year-on-year" was "derived" as `(5.8M − 3.1M) / 5.8M × 100` - R&D headcount
    minus a building lease, over R&D headcount. Legal arithmetic; meaningless quantity.

    More cleverness does not fix that. So the suite reports the **closest** fit rather than the
    first one it stumbles on, **counts the rivals** (nine fits is a coincidence wearing a proof's
    clothes), and hands the working to a judge that can read it and laugh.
    """
    pool = numbers_in(to_prose(SOURCE))
    working = derive(pct("23"), pool)
    assert working is not None
    # The closest fit for 23% really is the sequential growth a reader would expect.
    assert "41.6M − 33.9M" in working

    # And where a figure fits several ways, the report says so - out loud, in the check detail.
    crowded = derive(Number(Decimal(50), Unit.PERCENT, "50%"), pool)
    if crowded and "other fits" in crowded:
        assert crowded.count("[+") == 1


def test_an_ungrounded_figure_fails_even_with_no_sources_at_all() -> None:
    """A figure with nothing behind it is a figure with nothing behind it."""
    result = check_numbers(to_prose("Revenue was $41.6M."), "")
    assert result["status"] == "fail"


# ── quotes ────────────────────────────────────────────────────────────────────────────────────


def test_a_real_quotation_passes_even_when_spliced_into_a_sentence() -> None:
    """The two normalisations that decide whether this check survives contact with real writing:
    the leading capital is lowercased to splice it mid-sentence, and the sentence's own comma ends
    up *inside* the closing quote. Neither is the speaker's doing, and failing them fails correct
    work."""
    memo = 'Whitfield noted that "the constraint is no longer the line - it is the field engineers," which suggests a hiring problem.'
    assert check_quotes(to_prose(memo), to_prose(SOURCE))["status"] == "pass"


def test_a_fabricated_quotation_is_caught() -> None:
    memo = 'Whitfield said "we expect to double revenue again next quarter" in the review.'
    result = check_quotes(to_prose(memo), to_prose(SOURCE))
    assert result["status"] == "fail"
    assert "double revenue" in str(result["actual"])


def test_a_fabricated_quotation_that_wraps_across_a_line_is_also_caught() -> None:
    """**A false negative in the one check whose whole job is catching invention.**

    The pattern was `[^"\\n]` - it could not match a quotation that wrapped across a line, which is
    the single most ordinary thing a quotation does. So an invented multi-line quote was invisible:
    the check reported "the document quotes nobody" and *passed*. Found by feeding the suite a
    fabricated document, which is the only way this class of bug is ever found.
    """
    memo = 'CEO Marcus Chen told the board: "we have never seen a quarter like this one, and we\nexpect to double again by Christmas."'
    result = check_quotes(to_prose(memo), to_prose(SOURCE))
    assert result["status"] == "fail"
    assert "double again" in str(result["actual"])


def test_a_real_quotation_that_wraps_across_a_line_still_passes() -> None:
    """The source's own quotation wraps. Both directions, or the fix is half a fix."""
    memo = (
        'She said "we are shipping more units with fewer escalations than at any point\nsince the Series B".'
    )
    assert check_quotes(to_prose(memo), to_prose(SOURCE))["status"] == "pass"


# ── dates: a label, not a quantity ────────────────────────────────────────────────────────────


def test_a_year_is_not_a_figure() -> None:
    """**A live memo wrote "$28.4M in Q3 2025" from a brief headed "Q3 FY2026" that says "Q3 last
    year".** Correct, contextual, and utterly beyond a regex: grounding it means adjudicating an
    inference, not checking a sum. So the suite does not pretend to - it names the date it skipped
    and hands it to the judge.
    """
    assert data(to_prose("Revenue was $28.4M in Q3 2025.")) == [Decimal(28_400_000)]
    assert data(to_prose("The lease expires in July 2027.")) == []
    assert data(to_prose("Since 2019 we have doubled.")) == []


def test_a_number_that_merely_looks_like_a_year_is_still_a_figure() -> None:
    """Both halves are required - the range *and* the cue. Otherwise "increased by 2000 units"
    becomes a date and stops being checked, which is a hole shaped exactly like a fabrication."""
    assert data(to_prose("We shipped 2000 units.")) == [Decimal(2000)]
    assert data(to_prose("Costs were $2,025 per seat.")) == [Decimal(2025)]


def test_the_report_names_what_it_did_not_check() -> None:
    """A limit nobody is told about is a limit that becomes a lie."""
    result = check_numbers(
        to_prose("Revenue was $41.6M in Q3 2025, one of 3 record quarters."), to_prose(SOURCE)
    )
    assert "2025" in str(result["detail"])
    assert "3" in str(result["detail"])


def test_no_quotations_is_a_skip_not_a_pass() -> None:
    assert check_quotes(to_prose("Revenue rose."), to_prose(SOURCE))["status"] == "skip"


# ── links ─────────────────────────────────────────────────────────────────────────────────────


def test_a_link_to_a_domain_the_task_could_not_reach_is_a_fabrication() -> None:
    """The frame has no route out but the proxy, and the proxy only knows this task's allowlist. A
    link to a domain that was not allowed is a page the agent *could not have read*. Not a
    judgement - a network fact."""
    memo = "See [the study](https://journal-of-performance.example/vol3)."
    result = check_links(memo, sources=["/task/inputs/atlas-q3.md"], allow_domains=[])
    assert result["status"] == "fail"


def test_a_link_to_an_allowed_domain_resolves() -> None:
    memo = "See [the docs](https://docs.example.com/guide)."
    result = check_links(memo, sources=[], allow_domains=["example.com"])
    assert result["status"] == "pass"


def test_the_allowlist_is_anchored_on_a_dot() -> None:
    """`example.com` must never admit `evil-example.com` - the classic way an allowlist becomes
    decorative. Same rule as the egress proxy's, and it has to be the same rule."""
    memo = "See [this](https://evil-example.com/x)."
    assert check_links(memo, sources=[], allow_domains=["example.com"])["status"] == "fail"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com@evil.test/x",  # userinfo: the host is evil.test, not example.com
        "https://EVIL.TEST/x",  # case
        "https://evil.test:443/x",  # a port
        "https://example.com.evil.test/x",  # a suffix that merely *contains* the allowed domain
    ],
)
def test_a_hand_rolled_host_parse_gets_these_wrong(url: str) -> None:
    """Which is why it is `urlsplit`, and not a `split("/")` chain.

    The first of these is the classic: everything before the `@` is *userinfo*, not the host, so a
    naive parser reads `example.com` and waves it through - while the browser goes to `evil.test`.
    An allowlist that can be fooled by punctuation is not an allowlist.
    """
    memo = f"See [this]({url})."
    assert check_links(memo, sources=[], allow_domains=["example.com"])["status"] == "fail"


def test_a_link_to_a_declared_source_resolves() -> None:
    memo = "See [the brief](/task/inputs/atlas-q3.md)."
    result = check_links(memo, sources=["/task/inputs/atlas-q3.md"], allow_domains=[])
    assert result["status"] == "pass"


# ── the verdict ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("named", "expected"),
    [
        # A substantive check passed, and nothing failed.
        ([("numbers:grounded", "pass")], "pass"),
        ([("bytes:sha256", "pass"), ("numbers:grounded", "pass")], "pass"),
        # Anything failing fails the lot - structural checks included.
        ([("numbers:grounded", "pass"), ("quotes:verbatim", "fail")], "fail"),
        ([("bytes:sha256", "fail"), ("numbers:grounded", "pass")], "fail"),
        # Nothing ran at all.
        ([("numbers:grounded", "skip"), ("quotes:verbatim", "skip")], "unverified"),
        # **The one that nearly shipped.** `bytes:sha256` passes on every document that is the
        # document we meant to look at - including a fabricated one. A report of one structural
        # pass and four skips is a receipt, not a verdict.
        (
            [
                ("bytes:sha256", "pass"),
                ("sources:present", "skip"),
                ("numbers:grounded", "skip"),
                ("quotes:verbatim", "skip"),
                ("links:resolve", "skip"),
            ],
            "unverified",
        ),
        # And `sources:present` alone is no better: the sources existing says nothing about whether
        # the document used them.
        ([("bytes:sha256", "pass"), ("sources:present", "pass"), ("numbers:grounded", "skip")], "unverified"),
    ],
)
def test_a_suite_that_proved_nothing_says_unverified(named: list[tuple[str, str]], expected: str) -> None:
    """Rule 9: nothing unverified ships without an explicit mark. **This is the mark.**

    "We verified this memo (we successfully hashed it)" is not a verdict. Only a check that read the
    document's *claims* can support a claim about the document - so the verdict rests on
    **substantive** checks, and the plumbing around them cannot manufacture a green tick.
    """
    from prismoid.ai.verification.pipeline import verdict_of
    from prismoid.schemas.verification import Check, CheckStatus

    checks = [Check.model_validate({"name": n, "status": CheckStatus(s)}) for n, s in named]
    assert verdict_of(checks).value == expected


# ── binary documents are honest, never mojibake-verified ─────────────────────
#
# A `.docx` and a `.pdf` both get REAL extraction now - covered in a frame by
# tests/integration/verification/test_docx_suite.py and test_pdf_suite.py, because their parsers are frame-pinned
# (corrupt stream → extraction FAIL, textless → FAIL: both live there). What stays here is the
# missing-parser path: a missing library is the SUITE's gap, never the document's, so it must read as
# nobody-looked (UNVERIFIED), not as a FAIL blaming the artifact for the frame image. Either way the
# prose checks never run over bytes that are not prose.


def test_a_missing_pdf_extractor_is_the_suites_gap_never_the_documents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hashlib
    import sys

    from prismoid.ai.verification.pipeline import verdict_of
    from prismoid.ai.verification.suites.document import run
    from prismoid.schemas.verification import Check, Verdict

    monkeypatch.setitem(sys.modules, "pdfplumber", None)  # the frame-image gap: the parser will not import
    raw = b"%PDF-1.7 a perfectly plausible pdf this venv simply cannot read"
    file = tmp_path / "memo.pdf"
    file.write_bytes(raw)
    checks = run(
        {
            "artifact_path": str(file),
            "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            "sources": [],
            "allow_domains": [],
        }
    )

    names = {str(c["name"]): str(c["status"]) for c in checks}
    assert names["suite:extractor"] == "skip", "no parser = nobody read it - named, not blamed"
    assert "format:extraction" not in names, "an extraction verdict would claim somebody read it"
    assert "numbers:grounded" not in names, "prose checks must not run over PDF bytes"
    assert verdict_of([Check.model_validate(c) for c in checks]) is Verdict.UNVERIFIED


# ── .html documents: the prose pipeline runs on the page's WORDS, never its markup ──


def test_an_html_document_runs_the_prose_pipeline_on_extracted_text(tmp_path: Path) -> None:
    import hashlib

    from prismoid.ai.verification.suites.document import run

    source = tmp_path / "stats.md"
    source.write_text("Port traffic grew 12% in 2025.")
    raw = (
        b'<!doctype html><html lang="en"><head><title>Ports</title></head>'
        b"<body><h1>Ports</h1><p>Traffic grew 12% in 2025 "
        b'(see <a href="https://example.com/stats">the stats</a>).</p></body></html>'
    )
    file = tmp_path / "ports.html"
    file.write_bytes(raw)

    checks = run(
        {
            "artifact_path": str(file),
            "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            "sources": [str(source)],
            "allow_domains": [],
        }
    )
    names = {str(c["name"]): str(c["status"]) for c in checks}
    assert names["format:extraction"] == "pass"
    assert names["numbers:grounded"] == "pass", "12% grounds against the source, from the WORDS"
    assert "links:resolvable" in names or any(n.startswith("links") for n in names), (
        "the link check ran - hrefs live in the RAW markup"
    )


def test_a_markup_only_html_fails_extraction_honestly(tmp_path: Path) -> None:
    import hashlib

    from prismoid.ai.verification.suites.document import run

    raw = b"<!doctype html><html><head></head><body><div></div></body></html>"
    file = tmp_path / "empty.html"
    file.write_bytes(raw)
    checks = run(
        {
            "artifact_path": str(file),
            "artifact_sha256": hashlib.sha256(raw).hexdigest(),
            "sources": [],
            "allow_domains": [],
        }
    )
    names = {str(c["name"]): str(c["status"]) for c in checks}
    assert names["format:extraction"] == "fail"
    assert "numbers:grounded" not in names, "prose checks must not run over nothing"


@pytest.mark.parametrize(
    ("text", "kept"), [("a" * 50, "a" * 50), ("a" * 51, "a" * 50 + pipeline.EXTRACT_CUT)]
)
def test_an_extract_past_the_cap_says_it_was_cut(tmp_path: Path, text: str, kept: str) -> None:
    from prismoid.ai.verification.suites.document import write_extract

    out = tmp_path / "extracted.md"
    write_extract({"extract_path": str(out), "extract_chars": 50, "extract_cut": pipeline.EXTRACT_CUT}, text)
    assert out.read_text() == kept
