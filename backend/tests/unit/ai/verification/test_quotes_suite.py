# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The quote-match suite - stdlib-only, so tested in-process.

The suite polices the one lie a synthesis tells worst: a **manufactured consensus** - a quote the
chair attributes to answers that none of them made. So the tests pin both directions: a report whose
quotes are verbatim from the sources passes, and one with a fabricated quote fails, naming it. A
report with no quotes is a SKIP (the chair may paraphrase), never a vacuous pass.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from prismoid.ai.verification.suites import quotes


def _job(artifact: Path, sources: list[Path]) -> dict[str, object]:
    return {
        "artifact_path": str(artifact),
        "artifact_sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
        "sources": [str(s) for s in sources],
    }


def _status(checks: list[dict[str, object]]) -> dict[str, str]:
    return {str(c["name"]): str(c["status"]) for c in checks}


def _sources(tmp: Path) -> list[Path]:
    a = tmp / "source1.md"
    a.write_text("I recommend against it. Margin is thin and a price rise loses volume.\n")
    b = tmp / "source2.md"
    b.write_text("Raise prices. Demand is inelastic in this segment and margin is thin.\n")
    return [a, b]


def test_a_faithful_quote_traces(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    report = tmp_path / "report.md"
    report.write_text('Both answers note that "margin is thin," though they draw opposite conclusions.\n')
    status = _status(quotes.run(_job(report, sources)))
    assert status["bytes:sha256"] == "pass"
    assert status["quotes:trace"] == "pass"


def test_a_fabricated_consensus_fails(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    report = tmp_path / "report.md"
    # No answer said this - the chair invented the agreement.
    report.write_text('The models agree that "we should expand to Europe next quarter."\n')
    status = _status(quotes.run(_job(report, sources)))
    assert status["quotes:trace"] == "fail", "a quote in no source is a manufactured consensus"


def test_no_quotes_is_a_skip(tmp_path: Path) -> None:
    sources = _sources(tmp_path)
    report = tmp_path / "report.md"
    report.write_text("On balance the answers lean against a price rise; the tension is volume vs margin.\n")
    status = _status(quotes.run(_job(report, sources)))
    assert status["quotes:trace"] == "skip", (
        "a paraphrased synthesis is the judge's to weigh, not this suite's"
    )


def test_no_sources_skips_the_trace() -> None:
    # An answer to a question with no brief has nothing to trace to → SKIP (unverified, the
    # judge weighs it), never a FAIL - a false FAIL here would sink every brief-less fleet task.
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        report = Path(d) / "report.md"
        report.write_text('I recommend against it - "margin is thin" as they say.\n')
        status = _status(quotes.run(_job(report, [])))
    assert status["quotes:trace"] == "skip"
    assert "bytes:sha256" in status  # only the plumbing check ran; the verdict is unverified


# ── fleet coverage ──────────────────────────────────────────────────────────────────────────────


def _answers(tmp: Path) -> list[Path]:
    """Two fleet answers, named the way the executor mounts them (`answer-<model>.md`)."""
    a = tmp / "answer-anthropic-claude-opus-4-8.md"
    a.write_text("Yes - expose it, scoped to coarse states.\n")
    b = tmp / "answer-anthropic-claude-haiku-4-5.md"
    b.write_text("Yes, with data masking before launch.\n")
    return [a, b]


def _table(rows: int) -> str:
    body = "\n".join(f"| model{i} | yes | fast | leak |" for i in range(rows))
    return f"# Comparison\n\n| Model | Position | Strongest | Caveat |\n|---|---|---|---|\n{body}\n"


def test_a_paraphrased_table_covering_every_seat_passes(tmp_path: Path) -> None:
    # The debt this closes: a clean table with no quotes used to skip quotes:trace and land
    # `unverified`. Coverage is the substantive check an honest, paraphrased table passes on.
    sources = _answers(tmp_path)
    report = tmp_path / "table.md"
    report.write_text(_table(2))
    status = _status(quotes.run(_job(report, sources)))
    assert status["answers:covered"] == "pass"
    assert status["quotes:trace"] == "skip"  # a paraphrased table quotes nothing - still fine


def test_a_dropped_seat_fails_coverage(tmp_path: Path) -> None:
    # Two answers given, one row in the table - a lie of omission the reader cannot see.
    sources = _answers(tmp_path)
    report = tmp_path / "table.md"
    report.write_text(_table(1))
    status = _status(quotes.run(_job(report, sources)))
    assert status["answers:covered"] == "fail"


def test_coverage_does_not_fire_for_a_plain_document(tmp_path: Path) -> None:
    # The sources are a brief, not fleet answers - coverage must not apply to a normal document.
    sources = _sources(tmp_path)
    report = tmp_path / "memo.md"
    report.write_text("A memo with a table:\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
    status = _status(quotes.run(_job(report, sources)))
    assert "answers:covered" not in status
