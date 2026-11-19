# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`models seed` - a fresh deployment's first-run catalog (ADR-012 deploy story).

The contract: it adds the entries it is given, enabled, with their prices; it is idempotent
(a second run adds nothing and overwrites nothing); and the shipped starter set is well-formed
and includes a cross-family judge (or nothing an Anthropic-written doc produces could verify).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from prismoid.db.models.model_catalog import ModelCatalogEntry
from prismoid.db.models.price_book import PriceBookEntry
from prismoid.db.session import session_scope
from prismoid.operations import models as ops

pytestmark = pytest.mark.integration

ENTRIES = [
    {
        "provider": "acme",
        "model_id": "seed-test-a",
        "classes": ["fast-executor"],
        "context": 128000,
        "enabled": True,
        "prices": {"input_token": 0.000002, "output_token": 0.000008},
    },
    {"provider": "acme", "model_id": "seed-test-b", "classes": ["judge"], "enabled": False},
]


async def test_seed_is_idempotent_and_enables_with_prices() -> None:
    async with session_scope() as session:
        added = await ops.seed_catalog(session, ENTRIES)
        assert added == 2

        a = await ModelCatalogEntry.get(session, "acme", "seed-test-a")
        assert a is not None and a.enabled is True and a.classes == ["fast-executor"]
        b = await ModelCatalogEntry.get(session, "acme", "seed-test-b")
        assert b is not None and b.enabled is False, "the seed honours each entry's enabled flag"

        prices = list(
            await session.scalars(select(PriceBookEntry).where(PriceBookEntry.model == "seed-test-a"))
        )
        assert {p.unit: p.usd_per_unit for p in prices} == {
            "input_token": Decimal("0.000002"),
            "output_token": Decimal("0.000008"),
        }

        # second run adds nothing and does not raise on the existing rows
        assert await ops.seed_catalog(session, ENTRIES) == 0


def test_shipped_starter_catalog_is_wellformed_and_cross_family() -> None:
    """The default_models.yaml the image ships must parse, and carry a judge whose family differs
    from a producer's - otherwise a fresh deploy could plan but never verify."""
    src = Path(ops.__file__).parent.parent / "runner" / "default_models.yaml"
    data = yaml.safe_load(src.read_text())
    models = data["models"]
    assert models, "the starter catalog is empty"

    families = {m["family"] for m in models}
    judges = {m["family"] for m in models if "judge" in m["classes"]}
    producers = {m["family"] for m in models if {"fast-executor", "frontier-reasoner"} & set(m["classes"])}
    assert len(families) >= 2, "a single family cannot verify its own work (§8.2)"
    assert judges - producers or (judges and len(judges | producers) >= 2), (
        "no judge from a family other than a producer's - cross-family verification would starve"
    )


async def test_seed_routing_chains_only_unchained_classes() -> None:
    """The starter chain lands on a fresh table, and an operator's chain is never overwritten -
    a re-seed adds nothing."""
    from prismoid.operations import routing as route_ops

    async with session_scope() as session:
        before = (await route_ops.get_routing(session))["chains"]
        mine = {**before, "judge": ["acme:seed-test-judge"]}
        await route_ops.put_routing(session, None, mine)

        seeded = await route_ops.seed_routing(
            session, {"judge": ["acme:other"], "image-gen": ["acme:seed-test-image"]}
        )
        chains = (await route_ops.get_routing(session))["chains"]
        assert chains["judge"] == ["acme:seed-test-judge"], "an operator's chain is never overwritten"
        assert seeded == (0 if "image-gen" in before else 1)
        assert chains["image-gen"] == before.get("image-gen", ["acme:seed-test-image"])
        assert await route_ops.seed_routing(session, {"image-gen": ["acme:seed-test-image"]}) == 0

        # Leave the table as found: the chains it had, or none active at all.
        if before:
            await route_ops.put_routing(session, None, before)
        else:
            from prismoid.db.models.routing import RoutingTable

            await RoutingTable.deactivate_all(session)
            await session.commit()
        route_ops.invalidate_order_cache()


async def test_a_reseed_carries_an_untouched_row_to_the_starters_classes() -> None:
    """A row whose classes still read exactly as an earlier starter left them takes the new
    starter's classes; a row an operator re-classed is theirs and is never touched."""
    rows = [
        {"provider": "acme", "model_id": "seed-test-old", "classes": ["computer-use"], "enabled": True},
        {"provider": "acme", "model_id": "seed-test-mine", "classes": ["computer-use"], "enabled": True},
    ]
    starter = [{**r, "classes": ["computer-use", "code"]} for r in rows]
    was = {"acme:seed-test-old": [["computer-use"]], "acme:seed-test-mine": [["computer-use"]]}
    async with session_scope() as session:
        await ops.seed_catalog(session, rows)
        await ops.set_classes(session, "acme:seed-test-mine", ["computer-use", "judge"])

        assert await ops.reclass_untouched(session, starter, was) == 1
        old = await ModelCatalogEntry.get(session, "acme", "seed-test-old")
        mine = await ModelCatalogEntry.get(session, "acme", "seed-test-mine")
        assert old is not None and old.classes == ["computer-use", "code"]
        assert mine is not None and mine.classes == ["computer-use", "judge"]
        assert await ops.reclass_untouched(session, starter, was) == 0, "a re-run changes nothing"


