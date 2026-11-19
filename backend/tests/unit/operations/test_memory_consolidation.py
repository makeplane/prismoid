# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""memory_extract's pure half: a model proposes quotes, THIS code disposes - so this code is what
gets pinned. The quote gate, the brief and `replaces` are all deterministic."""

from __future__ import annotations

from prismoid.db.models.memory import Memory
from prismoid.operations.memory import extract_brief, keepable, normalise, quoted
from prismoid.schemas.outputs import MemoryExtract, QuotedMemory


def test_normalise_squashes_the_cosmetic_differences() -> None:
    assert normalise('  "Prefers   concise memos."  ') == "prefers concise memos."
    assert normalise("PREFERS CONCISE MEMOS.") == normalise("prefers concise memos.")


def test_a_quote_counts_only_when_it_is_verbatim_in_what_the_person_said() -> None:
    said = ["Write the Q3 memo.  Always   use euros, never dollars.", "The board."]
    assert quoted("Always use euros", said) == "Always use euros", "whitespace is not a difference"
    assert quoted("“always use euros”", said) is None, "case is the person's own"
    assert quoted("Always use pounds", said) is None, "a paraphrase is not a quote"
    assert quoted("Prefers euros", said) is None, "an agent's summary of them is not a quote"
    assert quoted("  ", said) is None
    assert quoted("I’m vegan", ["I'm vegan"]) == "I'm vegan", "phones type the curly apostrophe"


def test_keepable_keeps_quotes_and_never_an_agent_written_summary() -> None:
    said = ["From now on, reply in French."]
    out = MemoryExtract(
        items=[
            QuotedMemory(kind="instruction", lasts="standing", quote="reply in French"),
            QuotedMemory(kind="preference", lasts="standing", quote="The user prefers French replies."),
        ]
    )
    kept = keepable(out, said, [])
    assert [k.text for k in kept] == ["reply in French"]
    assert keepable("not an extract", said, []) == []


def test_keepable_drops_what_the_model_says_shapes_only_this_task() -> None:
    """A length or a format in the ask is this task's; only words the person says hold beyond it are kept."""
    said = ["Write the launch post, keep it under 120 words. I always write in British English."]
    out = MemoryExtract(
        items=[
            QuotedMemory(kind="preference", lasts="this_task", quote="keep it under 120 words"),
            QuotedMemory(kind="instruction", lasts="standing", quote="I always write in British English"),
            QuotedMemory(kind="preference", lasts="this_task", quote="Write the launch post"),
            QuotedMemory(kind="preference", lasts="this_task", quote="launch post"),
        ]
    )
    assert [k.text for k in keepable(out, said, [])] == ["I always write in British English"]


def test_a_contradiction_replaces_only_the_line_the_model_named() -> None:
    """Word overlap never supersedes: "always" and "never" share every other word, and both
    must stand unless the extraction names the line it corrects."""
    old = Memory(kind="instruction", text="Brian never joins the Monday call")
    said = ["Brian always joins the Monday call now"]
    named = keepable(
        MemoryExtract(
            items=[
                QuotedMemory(
                    kind="instruction",
                    lasts="standing",
                    quote="Brian always joins the Monday call",
                    replaces=1,
                )
            ]
        ),
        said,
        [old],
    )
    assert named[0].replaces is old
    unnamed = keepable(
        MemoryExtract(
            items=[
                QuotedMemory(kind="instruction", lasts="standing", quote="Brian always joins the Monday call")
            ]
        ),
        said,
        [old],
    )
    assert unnamed[0].replaces is None, "no line named, nothing superseded"
    stray = keepable(
        MemoryExtract(
            items=[QuotedMemory(kind="fact", lasts="standing", quote="Brian always joins", replaces=7)]
        ),
        said,
        [old],
    )
    assert stray[0].replaces is None, "a line number off the shown ledger counts for nothing"


def test_keepable_caps_at_three() -> None:
    said = ["a b c d e"]
    out = MemoryExtract(items=[QuotedMemory(kind="fact", lasts="standing", quote=w) for w in "abcde"])
    assert len(keepable(out, said, [])) == 3


def test_the_brief_fences_the_persons_words_and_numbers_the_ledger() -> None:
    brief = extract_brief(
        "The person's own private memory.", ["Write the Q3 memo", "use euros"], ["Prefers concise memos."]
    )
    assert "The person's own private memory." in brief
    assert "Write the Q3 memo" in brief and "use euros" in brief
    assert "1. Prefers concise memos." in brief
    assert "(nothing yet)" in extract_brief("x", ["hi"], [])


def test_bot_target_filter_demands_a_name_and_exactly_one_carrier() -> None:
    """The bot lane's wall: name required, carrier XOR - ambiguity is an error,
    never a broad read."""
    import uuid

    import pytest as _pytest

    from prismoid.db.models.memory import Memory, MemoryScope

    owner, project = uuid.uuid4(), uuid.uuid4()
    clauses = Memory.target_filter(MemoryScope.BOT, bot_name="reviewer", user_id=owner)
    assert len(clauses) == 3  # scope, name, carrier

    clauses = Memory.target_filter(MemoryScope.BOT, bot_name="reviewer", project_id=project)
    assert len(clauses) == 3

    with _pytest.raises(ValueError, match="bot_name"):
        Memory.target_filter(MemoryScope.BOT, user_id=owner)
    with _pytest.raises(ValueError, match="exactly one"):
        Memory.target_filter(MemoryScope.BOT, bot_name="reviewer")
    with _pytest.raises(ValueError, match="exactly one"):
        Memory.target_filter(MemoryScope.BOT, bot_name="reviewer", user_id=owner, project_id=project)
