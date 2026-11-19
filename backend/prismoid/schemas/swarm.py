# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The scatter shape's values (W2) - wide-and-deep research as typed contracts.

Three shapes, all frozen, all crossing the engine as values:

* **`ScatterResult`** - what the scatter map step RETURNS: the entity definition, the
  count contract (the WANDR qualification-key grammar, single-level v1: `entity(N) → url`),
  and the fenced seat briefs. Pure code (`operations/swarm`) validates and expands it into
  the gather wave - the model proposes, code disposes (the outline precedent).
* **`ResearchRecord`** - the seat's atomic unit, adopted from WANDR's measurement design
  (arXiv:2608.14747): `{entity, url, excerpts, answer}` - every claim citation-backed and
  independently verifiable by re-reading its own cited page. Excerpts are VERBATIM; the
  partial-support failure ("excerpts that only half-support the claim") is the field's
  dominant rendering failure and the thing this shape exists to make checkable.
* **`RecordSet`** - what one gather seat RETURNS: its records plus an honest `exhausted`
  flag, which is what lets the re-scatter loop stop widening a seat whose territory is
  genuinely empty instead of burning rounds on it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from pydantic.json_schema import SkipJsonSchema

# The schema's absolute bounds; the conf dials (`swarm_max_width`, `swarm_max_rounds`)
# clamp INSIDE these. A wave wider than this is a plan, not a scatter.
SCATTER_MAX_SEATS = 12
SCATTER_MIN_SEATS = 2
RECORD_MAX_EXCERPTS = 3
SEAT_GOAL_MAX = 1200  # rescatter's re-brief rider must FIT under this - it truncates to it


class SeatBrief(BaseModel):
    """One gather seat's assignment. `not_here` is the scope fence (the outline
    vocabulary, reused verbatim): what this seat must NOT cover because a sibling owns it -
    dedup lives at dispatch, never at merge (the field's measured lesson)."""

    model_config = ConfigDict(frozen=True)

    id: str = Field(min_length=1, max_length=24)  # g01, g02, … - stable, lexical
    goal: str = Field(min_length=10, max_length=SEAT_GOAL_MAX)
    not_here: str = Field(min_length=5, max_length=600)
    expected: str = Field(min_length=5, max_length=400)  # what a finding looks like


class ScatterResult(BaseModel):
    """The map - the model's PROPOSAL of how the territory divides. Validated by
    `operations/swarm.validate_scatter`; an invalid map is the map step's retry gotcha,
    never a stuck plan (the `InvalidOutline` law, one lane over)."""

    model_config = ConfigDict(frozen=True)

    objective: str = Field(min_length=10, max_length=1200)
    entity: str = Field(min_length=3, max_length=200)  # what ONE record is
    target_count: int = Field(ge=1, le=5000)  # entity(N) - the count contract
    record_fields: list[str] = Field(min_length=1, max_length=8)  # answer keys per record
    seats: list[SeatBrief] = Field(min_length=SCATTER_MIN_SEATS, max_length=SCATTER_MAX_SEATS)
    notes: str = Field(default="", max_length=600)


class ResearchRecord(BaseModel):
    """One citation-backed claim - the WANDR record atom. `answer` keys should match the
    map's `record_fields`; the synthesis renders them. The seat names its page by `source`, the
    id its read was given; the system fills `url` from that read, so a URL is never re-typed."""

    model_config = ConfigDict(frozen=True)

    entity: str = Field(min_length=1, max_length=300)
    source: str = Field(default="", max_length=12, description="the id of the page you read it on, like S3")
    url: SkipJsonSchema[str] = Field(default="", max_length=2000)  # the system's, from `source`
    excerpts: list[str] = Field(min_length=1, max_length=RECORD_MAX_EXCERPTS)
    answer: dict[str, str] = Field(default_factory=dict)


class RecordSet(BaseModel):
    """One seat's harvest. `exhausted=True` is the honest empty: the seat searched its
    fence and the territory holds no more - the re-scatter loop excludes exhausted ground
    instead of re-plowing it."""

    model_config = ConfigDict(frozen=True)

    seat: str = Field(default="", max_length=24)
    records: list[ResearchRecord] = Field(default_factory=list[ResearchRecord], max_length=500)
    exhausted: bool = False
    note: str = Field(default="", max_length=400)


def synthesis_digest(document: str, cap: int = 2600) -> str:
    """The head of the synthesis document, cut at a block boundary - the scatter finale's
    thread answer (the answer is primary: the thread once ended in
    receipts + a card while the findings hid behind a click). Blocks that fit keep their
    whole shape; a block too big for the room is torn at its last full LINE under the cap
    instead of exempted or dropped (the review's pair of defects: a single 23KB table came
    back whole, and '# Heading' + table kept the heading while the findings vanished) - so
    a table loses tail rows, never a row's middle, a one-line prose block still drops whole
    when something is already on the page, and the cap binds always. A cut names itself."""
    marker = "\n\n*The full document continues in the artifact.*"
    blocks = document.strip().split("\n\n")
    kept: list[str] = []
    used = 0
    for block in blocks:
        sep = 2 if kept else 0
        if used + sep + len(block) <= cap:
            kept.append(block)
            used += sep + len(block)
            continue
        head = block[: max(cap - used - sep, 0)]
        line_break = head.rfind("\n")
        if line_break > 0:
            head = head[:line_break]  # the last whole line under the room - rows stay intact
        elif kept:
            head = ""  # a one-line block with company on the page: whole-block honesty, drop it
        if head.strip():
            kept.append(head.rstrip())
        return "\n\n".join(kept) + marker
    return document.strip()
