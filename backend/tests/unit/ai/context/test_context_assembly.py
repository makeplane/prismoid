# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The pure half of context assembly: budgets bite deterministically, truncation is never
silent, relevance outranks recency, and the same inputs always make the same lanes."""

from __future__ import annotations

import uuid

from prismoid.db.models.memory import Memory
from prismoid.operations.context import (
    clip_head,
    clip_tail,
    fit_items,
    memory_lane,
    merge_unique,
    thread_note,
)
from prismoid.schemas.context import ContextItem, ContextPack


def _item(text: str) -> ContextItem:
    return ContextItem(text=text)


def test_fit_items_fills_in_order_and_reports_the_clip() -> None:
    items = [_item("a" * 40), _item("b" * 40), _item("c" * 40)]  # 10 tokens each
    kept, tokens, truncated = fit_items(items, budget=25)
    assert [i.text[0] for i in kept] == ["a", "b"], "greedy, in order"
    assert tokens == 20
    assert truncated is True, "the dropped third item is REPORTED, never silent"

    kept, tokens, truncated = fit_items(items, budget=30)
    assert len(kept) == 3 and truncated is False


def test_clip_head_keeps_the_opening_clip_tail_keeps_the_conclusion() -> None:
    text = "OPENING " + "x" * 100 + " CONCLUSION"
    head, _, h_trunc = clip_head(text, budget=5)
    tail, _, t_trunc = clip_tail(text, budget=5)
    assert head.startswith("OPENING") and h_trunc is True
    assert tail.endswith("CONCLUSION") and tail.startswith("…") and t_trunc is True

    short, tokens, trunc = clip_head("short", budget=100)
    assert (short, trunc) == ("short", False) and tokens > 0


def test_merge_unique_puts_relevance_first_and_each_row_once() -> None:
    a, b, c = (Memory(id=uuid.uuid4(), text=t, kind="note") for t in ("a", "b", "c"))
    merged = merge_unique([b, a], [a, c])
    assert [m.text for m in merged] == ["b", "a", "c"], "search order first, recency behind, no dupes"


def test_memory_lane_carries_ids_and_kinds_and_drops_to_none_when_empty() -> None:
    rows = [Memory(id=uuid.uuid4(), text="Prefers tables.", kind="preference")]
    lane = memory_lane("user", rows, budget=100)
    assert lane is not None
    assert lane.items[0].memory_id == rows[0].id and lane.items[0].kind == "preference"
    assert memory_lane("user", [], budget=100) is None, "an empty lane is no lane, not an empty block"


def test_same_inputs_same_pack() -> None:
    rows = [Memory(id=uuid.UUID(int=i), text=f"note {i}", kind="note") for i in range(3)]
    one = memory_lane("user", rows, budget=50)
    two = memory_lane("user", rows, budget=50)
    assert one == two, "assembly is deterministic - replays and reruns must agree"
    assert (
        ContextPack(lanes=[one] if one else []).memory_ids()
        == [r.id for r in rows][: len(one.items) if one else 0]
    )


def test_bot_lane_wears_its_name_as_the_heading() -> None:
    """The lane-instance label ("What reviewer remembers") overrides the stock
    heading, and a pack frozen before the field existed reads back with None."""
    from prismoid.ai.context.blocks import render_context_md
    from prismoid.operations.context import memory_lane
    from prismoid.schemas.context import ContextPack

    class Row:
        def __init__(self) -> None:
            import uuid

            self.text = "The Q3 workbook lives in the finance project."
            self.id = uuid.uuid4()
            self.kind = "fact"

    lane = memory_lane("bot", [Row()], 300, label="What reviewer remembers")  # pyright: ignore[reportArgumentType]
    assert lane is not None and lane.label == "What reviewer remembers"
    md = render_context_md(ContextPack(lanes=[lane], total_tokens=lane.tokens))
    assert "## What reviewer remembers" in md
    assert "Q3 workbook" in md

    # An old pack (no label key) still renders, under the stock heading.
    old = ContextPack.model_validate(
        {"lanes": [{"lane": "bot", "items": [{"text": "x"}], "tokens": 1}], "total_tokens": 1}
    )
    assert "## What this bot remembers" in render_context_md(old)


def test_the_fence_reads_widely_but_writes_only_home() -> None:
    """Personal work READS what its creator owns, a Project's included; a WRITE lands
    only in the work's own home - a Project task writes into no one's private conversation or
    memory, and a personal task (one moved out of a Project too) never writes into a Project's."""
    from prismoid.operations.context import PERSONAL, Fence

    launch, other = uuid.uuid4(), uuid.uuid4()
    personal, shared = Fence(PERSONAL), Fence(launch)

    assert personal.admits(PERSONAL) and personal.admits(launch), "control: personal work reads widely"
    assert shared.admits(launch) and not shared.admits(PERSONAL) and not shared.admits(other)

    assert personal.writes_to(PERSONAL) and not personal.writes_to(launch)
    assert shared.writes_to(launch)
    assert not shared.writes_to(PERSONAL), "a Project task writes into no one's private conversation"
    assert not shared.writes_to(other)


def test_lanes_carry_their_budget_to_the_wire() -> None:
    """The client reads each lane's ceiling from `context.assembled`; it never keeps a copy."""
    from prismoid.schemas.context import lane_views

    rows = [Memory(id=uuid.uuid4(), text="a note", kind="note")]
    lane = memory_lane("user", rows, budget=500)
    assert lane is not None and lane.budget == 500
    (view,) = lane_views(ContextPack(lanes=[lane], total_tokens=lane.tokens))
    assert view.budget == 500


def test_the_thread_lane_note_counts_turns_and_never_names_a_path() -> None:
    assert thread_note(1, 0) == "The conversation so far - 1 turn"
    assert thread_note(2, 0) == "The conversation so far - 2 turns"
    assert thread_note(5, 3) == "The conversation so far - 5 turns, 3 elided"
    assert "/task" not in thread_note(2, 0)
