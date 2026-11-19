# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The fleet recipe's two decisions made in code: the writer in its table part, and which seats a budget buys.

The table is the writer seated by its step's lane role. The seats are bought in catalog order while each one's
writer run, at its own row's prices, fits the budget left beside the table and the seats before it, so the
fleet's projected spend never passes the budget.
"""

from __future__ import annotations

from decimal import Decimal

from prismoid.ai.agents.base import definition_for
from prismoid.ai.deps import TaskDeps
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.operations.plan_shapes import fleet_plan
from prismoid.operations.planning_turns import TABLE_LABEL, fleet_seats
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.usage import Unit

PER_TOKEN = Decimal("0.000001")


def _row(ref: str, scale: int) -> ModelSpec:
    """An executor row whose every token costs `scale` micro-dollars."""
    provider, model_id = ref.split(":")
    price = PER_TOKEN * scale
    return ModelSpec(
        provider=provider,
        model_id=model_id,
        family=Family(provider),
        protocol="openai_compatible",
        classes=(ModelClass.FAST_EXECUTOR,),
        prices={
            Unit.INPUT_TOKEN: price,
            Unit.OUTPUT_TOKEN: price,
            Unit.CACHE_READ_TOKEN: price,
            Unit.CACHE_WRITE_TOKEN: price,
        },
    )


ROWS = [_row("a:dear", 10), _row("b:cheap", 1), _row("c:mid", 3), _row("d:cheap", 1)]
TABLE = _row("e:table", 1)


def test_the_table_is_the_writer_in_its_fleet_part() -> None:
    table = definition_for("writer", TaskDeps(lane_role="fleet_table"))
    assert table.output_ref == "document" and table.publishes_output
    assert not table.can_ask_user and table.connectors == "none"
    assert not table.quiet_stream, "the fleet's deliverable streams its draft"
    assert "One row for every answer you were given" in table.prompt
    assert definition_for("researcher", TaskDeps(lane_role="gather")).quiet_stream, "a wave stays quiet"
    assert "One row for every answer" not in definition_for("writer", TaskDeps()).prompt


def test_without_a_budget_every_eligible_row_sits() -> None:
    seats, priced = fleet_seats(ROWS, TABLE, None)
    assert seats == [r.ref for r in ROWS]
    assert {line.label for line in priced.steps} == {TABLE_LABEL, *seats}


def test_the_seats_a_budget_buys_fit_it_with_the_table() -> None:
    _, everything = fleet_seats(ROWS, TABLE, None)
    cost = {line.label: line.usd for line in everything.steps}
    left = cost[TABLE_LABEL] + cost["a:dear"] + cost["b:cheap"] + cost["d:cheap"]
    seats, priced = fleet_seats(ROWS, TABLE, left)
    assert seats == ["a:dear", "b:cheap", "d:cheap"], (
        "a seat that no longer fits is skipped, a cheaper one sits"
    )
    assert priced.usd <= left, "the fleet's projected spend stays within the budget left"
    assert priced.ceiling_usd <= left, "the estimate's high end never passes the budget left"


def test_the_first_seat_sits_even_when_the_budget_is_spent() -> None:
    seats, _ = fleet_seats(ROWS, TABLE, Decimal(0))
    assert seats == ["a:dear"], "the frontier's budget gate asks the owner; the plan never empties"


def test_every_fleet_step_is_priced_on_its_own_line() -> None:
    seats, priced = fleet_seats(ROWS, TABLE, None)
    steps, _, _ = fleet_plan("raise prices?", models=seats)
    labels = [spec.model_ref or TABLE_LABEL for _, spec in steps]
    assert labels == [*seats, TABLE_LABEL]
    cost = {line.label: line.usd for line in priced.steps}
    assert all(cost[label] > 0 for label in labels)
