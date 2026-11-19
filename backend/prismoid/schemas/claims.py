# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Claims: a document's figures and cited statements as typed items, and how each held against its sources.

One bounded call reads a finished document and lists its claims (`ClaimSet`): each with the figure as the
document states it, the source labels it cites, and a span quoted verbatim from a source. Code then checks
every claim against the stored source text (`ai/verification/grounding.py`); the reader proposes, code decides.
`GROUNDING` picks which grounding a document's receipt runs: this one, or the suite's figure search.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class Grounding(StrEnum):
    """How a document's figures are grounded."""

    NUMBERS = "numbers"  # the document suite's search for each figure in the source prose
    CLAIMS = "claims"  # extracted claims, each checked against the source text it cites


GROUNDING: Final = Grounding.CLAIMS  # the selection the live gate decides; the other path stays runnable

MAX_CLAIMS: Final = 40  # one bounded call: a document's load-bearing claims, never every sentence


class Claim(BaseModel):
    """One checkable statement of the document, with where a source states it."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(max_length=8, description="`c1`, `c2`, ... in document order")
    statement: str = Field(max_length=400, description="the document's sentence or table row stating it")
    figure: str | None = Field(
        default=None,
        max_length=40,
        description="the quantity as the document states it, with the unit and scale its context gives "
        "(a cell under `Revenue ($M)` is `$41.6M`); empty for a claim with no quantity",
    )
    approx: bool = Field(default=False, description="the document hedges the figure: about, roughly, ~")
    cites: list[str] = Field(
        default_factory=list[str],
        max_length=6,
        description="the labels of the sources the document attaches to this claim; empty when it cites none",
    )
    quote: str | None = Field(
        default=None,
        max_length=400,
        description="the shortest span of a source, copied exactly, that states the claim; empty when no "
        "source states it",
    )
    quoted_figure: str | None = Field(
        default=None, max_length=40, description="the claim's quantity as written inside `quote`"
    )
    formula: str | None = Field(
        default=None,
        max_length=200,
        description="when the figure is computed from other claims: arithmetic over their ids, "
        "`(c1 - c2) / c2 * 100`",
    )


class ClaimSet(BaseModel):
    """The claim reader's whole answer: an empty list when the document states nothing checkable."""

    claims: list[Claim] = Field(default_factory=list[Claim], max_length=MAX_CLAIMS)


class ClaimStatus(StrEnum):
    """How one claim held against its sources."""

    SUPPORTED = "supported"  # a cited source states it, at the figure the document gives
    COMPUTED = "computed"  # its formula over other claims gives the figure
    WRONG_NUMBER = "wrong_number"  # the source states a different figure
    MISCOMPUTED = "miscomputed"  # its formula gives a different figure
    MISCITED = "miscited"  # a source states it, but not one the document cites
    UNSUPPORTED = "unsupported"  # no source it was given states it
    UNCHECKED = "unchecked"  # the check could not decide; the judge reads it

    @property
    def failed(self) -> bool:
        """A defect in the document, not a gap in the check."""
        return self in (
            ClaimStatus.WRONG_NUMBER,
            ClaimStatus.MISCOMPUTED,
            ClaimStatus.MISCITED,
            ClaimStatus.UNSUPPORTED,
        )


class ClaimFinding(BaseModel):
    """One claim's outcome and the evidence for it, in words a reader can act on."""

    model_config = ConfigDict(frozen=True)

    claim: str
    status: ClaimStatus
    detail: str
