# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The QA gate's deterministic half (ADR-026) - the outcome law, the gotcha, the prompt.

Pure: no DB, no frame, no model. The composer (`operations/qa.py`) runs the in-frame pre-check
script and the model read; THIS module decides what they mean, so "would this outcome pass?"
is a unit test, not a rerun.

The outcome law, mirror of `final_verdict`'s shape with the lane's own teeth:

* a deterministic check FAIL is a **revise floor** - the agent cannot talk past a missing
  section or a mid-sentence break;
* the judge's FAIL stands on clean checks - the editorial read exists to catch what
  arithmetic cannot;
* both clean ⇒ PASS;
* the model read unreachable with clean checks ⇒ **PASS_UNREAD** - ship with the note showing.
  With the lane's receipts removed, a persistently unread gate is a product
  regression, not a footnote: the composer logs it loudly every time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from prismoid.schemas.qa import (
    MAX_FINDINGS,
    QACheck,
    QAFinding,
    QAFindingKind,
    QAOutcomeKind,
    QASeverity,
)
from prismoid.schemas.verification import JudgeVerdict, Verdict

QA_CHECK_TIMEOUT_S = 330.0  # the in-frame pre-check's wall clock - ABOVE one LibreOffice conversion
# (render.SOFFICE_TIMEOUT = 240 s): the precheck renders the deliverable to pages, and a
# budget below the converter's would turn every long document into an INFRA no-read.
QA_READ_TIMEOUT_S = 300.0  # a 110-page read is minutes, not the judge's 90 s
MAX_QA_ATTEMPTS = 2  # one build + one corrective rebuild; past it, notes ship visibly

# The extracted text fed to the read is clipped only past this belt - ~150k tokens, inside the
# judge's window with room for the outline and the rubric. Clipping is ANNOUNCED in the
# prompt when it happens; a silently truncated read is the judge's old sin, not repeated here.
READ_BELT_CHARS = 600_000


@dataclass(frozen=True, slots=True)
class QAOutcome:
    kind: QAOutcomeKind
    findings: list[QAFinding] = field(default_factory=list[QAFinding])
    note: str = ""  # the visible sentence when the outcome ships with caveats


def outcome(checks: list[QACheck], report: JudgeVerdict | None) -> QAOutcome:
    """The law. Checks first (the floor), then the judge's read, then the honest degradations."""
    failed = [c for c in checks if not c.ok]
    check_findings = [
        QAFinding(
            location=c.name.removeprefix("qa:"),
            severity=QASeverity.BLOCKER,
            kind=_kind_of(c.name),
            instruction=(c.detail or f"{c.name} failed")[:300],
        )
        for c in failed
    ]

    if failed:
        merged = check_findings + (list(report.findings) if report else [])
        return QAOutcome(QAOutcomeKind.REVISE, merged[:MAX_FINDINGS])

    if report is None:
        # Two honestly different unread shapes: checks that RAN and were clean, vs a
        # precheck that never ran at all (exit-3 INFRA, a composer crash) - this note must not
        # claim a cleanliness nobody measured.
        note = (
            "The final review could not read this file; its automatic checks passed."
            if checks
            else "The final review could not check this file, so it is delivered unchecked."
        )
        return QAOutcome(QAOutcomeKind.PASS_UNREAD, [], note=note)

    if report.verdict is Verdict.FAIL and report.findings:
        return QAOutcome(QAOutcomeKind.REVISE, list(report.findings[:MAX_FINDINGS]))
    if report.verdict is Verdict.FAIL:
        # A FAIL with ZERO actionable findings cannot drive a rebuild - the gotcha would be
        # empty. Ship PASS, with the reason visible in the note: the composer surfaces `note`
        # into the narration and the qa.reviewed frame, as loud as PASS_UNREAD.
        return QAOutcome(
            QAOutcomeKind.PASS,
            [],
            note="The final review had doubts but named nothing to fix.",
        )
    return QAOutcome(QAOutcomeKind.PASS, [])


