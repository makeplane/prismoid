# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The QA gate's law (ADR-026): the outcome table over the judge's verdict, the gotcha wording, the pre-checks.

Pure policy + the precheck's `run()` in-process over markdown fixtures (the md path needs no
frame parsers) - the gate's whole decision surface, provable with nothing stood up.
"""

from __future__ import annotations

import pytest

from prismoid.ai.verification import qa_outcome
from prismoid.ai.verification.qa_precheck import run as precheck_run
from prismoid.schemas.qa import (
    QACheck,
    QAFinding,
    QAFindingKind,
    QAOutcomeKind,
    QASeverity,
)
from prismoid.schemas.verification import JudgeVerdict, Verdict

FINDING = QAFinding(
    location="section 02",
    severity=QASeverity.MAJOR,
    kind=QAFindingKind.CONTINUITY,
    instruction="Rename 'the firm' to 'the Company' per the bible.",
)


def report(verdict: Verdict, findings: list[QAFinding] | None = None) -> JudgeVerdict:
    return JudgeVerdict(verdict=verdict, rationale="read to the end", findings=findings or [])


# ── the outcome law ───────────────────────────────────────────────────────────────────────────


def test_a_check_fail_is_a_revise_floor_the_agent_cannot_lift() -> None:
    checks = [QACheck(name="qa:truncation", ok=False, detail="ends mid-thought")]
    out = qa_outcome.outcome(checks, report(Verdict.PASS))
    assert out.kind is QAOutcomeKind.REVISE
    assert any(f.kind is QAFindingKind.TRUNCATION for f in out.findings)


def test_the_agents_revise_stands_on_clean_checks() -> None:
    out = qa_outcome.outcome([QACheck(name="qa:sections", ok=True)], report(Verdict.FAIL, [FINDING]))
    assert out.kind is QAOutcomeKind.REVISE
    assert out.findings == [FINDING]


def test_a_fail_with_nothing_to_fix_ships_with_the_reason_showing() -> None:
    out = qa_outcome.outcome([QACheck(name="qa:sections", ok=True)], report(Verdict.FAIL))
    assert out.kind is QAOutcomeKind.PASS and "named nothing to fix" in out.note


def test_both_clean_is_a_pass() -> None:
    out = qa_outcome.outcome([QACheck(name="qa:sections", ok=True)], report(Verdict.PASS))
    assert out.kind is QAOutcomeKind.PASS and not out.findings


def test_no_read_with_clean_checks_ships_unread_with_the_note() -> None:
    out = qa_outcome.outcome([QACheck(name="qa:sections", ok=True)], None)
    assert out.kind is QAOutcomeKind.PASS_UNREAD
    assert "could not read" in out.note
    assert "checks passed" in out.note  # checks RAN here - the wording may say so


def test_no_read_with_no_checks_never_claims_cleanliness() -> None:
    # (review) exit-3 INFRA reaches outcome([], None): nothing was measured, so the note must
    # say "no check ran" - not certify a cleanliness nobody established.
    out = qa_outcome.outcome([], None)
    assert out.kind is QAOutcomeKind.PASS_UNREAD
    assert "could not check" in out.note
    assert "checks passed" not in out.note


def test_unsupported_extension_is_infra_exit_3() -> None:
    # "we could not read it" is the runner's failure, never the document's.
    with pytest.raises(SystemExit) as died:
        precheck_run({"deliverable": "/work/answer.weird"})
    assert died.value.code == 3


def test_missing_parser_is_infra_exit_3_not_a_billed_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    # (review) an ImportError used to land as a qa:extraction FAIL → REVISE floor → a billed
    # rebuild for a frame-image gap. Simulate the gap: the import of pptx is halted.
    import sys

    monkeypatch.setitem(sys.modules, "pptx", None)
    with pytest.raises(SystemExit) as died:
        precheck_run({"deliverable": "/work/deck.pptx"})
    assert died.value.code == 3


def test_docx_extraction_preserves_document_order(tmp_path: object) -> None:
    # LIVE false-REVISE (task c0294b9c): paragraphs-then-tables extraction made a
    # glossary table the extracted tail - qa:truncation read a cell as a broken last sentence
    # and qa:sections saw scrambled titles. The extraction must walk the body in order.
    docx = pytest.importorskip("docx")
    from pathlib import Path

    from prismoid.ai.verification.qa_precheck import extract

    document = docx.Document()
    document.add_paragraph("Opening prose.")
    table = document.add_table(rows=1, cols=2)
    table.rows[0].cells[0].text = "OFHC"
    table.rows[0].cells[1].text = "Oxygen-Free High-Conductivity Copper"
    document.add_paragraph("The document closes like a sentence.")
    path = Path(str(tmp_path)) / "plan.docx"
    document.save(str(path))

    text = extract(str(path)).markdown
    lines = [line for line in text.splitlines() if line.strip()]
    assert lines[-1] == "The document closes like a sentence."
    assert lines.index("Opening prose.") < lines.index("| OFHC | Oxygen-Free High-Conductivity Copper |")


def test_the_gotcha_names_location_and_instruction_verbatim() -> None:
    out = qa_outcome.outcome([], report(Verdict.FAIL, [FINDING]))
    gotcha = qa_outcome.as_gotcha(out, attempt=1)
    assert "section 02" in gotcha and "the Company" in gotcha
    assert "Fix exactly what is named" in gotcha


def test_the_notes_sentence_never_says_unverified() -> None:
    out = qa_outcome.outcome([], report(Verdict.FAIL, [FINDING]))
    sentence = qa_outcome.notes_sentence(out)
    assert "QA notes" in sentence and "unverified" not in sentence.lower()


def test_the_read_prompt_announces_clipping_and_never_clips_silently() -> None:
    small = qa_outcome.read_prompt(extracted="short doc", outline_md="o", acceptance=["a"], checks=[])
    assert "NOT read" not in small and "short doc" in small

    big = qa_outcome.read_prompt(
        extracted="x" * (qa_outcome.READ_BELT_CHARS + 10), outline_md="o", acceptance=[], checks=[]
    )
    assert "NOT read" in big


# ── the pre-checks, over markdown fixtures ────────────────────────────────────────────────────

OUTLINE = """# The Report

