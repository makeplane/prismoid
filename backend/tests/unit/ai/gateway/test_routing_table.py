# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The routing table breaks the alphabetical tie.

`chain_for` used to return candidates in catalog order - `ORDER BY provider, model_id`, alphabetical,
true only by luck (`anthropic` sorts before `local`). The routing table makes order data: a per-class
chain of refs that leads, with everything unnamed falling in behind it keeping its alphabetical place.
An empty order changes nothing (the earlier behaviour); a chain changes exactly what it names.
"""

from __future__ import annotations

from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.resolve import chain_for
from prismoid.schemas.model_class import Family, ModelClass

FAST = ModelClass.FAST_EXECUTOR


def _spec(ref: str, family: str) -> ModelSpec:
    provider, _, model_id = ref.partition(":")
    return ModelSpec(
        provider=provider, model_id=model_id, family=Family(family), protocol="x", classes=(FAST,)
    )


# The catalog as operations hands it down - already alphabetical (anthropic < local).
CATALOG = [
    _spec("anthropic:haiku", "anthropic"),
    _spec("local:qwen", "qwen"),
    _spec("local:zephyr", "zephyr"),
]


def test_no_order_is_alphabetical() -> None:
    assert [m.ref for m in chain_for(FAST, CATALOG)] == ["anthropic:haiku", "local:qwen", "local:zephyr"]


def test_a_chain_leads() -> None:
    # The routing table says: try local:qwen first for fast-executor.
    ordered = chain_for(FAST, CATALOG, order=["local:qwen"])
    assert [m.ref for m in ordered] == ["local:qwen", "anthropic:haiku", "local:zephyr"], (
        "the chained ref leads"
    )


def test_a_full_chain_sets_the_whole_order() -> None:
    ordered = chain_for(FAST, CATALOG, order=["local:zephyr", "local:qwen", "anthropic:haiku"])
    assert [m.ref for m in ordered] == ["local:zephyr", "local:qwen", "anthropic:haiku"]


def test_unnamed_models_keep_their_alphabetical_place_behind_the_chain() -> None:
    # Only anthropic:haiku is chained; the two locals fall in behind it, still alphabetical among themselves.
    ordered = chain_for(FAST, CATALOG, order=["anthropic:haiku"])
    assert [m.ref for m in ordered] == ["anthropic:haiku", "local:qwen", "local:zephyr"]
