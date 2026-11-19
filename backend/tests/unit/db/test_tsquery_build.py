# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The goal→tsquery tokeniser (memory retrieval v1). Recall-biased by construction: OR, not
AND - and deliberately dumb about stopwords, because `to_tsquery('english', …)` owns those."""

from __future__ import annotations

from prismoid.db.models.memory import tsquery_of


def test_words_become_an_or_query_in_order() -> None:
    assert tsquery_of("Write a concise board memo") == "write | concise | board | memo"


def test_short_words_drop_and_duplicates_collapse() -> None:
    # "a"/"of" fall to the length floor; repeated words keep their first position.
    assert tsquery_of("memo of a memo IS a MEMO") == "memo"


def test_punctuation_is_a_separator_not_a_term() -> None:
    assert tsquery_of("plots: matplotlib, not plotly!") == "plots | matplotlib | not | plotly"


def test_caps_at_24_terms() -> None:
    q = tsquery_of(" ".join(f"word{i:02d}" for i in range(40)))
    assert q.count("|") == 23, "24 terms exactly - a 40-word goal must not become a 40-arm query"


def test_empty_and_all_short_inputs_yield_nothing() -> None:
    assert tsquery_of("") == ""
    assert tsquery_of("a to я ¡!") == ""
