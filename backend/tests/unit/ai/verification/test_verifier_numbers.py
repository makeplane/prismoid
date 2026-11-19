# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`numbers:grounded`, in both directions.

**The control comes first, and it is the whole point of the file.** An early egress test was green for
two entire micros because the tool it was testing did not exist; the lesson written down then was
that a refusal is only evidence if the same attack can be shown to succeed where it is allowed. So
every fix in here is paired with the fabrication it must not hide - and one of them, the annualiser,
*was* hidden by the first version of this fix and was caught by exactly this test.
"""

from pathlib import Path

import pytest

from prismoid.ai.verification.suites.document import check_numbers, check_quotes, to_prose

SOURCE = to_prose(Path("tests/fixtures/atlas-q3.md").read_text())


def verdict(document: str) -> tuple[str, str]:
    check = check_numbers(to_prose(document), SOURCE)
    return str(check["status"]), str(check.get("actual") or check.get("detail") or "")


def quoting(document: str) -> tuple[str, str]:
    check = check_quotes(to_prose(document), SOURCE)
    return str(check["status"]), str(check.get("actual") or check.get("detail") or "")


# ── quotations: the pairing bug that accused a clean document of fabricating them ──────────────


def test_a_short_quotation_does_not_desync_the_ones_after_it() -> None:
    """**The worst false positive this suite has produced.**

    `QUOTED` was one regex doing two jobs: finding quotations *and* ignoring short ones. A quotation
    below the length floor did not match, so the scan skipped its opening mark and paired its closing
    mark with the *next* quotation's opening mark. Everything after slipped by one, and what the
    check extracted was the glue between citations - `', atlas-q3.md).\\nEnd-of-Q2 employees: 287 ('`
    - which it then reported as a quotation appearing in no source.

    It accused a clean, meticulously-cited document of **fabricating quotations**: the exact sin the
    suite exists to catch, and the worst possible thing to be wrong about. It failed a real task
    three times before anyone read the "quotation" it was complaining about.
    """
    document = (
        'The brief gives no "Q2 COGS" line, so Q2 margin is not computable.\n'
        'Q3 revenue: $41.6M ("Q3 revenue was $41.6M", atlas-q3.md).\n'
        'Cost of goods sold: $22.9M ("Cost of goods sold was $22.9M", atlas-q3.md).\n'
    )
    status, detail = quoting(document)
    assert status == "pass", detail


def test_a_fabricated_quotation_still_fails() -> None:
    status, actual = quoting('The CFO said "we expect to triple revenue by next quarter".')
    assert status == "fail"
    assert "triple revenue" in actual


# ── correct work must pass. A verifier that fails correct work is a verifier somebody switches off.


def test_a_document_that_shows_its_working_is_not_punished_for_it() -> None:
    """The `× 100` is a percent conversion, not a claim about the world. It appeared in the
    ungrounded list of nearly every golden failure."""
    status, detail = verdict(
        "Revenue grew 22.7% sequentially, from $33.9M in Q2 to $41.6M in Q3.\n"
        "Derivation: 22.7% = ($41.6M - $33.9M) / $33.9M x 100 = 0.2272 -> 22.7%."
    )
    assert status == "pass", detail


def test_a_bare_table_cell_wears_the_unit_from_its_column() -> None:
    """`| Revenue | 41.6 |` under `Q3 ($M)` is $41.6M - which IS in the source. Read the cell alone
    and it is the count 41.6, which is in no source and never was."""
    status, detail = verdict(
        "| Line item | Q3 ($M) |\n|---|---|\n| Revenue | 41.6 |\n| COGS | 22.9 |\n| Opex | 14.2 |"
    )
    assert status == "pass", detail
    assert "3 verbatim" in detail


@pytest.mark.parametrize(
    "shown",
    [
        pytest.param(
            "Sequential growth: (41.6M - 33.9M) / 33.9M x 100 = 22.7%.",
            id="working on the LEFT of the =",
        ),
        pytest.param(
            "Gross margin: (41.6M - 22.9M) / 41.6M x 100 = 45.0%, i.e. 0.4495.",
            id="an un-converted intermediate is apparatus, not a claim",
        ),
        pytest.param(
            "Services were 9.2M of 41.6M revenue: 9.2 / 41.6 x 100 = 22.1%.",
            id="bare operands inside the expression",
        ),
    ],
)
def test_the_apparatus_of_a_calculation_is_not_a_claim(shown: str) -> None:
    """These three are the five real golden failures, reduced.

    The claim is the *result*; the working is everything that produces it - and it sits on whichever
    side of the `=` the author felt like putting it. Stripping only the right-hand side (the first
    attempt) is exactly backwards for the form models actually write, and reported
    `in no source: 100, 0.227` while the one figure a reader would act on grounded perfectly.
    """
    status, detail = verdict(shown)
    assert status == "pass", detail


def test_a_ratio_restated_beside_its_percentage_is_the_same_claim() -> None:
    """`0.20` is not a claim when the document also says `20%` - it is that claim, in ratio form.

    Third time a bare ratio failed a correct document, and it escalated a live board memo whose every
    real figure was sound. Nothing is hidden by dropping it: the percentage form is still checked, and
    it is the form a reader acts on.
    """
    status, detail = verdict(
        "The Fremont lease renews at a 20% increase, so the 0.20 escalator applies to the $3.1M rent."
    )
    assert status == "pass", detail


def test_a_fabricated_ratio_with_no_percentage_beside_it_still_fails() -> None:
    """The control for the above. `0.47` alone is a figure from nowhere, and it stays one."""
    status, actual = verdict("Renewal probability is 0.47 on the $3.1M Fremont lease.")
    assert status == "fail"
    assert "0.47" in actual


@pytest.mark.parametrize("dots", ["...", "…"])
def test_a_percentage_written_with_an_ellipsis_is_still_a_percentage(dots: str) -> None:
    """The golden yoy-growth memo (2026-10-25) showed its division to three places and an ellipsis:
    `46.479...%` for a true 46.4789%. The ellipsis hid the `%`, the figure read as a bare count, and
    a correct growth rate failed as "in no source" beside the 46.5% that grounded."""
    status, actual = verdict(
        f"Growth rate: ($13.2M / $28.4M) x 100 = 0.46479{dots} x 100 = 46.479{dots}% ≈ 46.5%"
    )
    assert status == "pass", actual


def test_a_fabricated_percentage_with_an_ellipsis_still_fails() -> None:
    """The control: the ellipsis earns the unit back, never a pass. 47.9 is no growth rate the brief
    supports, whatever trails it."""
    status, actual = verdict("Year-on-year growth was 47.9...%.")
    assert status == "fail", f"BLIND: {actual}"
    assert "47.9" in actual


def test_gross_margin_grounds_from_revenue_and_cogs() -> None:
    """The headline question the product exists to answer, and the rounding the model actually
    writes. 44.9519% → `44.9%`; half-a-ulp rejected it by 0.002 of a point (see RELATIVE_FLOOR)."""
    status, detail = verdict("Q3 gross margin was 44.9%, on $41.6M revenue against $22.9M COGS.")
    assert status == "pass", detail


# ── and the fabrications must still fail. If any of these passes, the check above is a green tick
#    on a lie, and this suite is worse than not having one.


@pytest.mark.parametrize(
    ("attack", "smuggled"),
    [
        pytest.param(
            "| Line item | Q3 ($M) |\n|---|---|\n| Revenue | 41.6 |\n| Pipeline | 99.9 |",
            "99.9m",
            id="fabricated cell in a qualified table",
        ),
        pytest.param(
            "Projected FY2027 revenue = $41.6M x 4 = $166.4M.",
            "166.4M",
            id="the annualiser - hidden behind an '=' with no claim on the left",
        ),
        pytest.param(
            "Run-rate: $258K per employee = $41.6M / 312 x 2.",
            "258K",
            id="fabrication as the result of shown working",
        ),
        pytest.param(
            "Q3 revenue was $41.6M against a $19.3M backlog.",
            "19.3M",
            id="plain fabrication, no apparatus",
        ),
    ],
)
def test_a_fabrication_still_fails(attack: str, smuggled: str) -> None:
    """Every one of these was run against the *fixed* suite before the fix was believed.

    The annualiser is the one that matters. The first cut of `strip_shown_working` dropped everything
    right of an `=`, and this document - a wholly ordinary way to write a projection - reported *"the
    document states no figures"*. The fabricated $166.4M was swallowed as apparatus. That is the
    single most damaging thing this verifier could do, and the only reason it is not in the build is
    that the attack was written down before the fix was trusted.
    """
    status, actual = verdict(attack)
    assert status == "fail", f"BLIND: {attack!r} passed"
    assert smuggled in actual, f"failed, but did not name {smuggled}: {actual}"


def test_a_gap_in_percentage_points_is_a_percent_difference() -> None:
    source = "Atlas spent 34.1% of sales. Helios spent 46.0% of sales."
    check = check_numbers("Atlas spent 34.1%, Helios 46.0%: a gap of 11.9 percentage points.", source)
    assert check["status"] == "pass", check


def test_a_growth_factor_beside_its_percentage_is_that_percentage() -> None:
    """Found live: `in no source: 1.4648` failed a partner email the judge passed."""
    source = "Q3 revenue was $41.6M, and $28.4M in Q3 last year."
    good = check_numbers("Atlas year on year: $41.6M against $28.4M is 1.4648, so +46.5%.", source)
    assert good["status"] == "pass"
    assert check_numbers("Growth was 1.4648 on the year.", source)["status"] == "fail"
    assert check_numbers("Each order took 1.5 days, up 50%.", "Each order took 1 day.")["status"] == "fail"


def test_the_rest_of_a_stated_share_is_its_complement() -> None:
    """Found live: 69% beside 31% printed a cross-company working the judge rightly refused."""
    source = "Two customers represent 31% of revenue. Opex was $14.2M, and the lease $4.4M."
    check = check_numbers("All other customers make up the remaining 69%.", source)
    assert check["status"] == "pass" and "69% = 100% − 31%" in str(check["detail"])


def test_a_unicode_minus_is_an_operator_in_shown_working() -> None:
    source = "Q3 revenue was $41.6M, against $33.9M in Q2."
    assert check_numbers("Working: (41.6 − 33.9) / 33.9 = 22.7%.", source)["status"] == "pass"
