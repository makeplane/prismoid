# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The starter catalog routes on purpose: every class carries an explicit chain (nothing is left
to alphabetical order, which once put Haiku 4.5 at the head of fast-executor), every chain names
rows that exist and serve its class, and every chain spans at least two model families among the
rows a fresh deployment enables - so one provider's outage costs latency, never the answer.

The leads are the owner's ruling of 2026-11-06: Sonnet 5.5 leads fast-executor, Opus 5.5
leads long-context, Haiku 5.5 leads cheap-classifier and is priced at its <=100K-prompt card."""

from __future__ import annotations

from typing import Any

import pytest
import yaml

from prismoid.schemas.model_class import ModelClass
from tests.support.paths import BACKEND

YAML = BACKEND / "prismoid" / "runner" / "default_models.yaml"


def _seed() -> dict[str, Any]:
    return yaml.safe_load(YAML.read_text())


def _rows() -> dict[str, dict[str, Any]]:
    return {f"{r['provider']}:{r['model_id']}": r for r in _seed()["models"]}


@pytest.mark.parametrize("capability", list(ModelClass))
def test_every_class_has_an_explicit_chain_of_real_rows(capability: ModelClass) -> None:
    chain = _seed()["routing"].get(capability.value)
    assert chain, f"{capability.value} has no chain: its order would be alphabetical"
    rows = _rows()
    for ref in chain:
        assert ref in rows, f"{ref} is not a row of the starter catalog"
        assert capability.value in rows[ref]["classes"], f"{ref} does not serve {capability.value}"
    serving = {ref for ref, r in rows.items() if capability.value in r["classes"]}
    assert serving <= set(chain), (
        f"rows serving {capability.value} left out of its chain: {serving - set(chain)}"
    )


@pytest.mark.parametrize("capability", list(ModelClass))
def test_every_chain_spans_two_families_a_fresh_deployment_enables(capability: ModelClass) -> None:
    rows = _rows()
    enabled = [rows[ref] for ref in _seed()["routing"][capability.value] if rows[ref].get("enabled")]
    assert len({r["family"] for r in enabled}) >= 2


@pytest.mark.parametrize(
    ("capability", "lead"),
    [
        ("frontier-reasoner", "anthropic:claude-opus-5-5"),
        ("long-context", "anthropic:claude-opus-5-5"),
        ("code", "anthropic:claude-sonnet-5-5"),
        ("fast-executor", "anthropic:claude-sonnet-5-5"),
        ("cheap-classifier", "anthropic:claude-haiku-5-5"),
        ("computer-use", "anthropic:claude-sonnet-5-5"),
    ],
)
def test_the_owners_leads(capability: str, lead: str) -> None:
    assert _seed()["routing"][capability][0] == lead


def test_haiku_5_5_is_priced_at_its_short_prompt_card() -> None:
    row = _rows()["anthropic:claude-haiku-5-5"]
    assert row["prices"] == {
        "input_token": 0.0000001,
        "output_token": 0.0000005,
        "cache_read_token": 0.00000001,
        "cache_write_token": 0.000000125,
        "cache_write_1h_token": 0.0000002,
    }
    assert row["context"] == 1000000 and row["enabled"]


def test_the_superseded_record_names_real_rows_and_real_changes() -> None:
    """`superseded` is what a re-seed matches an untouched deployment against: every entry names a
    starter row or a chained class, and every old value differs from today's (an entry equal to
    today's would re-seed nothing and hide a missing record)."""
    seed, rows = _seed(), _rows()
    was = seed["superseded"]
    for ref, olds in was["classes"].items():
        assert ref in rows
        assert all(set(old) != set(rows[ref]["classes"]) for old in olds)
    for capability, olds in was["routing"].items():
        assert capability in seed["routing"]
        assert all(list(old) != seed["routing"][capability] for old in olds)
    for capability, olds in was["settings"].items():
        assert capability in seed["routing"]
        assert all(dict(old) != seed["settings"].get(capability, {}) for old in olds)


def test_the_judge_is_asked_on_the_standard_tier() -> None:
    """A flex judge queued past its wall and signed receipts with no verdict; the earlier flex value
    is superseded, so a re-seed moves an untouched deployment off it."""
    seed = _seed()
    assert "service_tier" not in seed["settings"].get("judge", {})
    assert {"service_tier": "flex"} in seed["superseded"]["settings"]["judge"]