def _kind_of(check_name: str) -> QAFindingKind:
    if check_name.startswith("qa:layout:"):
        return QAFindingKind.LAYOUT  # the rendered-page family: one kind, many sub-checks
    return {
        "qa:sections": QAFindingKind.MISSING_CONTENT,
        "qa:size": QAFindingKind.MISSING_CONTENT,
        "qa:truncation": QAFindingKind.TRUNCATION,
        "qa:figures": QAFindingKind.FIGURE,
        "qa:headings": QAFindingKind.STRUCTURE,
        "qa:links": QAFindingKind.FORMATTING,
        "qa:terms": QAFindingKind.CONTINUITY,
    }.get(check_name, QAFindingKind.ACCEPTANCE)


def as_gotcha(outcome_: QAOutcome, attempt: int) -> str:
    """The findings, **in the words a retry can use**: location →
    instruction, one line each - the rebuild's whole brief."""
    lines = [f"- **QA (attempt {attempt}) requires revision.** Fix exactly what is named:"]
    for finding in outcome_.findings:
        lines.append(f"  - [{finding.severity.value}] {finding.location}: {finding.instruction}")
    if len(outcome_.findings) == MAX_FINDINGS:
        lines.append("  - (further findings clamped - fix these first)")
    return "\n".join(lines)


# What a person reads for each kind of open finding: the findings themselves are the rebuild's brief,
# in an agent's words (paths, tools, check ids), so they stay on the step record.
_OPEN: Final[dict[QAFindingKind, str]] = {
    QAFindingKind.MISSING_CONTENT: "missing content",
    QAFindingKind.TRUNCATION: "text cut off",
    QAFindingKind.CONTINUITY: "consistency",
    QAFindingKind.STRUCTURE: "structure",
    QAFindingKind.FORMATTING: "formatting",
    QAFindingKind.FIGURE: "figures",
    QAFindingKind.ACCEPTANCE: "the brief",
    QAFindingKind.DUPLICATION: "repetition",
    QAFindingKind.DISCONTINUITY: "flow",
    QAFindingKind.SALIENCE: "focus",
    QAFindingKind.INCONSISTENCY: "contradictions",
    QAFindingKind.LAYOUT: "layout",
}


def notes_sentence(outcome_: QAOutcome) -> str:
    """The visible accept-with-notes line: how many points stayed open and on what, in product words,
    never "unverified" (that word is the receipt lane's)."""
    n = len(outcome_.findings)
    kinds = ", ".join(dict.fromkeys(_OPEN[f.kind] for f in outcome_.findings))
    said = (
        f" {n} point{'' if n == 1 else 's'} stayed open ({kinds}); they are on the step record." if n else ""
    )
    return f"\u26a0\ufe0f **Delivered with QA notes.**{said}"


def read_prompt(*, extracted: str, outline_md: str, acceptance: list[str], checks: list[QACheck]) -> str:
    """The judge's whole editorial input, assembled deterministically; truncation past the belt is
    announced, and rendered pages ride after it."""
    body = extracted
    clipped = ""
    if len(body) > READ_BELT_CHARS:
        body = body[:READ_BELT_CHARS]
        clipped = (
            "\n\n[NOTE: the document exceeds the read belt - the tail beyond this point was "
            "NOT read; judge what you saw and say so in the rationale.]"
        )
    check_lines = (
        "\n".join(f"- {c.name}: {'ok' if c.ok else 'FAILED - ' + c.detail}" for c in checks) or "- none ran"
    )
    acceptance_lines = "\n".join(f"- {line}" for line in acceptance) or "- (none declared)"
    return (
        "## What was promised - the outline and its continuity bible\n\n"
        f"{outline_md}\n\n"
        "## What 'done' means - the acceptance criteria, verbatim\n\n"
        f"{acceptance_lines}\n\n"
        "## What the deterministic checks already found (facts - do not re-litigate a pass)\n\n"
        f"{check_lines}\n\n"
        "## The document, extracted in full\n\n"
        f"{body}{clipped}"
    )