## Continuity bible

Voice: analyst.

## Sections

### 01 · Introduction  (1200 words)

Set the scene.

### 02 · Findings  (1500 words)

- Figure `fig-s02-1`: a concept visual

### 03 · Conclusion  (1000 words)

Close it.
"""

GOOD = (
    "# The Report\n\n## Introduction\n\nAll is set. "
    + "word " * 3000
    + ".\n\n## Findings\n\nFigure (fig-s02-1): the concept.\n\nMore words end properly.\n\n## Conclusion\n\nIt ends well.\n"
)


def _run(doc: str, outline: str = OUTLINE, target_words: int = 3000) -> dict[str, dict[str, object]]:
    import tempfile
    from pathlib import Path

    base = Path(tempfile.mkdtemp(prefix="qa-precheck-"))
    (base / "doc.md").write_text(doc)
    (base / "outline.md").write_text(outline)
    import prismoid.ai.verification.qa_precheck as pc

    original = pc.EXTRACTED
    pc.EXTRACTED = str(base / "extracted.md")
    try:
        checks = precheck_run(
            {
                "deliverable": str(base / "doc.md"),
                "outline": str(base / "outline.md"),
                "target_words": target_words,
                "tolerance": 0.3,
                "figure_ids": ["fig-s02-1"],
            }
        )
    finally:
        pc.EXTRACTED = original
    return {c["name"]: c for c in checks}


def test_a_clean_document_passes_every_precheck() -> None:
    checks = _run(GOOD)
    failing = [name for name, c in checks.items() if not c["ok"]]
    assert failing == [], {name: checks[name]["detail"] for name in failing}


def test_a_missing_section_trips_sections() -> None:
    checks = _run(GOOD.replace("## Conclusion\n\nIt ends well.\n", ""))
    assert not checks["qa:sections"]["ok"]
    assert "Conclusion" in str(checks["qa:sections"]["detail"])


def test_a_mid_sentence_ending_trips_truncation() -> None:
    checks = _run(GOOD.rstrip() + '\n\nAnd then she said, "We will')
    assert not checks["qa:truncation"]["ok"]


def test_a_long_paragraph_ending_on_an_em_dash_dangles() -> None:
    # The dash half of _dangles, pinned byte-exact: an em-dash purge once rewrote
    # the tuple's escape into a duplicate hyphen and the check went blind to the character.
    from prismoid.ai.verification.qa_precheck import _dangles  # pyright: ignore[reportPrivateUsage]

    long_tail = ("the clause runs on and then breaks " * 8).strip()  # > 200 chars of prose
    assert _dangles(long_tail + "\u2014")
    assert _dangles(long_tail + ",")
    assert not _dangles(long_tail + ".")
    assert not _dangles("short \u2014")  # under the length floor, never a dangle


def test_a_missing_figure_marker_trips_figures() -> None:
    checks = _run(GOOD.replace("Figure (fig-s02-1): the concept.", "No figure here."))
    assert not checks["qa:figures"]["ok"]
    assert "fig-s02-1" in str(checks["qa:figures"]["detail"])


@pytest.mark.parametrize("factor,ok", [(1.0, True), (0.4, False)])
def test_size_holds_the_band(factor: float, ok: bool) -> None:
    words = int(3000 * factor)
    doc = (
        "# The Report\n\n## Introduction\n\n"
        + "word " * words
        + ".\n\n## Findings\n\nFigure (fig-s02-1): x.\n\n## Conclusion\n\nDone.\n"
    )
    checks = _run(doc)
    assert checks["qa:size"]["ok"] is ok, checks["qa:size"]["detail"]


def test_size_without_target_is_an_explicit_skip_row() -> None:
    # The old `if target:` silently DROPPED the check; the record must say "skipped", always.
    doc = "# T\n\nA short honest document. Done.\n"
    checks = _run(doc, target_words=0)
    assert checks["qa:size"]["ok"] is True
    assert "skipped" in str(checks["qa:size"]["detail"])


# ── the visual pass ──────────────────────────────────────────────────────────────────────────


def test_layout_check_fail_is_a_layout_revise_floor() -> None:
    from prismoid.schemas.qa import QACheck, QAFindingKind, QAOutcomeKind

    checks = [QACheck(name="qa:layout:overlap", ok=False, detail="slide 4: text ∩ picture 40%")]
    out = qa_outcome.outcome(checks, None)
    assert out.kind is QAOutcomeKind.REVISE
    assert out.findings[0].kind is QAFindingKind.LAYOUT
    assert "slide 4" in out.findings[0].instruction
    assert qa_outcome._kind_of("qa:layout:anything-new") is QAFindingKind.LAYOUT  # pyright: ignore[reportPrivateUsage]


# ── what the person reads: product words, never the rebuild's brief ───────────────────────────

ENGINE_WORDS = ("/work", "type=", "publish", "QA read", "deterministic", "house_document", "layout:")


def _person_texts() -> list[str]:
    """Every QA line a person reads: the visible notes of each outcome shape and the notes sentence."""
    leak = QAFinding(
        location="Document delivery",
        severity=QASeverity.MAJOR,
        kind=QAFindingKind.ACCEPTANCE,
        instruction="Publish /work/memo.docx as type='document'.",
    )
    house = QACheck(name="qa:layout:house", ok=False, detail="built without house_document; rebuild")
    revised = qa_outcome.outcome([house], report(Verdict.FAIL, [leak]))
    return [
        qa_outcome.notes_sentence(revised),
        qa_outcome.outcome([QACheck(name="qa:sections", ok=True)], None).note,
        qa_outcome.outcome([], None).note,
        qa_outcome.outcome([QACheck(name="qa:sections", ok=True)], report(Verdict.FAIL)).note,
    ]


@pytest.mark.parametrize("text", _person_texts())
def test_what_the_person_reads_carries_no_engine_words(text: str) -> None:
    """The findings are the rebuild's brief, in words an agent acts on; the line a person reads names
    what is still open in product words, so a path, a tool call or a check's id never reaches them."""
    assert text and not any(word in text for word in ENGINE_WORDS), text


def test_the_notes_sentence_names_what_is_still_open() -> None:
    house = QACheck(name="qa:layout:house", ok=False, detail="x")
    out = qa_outcome.outcome([house], report(Verdict.FAIL, [FINDING]))
    sentence = qa_outcome.notes_sentence(out)
    assert "layout" in sentence and "consistency" in sentence and "2" in sentence
