# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The collection normalizer (ADR-019), pinned - a collection never fails the thread.

The lockstep suite proves the model and its in-frame mirror REFUSE the same 21 mutations;
this suite proves the harvest normalizer REPAIRS every one of them into a collection the
model accepts - deterministically, idempotently, and with every change honestly noted. The
strict contract and the forgiving door are two halves of one law: the user always gets a
renderable board; the verifier still reads the truth about what was fixed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from prismoid.schemas.collection import Collection
from prismoid.schemas.collection_repair import repair_collection
from tests.support.samples import FIXTURE, MUTATIONS


def _bytes(doc: dict[str, Any]) -> bytes:
    return json.dumps(doc).encode()


def test_a_clean_collection_passes_untouched() -> None:
    r = repair_collection(_bytes(FIXTURE))
    assert r.ok and r.repairs == [], "no repair may ever touch a valid collection"
    assert r.content is not None
    Collection.model_validate(r.content)


@pytest.mark.parametrize("doc", MUTATIONS.values(), ids=MUTATIONS.keys())
def test_every_lockstep_mutation_repairs_to_a_valid_collection(doc: dict[str, Any]) -> None:
    """The exact JSONs the model refuses are the exact JSONs the normalizer must save."""
    r = repair_collection(_bytes(doc))
    assert r.ok, f"unrepairable: {r.repairs}"
    assert r.repairs, "a repair without a note is a silent rewrite - never"
    assert r.content is not None
    Collection.model_validate(r.content)


def test_the_pass_is_deterministic_and_idempotent() -> None:
    raw = _bytes(MUTATIONS["a bad date"])
    first = repair_collection(raw)
    assert first == repair_collection(raw), "same bytes in, same collection out"
    again = repair_collection(_bytes(first.content))  # pyright: ignore[reportArgumentType]
    assert again.repairs == [] and again.content == first.content, (
        "repaired bytes are a fixed point - a second pass changes nothing"
    )


def test_faithful_coercions_preserve_the_data() -> None:
    doc = MUTATIONS["an illegal select value"]
    r = repair_collection(_bytes(doc))
    assert r.ok and r.content is not None
    got = Collection.model_validate(r.content)
    stage = next(f for f in got.fields if f.key == "stage")
    assert "Shipped" in stage.options, "a stray select value becomes an option, never lost data"

    r2 = repair_collection(_bytes(MUTATIONS["a number cell holding a string"]))
    assert r2.ok and r2.content is not None
    got2 = Collection.model_validate(r2.content)
    row = next(row for row in got2.rows if "cost" in row.cells)
    assert row.cells["cost"] == 41.6, "a numeric string becomes the number it spells"


def test_a_timestamp_becomes_its_date() -> None:
    doc = json.loads(json.dumps(FIXTURE))
    doc["rows"][0]["cells"]["due"] = "2026-08-06T10:30:00Z"
    r = repair_collection(_bytes(doc))
    assert r.ok and r.content is not None
    got = Collection.model_validate(r.content)
    assert got.rows[0].cells["due"] == "2026-08-06"
    assert any("trimmed to its date" in n for n in r.repairs)


def test_a_board_without_any_groupable_field_demotes_to_table() -> None:
    doc = {
        "collection_version": 1,
        "title": "Notes",
        "fields": [{"key": "name", "label": "Name", "type": "text"}],
        "rows": [{"id": "r1", "cells": {"name": "x"}}],
        "views": [{"name": "Board", "layout": "board"}],
    }
    r = repair_collection(_bytes(doc))
    assert r.ok and r.content is not None
    got = Collection.model_validate(r.content)
    assert got.views[0].layout.value == "table", "a table always renders; a refusal never does"
    assert any("→ table" in n for n in r.repairs)


def test_the_floors_are_honest() -> None:
    truncated = repair_collection(b'{"collection_version": 1, "title": "x", "fie')
    assert not truncated.ok, "a truncated object is never guessed at"

    salvaged = repair_collection(_bytes(FIXTURE) + b"\n```done")
    assert salvaged.ok and any("trailing" in n for n in salvaged.repairs)

    fieldless = repair_collection(b'{"collection_version": 1, "title": "x", "fields": []}')
    assert not fieldless.ok, "fields cannot be invented"
