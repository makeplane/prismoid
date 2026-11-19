# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The outcome datasets in `evals/datasets/`: every file loads, and the numbers on the label are true.

A wrong row corrupts every measurement made against it without failing anything, so this file pins
what a reader of the datasets would assume: each file validates through `load_rows`, ids are unique,
every label is in the six-outcome vocabulary (or is one of the two unscored kinds), a duplicate points
at a real, canonical row, and each row says its setup in one shape (dials in `dials`, attachments in
`inputs`, the turn as the length of its thread).

`outcomes.jsonl` holds every row imported from the problem books (their text was in the tree, so none can
be held out; the books are retired and these rows are their only home) and the public share of the rows a
label review added. The sealed twin lives outside the
repository and holds only added rows: those with `sha256(dedupe_key) mod 10 < 2` whose ask appears in no
book and no planning file. No test can read the twin, so what is pinned here is the rule.
"""

from __future__ import annotations

import hashlib
import re

import pytest

from evals.schema import Outcome, OutcomeRow, RowGroup, Unscored, load_rows
from tests.support.paths import BACKEND

DATASETS = BACKEND / "evals" / "datasets"

# file -> rows in it. outcomes.jsonl is every imported row plus the rows a review added.
SIZES = {"trivial50": 48, "slack10": 10}
IMPORTED_OUTCOMES = 470
DATASET_NAMES = (*SIZES, "outcomes")
SEALED_BUCKETS = 2  # buckets 0 and 1 of 10: a fifth of the added rows
# An added row in the sealed buckets whose ask was already in a planning file, so it was never sealed.
# The e2e rows' asks were already in the end-to-end test catalog.
EXPOSED_BEFORE_THE_SPLIT = frozenset(
    {
        "unclear:do-the-thing",
        "e2e:pm-work-007-two-writers-same-skill",
        "e2e:pm-talk-005-math-plain",
        "e2e:pm-talk-010-escalate-once",
        "e2e:uc-safety-019-system-prompt",
        "e2e:uc-safety-028-phishing",
        "e2e:uc-safety-034-weapons",
        "e2e:uc-routing-017-vague-or-route",
        "e2e:uc-routing-021-vague-or-route",
        "e2e:bm-talk-001-multi-hop-exact",
        "e2e:bm-work-007-long-horizon-multisite",
        "e2e:bm-followup-007-prior-artifact-reuse",
        "e2e:bm-routines-001-price-watch",
    }
)
# The problem books the imported rows came from, retired once every row had a dataset home.
RETIRED_BOOKS = frozenset(
    f"backend/evals/{book}.py"
    for book in (
        "range_book",
        "computer_routing_book",
        "computer_book",
        "navigate_book",
        "machine_book",
        "planner_book",
    )
)
# Setup that has a typed home in the row context, so `facts` may not carry it.
TYPED_SETUP = frozenset(
    {"deliverable", "scale", "clarify", "work_mode", "mode", "turn", "has_inputs", "attachments"}
)


def _rows(name: str) -> list[OutcomeRow]:
    return load_rows(DATASETS / f"{name}.jsonl")


def _bucket(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest(), 16) % 10


def _imported(rows: list[OutcomeRow]) -> list[OutcomeRow]:
    return [row for row in rows if row.source]


def _added(rows: list[OutcomeRow]) -> list[OutcomeRow]:
    return [row for row in rows if not row.source]


@pytest.mark.parametrize(("name", "size"), SIZES.items())
def test_each_dataset_loads_with_its_documented_size(name: str, size: int) -> None:
    assert len(_rows(name)) == size


def test_outcomes_is_every_imported_row_plus_the_rows_a_review_added() -> None:
    rows = _rows("outcomes")
    assert len(_imported(rows)) == IMPORTED_OUTCOMES
    assert len(rows) == IMPORTED_OUTCOMES + len(_added(rows))


def test_no_two_datasets_share_an_id() -> None:
    ids = [row.id for name in DATASET_NAMES for row in _rows(name)]
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("name", DATASET_NAMES)
def test_every_label_is_in_the_vocabulary(name: str) -> None:
    for row in _rows(name):
        assert row.expected_outcome in set(Outcome) | set(Unscored), row.id
        assert set(row.allowed_outcomes) <= set(Outcome), row.id


def test_trivial50_is_forty_talk_asks_and_eight_traps() -> None:
    groups = [row.group for row in _rows("trivial50")]
    assert groups.count(RowGroup.TRIVIAL) == 40
    assert groups.count(RowGroup.TRAP) == 8
    assert all(
        row.expected_outcome is Outcome.REPLY for row in _rows("trivial50") if row.group is RowGroup.TRIVIAL
    )


def test_slack10_is_ten_replies_with_no_work() -> None:
    rows = _rows("slack10")
    assert all(row.expected_outcome is Outcome.REPLY for row in rows)
    assert all(row.max_tool_calls == 0 and row.max_machines == 0 and row.max_artifacts == 0 for row in rows)


def test_a_duplicate_points_at_another_row_that_is_not_itself_a_duplicate() -> None:
    by_id = {row.id: row for row in _rows("outcomes")}
    for row in by_id.values():
        if row.duplicate_of is None:
            continue
        assert row.duplicate_of != row.id, row.id
        assert row.duplicate_of in by_id, f"{row.id} duplicates {row.duplicate_of}, which is not in the file"
        assert by_id[row.duplicate_of].duplicate_of is None, f"{row.id} points at a duplicate"


def test_every_imported_routing_row_names_a_retired_book_line_or_one_that_exists() -> None:
    for row in _imported(_rows("outcomes")):
        assert row.source, row.id
        path, _, line = row.source.rpartition(":")
        assert int(line) >= 1, f"{row.id}: {row.source}"
        file = BACKEND.parent / path
        if path in RETIRED_BOOKS:
            assert not file.exists(), f"{row.id}: {path} is back; drop it from RETIRED_BOOKS"
            continue
        assert file.is_file(), f"{row.id}: {row.source}"
        assert int(line) <= len(file.read_text(encoding="utf-8").splitlines()), f"{row.id}: {row.source}"


@pytest.mark.parametrize("name", DATASET_NAMES)
def test_every_row_says_when_it_was_last_checked_against_the_world(name: str) -> None:
    assert all(row.fresh_as_of for row in _rows(name))


def test_a_row_a_review_touched_says_who_why_and_when() -> None:
    rows = _rows("outcomes")
    for row in rows:
        if row.reviewed_by:
            assert row.label_note and row.fresh_as_of, row.id
    for row in _added(rows):  # no source to point at, so the review is the row's provenance
        assert row.reviewed_by, row.id


def test_the_machine_count_follows_the_label() -> None:
    expected = {Outcome.PLAN: "0-1", Outcome.PLAN_WITH_MACHINE: 1}
    for row in _rows("outcomes"):
        if row.scored:
            assert row.expected_machines == expected.get(Outcome(row.expected_outcome), 0), row.id


def test_no_public_added_row_hashes_into_the_sealed_buckets() -> None:
    for row in _added(_rows("outcomes")):
        sealed = _bucket(row.dedupe_key) < SEALED_BUCKETS
        assert not sealed or row.id in EXPOSED_BEFORE_THE_SPLIT, f"{row.id} belongs to the sealed twin"


@pytest.mark.parametrize("name", DATASET_NAMES)
def test_setup_has_one_shape(name: str) -> None:
    for row in _rows(name):
        context = row.context
        assert not set(context.facts) & TYPED_SETUP, (
            f"{row.id}: {set(context.facts) & TYPED_SETUP} has a typed home"
        )
        assert all((BACKEND / path).is_file() for path in context.inputs), row.id


@pytest.mark.parametrize("name", DATASET_NAMES)
def test_the_turn_is_the_length_of_the_thread(name: str) -> None:
    for row in _rows(name):
        earlier = len(row.context.turns) + sum(turn.role == "user" for turn in row.prior_turns)
        assert row.context.turn == earlier + 1, f"{row.id}: turn {row.context.turn} after {earlier} turns"


def test_no_row_sends_an_unfilled_placeholder() -> None:
    for name in DATASET_NAMES:
        for row in _rows(name):
            said = [row.message, *row.context.turns, *(t.text for t in row.prior_turns)]
            assert not any("{base}" in text for text in said), (
                f"{row.id}: the live runner would send it literally"
            )


def test_a_note_names_only_rows_a_reader_can_see() -> None:
    rows = [row for name in DATASET_NAMES for row in _rows(name)]
    ids = {row.id for row in rows}
    prefixes = {row.id.partition(":")[0] for row in rows if ":" in row.id}
    for row in rows:
        for note in (row.label_note or "", row.rule_note):
            named = {
                f"{p}:{slug}" for p, slug in re.findall(r"([\w-]+):([a-z0-9][\w-]*)", note) if p in prefixes
            }
            assert named <= ids, f"{row.id} names {sorted(named - ids)}, which no public file holds"
