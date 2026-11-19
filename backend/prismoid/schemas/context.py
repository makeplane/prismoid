# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Context Pack - what a task was told beyond its ask, as a value (values live here).

Assembled once at planning (`operations/context.assemble`), frozen onto the task row, mounted
into every frame as `/task/context.md`, and recorded durably as a `context.assembled` event.
One pack, three surfaces - which is the observability contract: the file the agent read, the
section the planner saw, and the event the rail renders are all THIS object at assembly. The one
sanctioned drift: a task moved into a Project has its stored pack narrowed to what
that Project's members may read, so the file and later prompts hold a subset of what the
`context.assembled` event recorded at assembly time.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

type LaneName = Literal["instructions", "user", "project", "bot", "thread", "skills"]

LANES: tuple[LaneName, ...] = ("instructions", "user", "project", "bot", "thread", "skills")

# The lanes whose items may be one person's own - the creator's memory, a personal bot's ledger
# (see ADR-062). Their contents are the creator's to read (`events.redact_for`); a Project task
# keeps a `bot` lane only when it proves to be that Project's own bot's (`operations/context.Fence`).
PRIVATE_LANES: frozenset[LaneName] = frozenset({"user", "bot"})

PREVIEW_MAX = 120  # what a rail row can carry; the full text lives in the ledger


class ContextItem(BaseModel):
    """One remembered line. `memory_id` links it to the ledger; instructions and thread-derived
    lines have none - they come from the project row and the parent task, not from memories."""

    model_config = ConfigDict(frozen=True)

    text: str
    memory_id: UUID | None = None
    kind: str | None = None


class ContextLane(BaseModel):
    model_config = ConfigDict(frozen=True)

    lane: LaneName
    items: list[ContextItem] = Field(default_factory=list[ContextItem])
    tokens: int = 0
    truncated: bool = False  # ALWAYS set when the budget bit - truncation is never silent
    # The ceiling this lane was fitted to; None for an unbudgeted lane (a `memory_search` hit list)
    # and for packs frozen before this field.
    budget: int | None = None
    # A lane-instance heading ("What reviewer remembers"); None falls to the
    # lane's stock heading. Additive: packs frozen before this field read back with None.
    label: str | None = None


class ContextPack(BaseModel):
    model_config = ConfigDict(frozen=True)

    lanes: list[ContextLane] = Field(default_factory=list[ContextLane])
    total_tokens: int = 0

    def memory_ids(self) -> list[UUID]:
        """Every ledger row this pack spent - the ids the event carries and `use_count` bumps."""
        return [i.memory_id for lane in self.lanes for i in lane.items if i.memory_id is not None]


# ── the event's view: same shape, previews instead of full text ───────────────────────────────


class ContextItemView(BaseModel):
    model_config = ConfigDict(frozen=True)

    preview: str  # ≤ PREVIEW_MAX chars
    memory_id: UUID | None = None


class ContextLaneView(BaseModel):
    model_config = ConfigDict(frozen=True)

    lane: LaneName
    tokens: int
    truncated: bool = False
    budget: int | None = None  # the lane's ceiling; the client never hardcodes one
    items: list[ContextItemView] = Field(default_factory=list[ContextItemView])


def lane_views(pack: ContextPack) -> list[ContextLaneView]:
    """The pack, sized for the wire: previews ride NOTIFY; the ledger holds the full text."""
    return [
        ContextLaneView(
            lane=lane.lane,
            tokens=lane.tokens,
            truncated=lane.truncated,
            budget=lane.budget,
            items=[ContextItemView(preview=i.text[:PREVIEW_MAX], memory_id=i.memory_id) for i in lane.items],
        )
        for lane in pack.lanes
    ]


def shared_view(
    lanes: Sequence[ContextLaneView], memory_ids: Sequence[UUID]
) -> tuple[list[ContextLaneView], list[UUID]] | None:
    """An assembled context as someone other than its creator reads it: the lanes
    and memory ids they may see, or None when nothing in it is private. A private memory's id is
    the creator's too: only ids a shared lane's item vouches for stay (fail closed - an id no shared
    item carries is not served)."""
    shared = {i.memory_id for lane in lanes if lane.lane not in PRIVATE_LANES for i in lane.items}
    kept = [m for m in memory_ids if m in shared]
    private = any(lane.lane in PRIVATE_LANES and lane.items for lane in lanes)
    if not private and kept == list(memory_ids):
        return None
    return [
        lane.model_copy(update={"items": []}) if lane.lane in PRIVATE_LANES else lane for lane in lanes
    ], kept
