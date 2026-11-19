# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The QA lane's values (ADR-026) - the editorial gate's findings, verdicts, and checks.

QA is NOT verification. Verification is the adversarial post-delivery receipt -
grounding, cross-family, signed. QA is the pre-delivery EDITORIAL pass on a long-form assembly:
did the document keep the outline's promises, read as one book, and survive its own seams. Its
findings are revision INSTRUCTIONS a rebuild can execute; nothing here is ever signed and no
`qa:`-prefixed check ever enters a `VerificationReport`.

Flat, frozen, clamped - a finding is one imperative sentence with an address, because the whole
report is fed back as the assembly's retry gotcha and a vague finding buys a vague rebuild.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

MAX_FINDINGS = 20
MAX_INSTRUCTION_CHARS = 300


class QASeverity(StrEnum):
    BLOCKER = "blocker"  # a reader would call the document broken
    MAJOR = "major"  # a reader would notice and judge it
    MINOR = "minor"  # worth fixing in the same rebuild, never alone worth one


class QAFindingKind(StrEnum):
    """The finding taxonomy - aligned with the published long-form coherence error types
    (BooookScore's eight), so findings are span-anchored instances of a validated
    vocabulary rather than a homegrown vibe scale."""

    MISSING_CONTENT = "missing_content"  # an outline promise nowhere in the document (omission)
    TRUNCATION = "truncation"  # text that ends mid-thought (the live docx failure class)
    CONTINUITY = "continuity"  # terminology/voice/naming drifts across a seam
    STRUCTURE = "structure"  # order, headings, TOC, numbering
    FORMATTING = "formatting"  # painted structure, literal markdown in a docx, template scraps
    FIGURE = "figure"  # missing, uncaptioned, unreferenced, or orphaned figures
    ACCEPTANCE = "acceptance"  # a contract acceptance line not met
    DUPLICATION = "duplication"  # the same ground covered twice across sections (the patchwork tell)
    DISCONTINUITY = "discontinuity"  # a sudden jump in time, topic, or perspective at a seam
    SALIENCE = "salience"  # detail that crowds out what the outline says matters
    INCONSISTENCY = "inconsistency"  # the document contradicts itself across sections
    LAYOUT = "layout"  # what the RENDERED page shows: overlap, overflow, orphan pages, ids on the face


class QAFinding(BaseModel):
    model_config = ConfigDict(frozen=True)

    # 200, not 120: span-anchored locations ("section 04, at 'The reefer fleet
    # grew…'") need room for the quote. The first live panel died 3 retries deep on exactly
    # this: the prompt demanded a quote the old clamp forbade - an instruction-schema
    # conflict no retry can win. The prompt states the budget; the schema affords it.
    location: str = Field(min_length=1, max_length=200)
    severity: QASeverity
    kind: QAFindingKind
    instruction: str = Field(min_length=1, max_length=MAX_INSTRUCTION_CHARS)  # ONE imperative sentence


class QAOutcomeKind(StrEnum):
    PASS = "pass"
    REVISE = "revise"
    PASS_UNREAD = "pass_unread"  # checks clean, the model read could not run - ship with the note


class QACheck(BaseModel):
    """One deterministic pre-check's row (the in-frame script's output). Deliberately its own
    type, not `verification.Check`: a qa check can never ride into a signed report by import."""

    model_config = ConfigDict(frozen=True)

    name: str  # qa:sections, qa:truncation, … - the qa: prefix is the namespace law
    ok: bool
    detail: str = Field(default="", max_length=500)
