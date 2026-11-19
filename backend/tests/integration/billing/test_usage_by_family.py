# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Per-family cost breakdown, reconciled to the cent.

`prismoid tasks usage $ID --by family` groups a task's spend by model family - the view that makes a
fleet's cost legible ("$0.40 anthropic, $0.01 qwen, $0.00 llama") rather than one lump. The property
that matters is arithmetic: the families must **sum to the task total, exactly** - a breakdown that
does not add up to the total is a breakdown nobody can trust. Decimal all the way, because the ledger says the
ledger reconciles to the cent, not the nearest fifty, and a float would lose the cent it promises.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from prismoid.db.base import utcnow
from prismoid.db.models.task import Task
from prismoid.db.session import session_scope
from prismoid.operations import usage as ops
from prismoid.schemas.ids import TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.usage import Unit, UsageRecord
from tests.support.cast import member

pytestmark = pytest.mark.integration


def _record(task_id: uuid.UUID, family: str, cost: str) -> UsageRecord:
    return UsageRecord(
        task_id=TaskId(task_id),
        provider="local" if family != "anthropic" else "anthropic",
        model=f"{family}-model",
        family=Family(family),
        units={Unit.INPUT_TOKEN: 100, Unit.OUTPUT_TOKEN: 20, Unit.REQUEST: 1},
        cost_usd=Decimal(cost),
        price_version=1,
        at=utcnow(),
    )


async def test_three_families_sum_to_the_task_total_to_the_cent() -> None:
    async with session_scope() as session:
        actor, _ = await member(session, "Accountant")
        await session.flush()
        task = Task(id=uuid.uuid4(), goal="a fleet", creator_actor_id=actor.id, criteria={}, inputs=[])
        session.add(task)
        await session.flush()

        # Three families, precise sub-cent costs - the exact case a float column would mangle.
        await ops.record(session, _record(task.id, "anthropic", "0.41230000"))
        await ops.record(session, _record(task.id, "qwen", "0.00000344"))
        await ops.record(session, _record(task.id, "llama", "0.00000201"))
        await session.commit()

        summary = await ops.for_task(session, task.id)

    assert len(summary.by_family) == 3, list(summary.by_family)
    assert {str(f) for f in summary.by_family} == {"anthropic", "qwen", "llama"}
    # The load-bearing property: the families reconcile to the total, exactly.
    assert sum(summary.by_family.values()) == summary.total_usd
    assert summary.total_usd == Decimal("0.41230545"), "not a cent rounded away"
    assert summary.calls == 3


async def test_units_and_people_are_summed_by_the_database() -> None:
    """The unit counts add up across calls, an empty ledger is zero everywhere, and an unlabeled
    call is the creator's spend (rows from before the label always meant the creator)."""
    async with session_scope() as session:
        actor, _ = await member(session, "Bookkeeper")
        await session.flush()
        actor_id = actor.id
        task = Task(id=uuid.uuid4(), goal="a ledger", creator_actor_id=actor_id, criteria={}, inputs=[])
        session.add(task)
        await session.flush()
        empty = await ops.for_task(session, task.id)

        await ops.record(session, _record(task.id, "anthropic", "0.10000000"))
        await ops.record(session, _record(task.id, "anthropic", "0.20000000"))
        await ops.record(session, _record(task.id, "qwen", "0.00000300"))
        await session.commit()
        summary = await ops.for_task(session, task.id)

    assert (empty.total_usd, empty.calls, empty.by_family, empty.units, empty.by_person) == (
        Decimal("0"),
        0,
        {},
        {},
        (),
    )
    assert summary.units == {Unit.INPUT_TOKEN: 300, Unit.OUTPUT_TOKEN: 60, Unit.REQUEST: 3}
    assert summary.by_family == {Family("anthropic"): Decimal("0.3"), Family("qwen"): Decimal("0.000003")}
    assert [(p.actor_id, p.name, p.usd) for p in summary.by_person] == [
        (actor_id, "Bookkeeper", Decimal("0.300003"))
    ]
    assert summary.by_step == {} and summary.calls == 3