async def test_a_reseed_carries_an_untouched_chain_forward() -> None:
    """A chain that still reads exactly as an earlier starter left it takes the new starter's
    chain; an operator's chain is never overwritten."""
    from prismoid.db.models.routing import RoutingTable
    from prismoid.operations import routing as route_ops

    async with session_scope() as session:
        before = (await route_ops.get_routing(session))["chains"]
        await route_ops.put_routing(
            session, None, {**before, "computer-use": ["acme:a", "acme:b"], "judge": ["acme:mine"]}
        )
        was = {"computer-use": [["acme:a", "acme:b"]], "judge": [["acme:a"]]}
        starter = {"computer-use": ["acme:a", "acme:b", "acme:c"], "judge": ["acme:a", "acme:c"]}

        assert await route_ops.seed_routing(session, starter, superseded=was) == 1
        chains = (await route_ops.get_routing(session))["chains"]
        assert chains["computer-use"] == ["acme:a", "acme:b", "acme:c"]
        assert chains["judge"] == ["acme:mine"], "an operator's chain is never overwritten"
        assert await route_ops.seed_routing(session, starter, superseded=was) == 0

        if before:
            await route_ops.put_routing(session, None, before)
        else:
            await RoutingTable.deactivate_all(session)
            await session.commit()
        route_ops.invalidate_order_cache()


async def test_the_doctor_reads_a_disabled_row_in_a_chain_as_parked_not_dead() -> None:
    """A row the operator disabled (a starter row with no key yet) is skipped by its chain on
    purpose; only a ref with no row at all is a dead ref."""
    from prismoid.db.models.routing import RoutingTable
    from prismoid.operations import routing as route_ops

    async with session_scope() as session:
        await ops.seed_catalog(
            session,
            [{"provider": "acme", "model_id": "seed-test-off", "classes": ["judge"], "enabled": False}],
        )
        before = (await route_ops.get_routing(session))["chains"]
        await route_ops.put_routing(
            session, None, {**before, "judge": ["acme:seed-test-off", "acme:seed-test-absent"]}
        )
        dead = {f["ref"] for f in await ops.doctor(session) if f["check"] == "routing-dead-ref"}
        assert "acme:seed-test-absent" in dead
        assert "acme:seed-test-off" not in dead

        if before:
            await route_ops.put_routing(session, None, before)
        else:
            await RoutingTable.deactivate_all(session)
            await session.commit()
        route_ops.invalidate_order_cache()


async def test_class_settings_are_seeded_carried_by_a_put_and_read_safely() -> None:
    """The starter's class settings land where a class has none, a routing put carries them forward
    (a class it omits included), a re-seed never overwrites them, and a bad value is dropped on read
    while the class still routes."""
    from prismoid.ai.gateway.providers import ClassSettings
    from prismoid.db.models.routing import RoutingTable
    from prismoid.operations import routing as route_ops

    async with session_scope() as session:
        found = [
            (r.capability_class, list(r.chain), dict(r.settings))
            for r in await RoutingTable.active_rows(session)
        ]
        await RoutingTable.deactivate_all(session)
        await session.commit()

        starter = {"judge": {"service_tier": "flex"}, "cheap-classifier": {"effort": "low"}}
        assert await route_ops.seed_routing(session, {"judge": ["acme:j"]}, settings=starter) == 1
        asked = await route_ops.settings_for(session)
        assert asked["judge"] == ClassSettings(service_tier="flex")
        assert asked["cheap-classifier"] == ClassSettings(effort="low")

        await route_ops.put_routing(session, None, {"judge": ["acme:k"]})
        assert (await route_ops.get_routing(session))["chains"] == {"judge": ["acme:k"]}
        asked = await route_ops.settings_for(session)
        assert asked["judge"] == ClassSettings(service_tier="flex"), "a put keeps a chained class's settings"
        assert asked["cheap-classifier"] == ClassSettings(effort="low"), "and an omitted class's"

        await route_ops.seed_routing(session, {}, settings={"judge": {"service_tier": "default"}})
        assert (await route_ops.settings_for(session))["judge"] == ClassSettings(service_tier="flex")

        flex = {"judge": [{"service_tier": "flex"}]}
        await route_ops.seed_routing(session, {}, settings={}, superseded_settings=flex)
        asked = await route_ops.settings_for(session)
        assert asked["judge"] == ClassSettings(), "settings an earlier starter left untouched are superseded"
        assert asked["cheap-classifier"] == ClassSettings(effort="low"), "and nothing else moves"
        await route_ops.seed_routing(
            session, {}, settings={"judge": {"effort": "low"}}, superseded_settings=flex
        )
        assert (await route_ops.settings_for(session))["judge"] == ClassSettings(effort="low")
        await route_ops.seed_routing(
            session, {}, settings={}, superseded_settings={"judge": [{"effort": "x"}]}
        )
        assert (await route_ops.settings_for(session))["judge"] == ClassSettings(effort="low"), (
            "an operator's value matches no earlier starter and is never touched"
        )

        bad = {"decider": {"effort": "extreme", "service_tier": 3, "cache_ttl": "1h"}}
        await route_ops.seed_routing(session, {"decider": ["acme:d"]}, settings=bad)
        assert (await route_ops.settings_for(session))["decider"] == ClassSettings()
        assert (await route_ops.order_for(session))["decider"] == ["acme:d"]

        await RoutingTable.deactivate_all(session)
        version = await RoutingTable.max_version(session) + 1
        for cap, chain, settings in found:
            session.add(RoutingTable(version=version, capability_class=cap, chain=chain, settings=settings))
        await session.commit()
        route_ops.invalidate_order_cache()
