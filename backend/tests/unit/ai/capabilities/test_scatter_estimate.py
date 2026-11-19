# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The scatter wave's per-seat projection is priced from the seat's model, not a flat $0.25.

With every class on a $0.10/$0.50-per-M model a scatter map spends ~$0.002, but a flat $0.25 a seat
projects a 2-seat wave at $0.50 against a $0.40 cap - parking the task on "Budget reached" before
a seat that really costs ~$0.001 runs. The estimate is the
fast-executor head's price over a gather seat's token envelope, raised to what this task's finished
seats measured, floored above $0, and the flat $0.25 only when the price is unknown."""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import pytest

from prismoid.ai import config
from prismoid.ai.agents.bot_compile import seat_table
from prismoid.ai.capabilities.lead_check import plan_estimate
from prismoid.ai.gateway.run_cost import SEAT_FLOOR_USD, seat_estimate
from prismoid.operations import budget_estimate
from prismoid.schemas.lead import LeadFacts, SubmitPlan, TurnOrigin
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.plan import PlanFacts, StepState
from prismoid.schemas.usage import Unit

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from prismoid.db.models.task import Task

_FLAT = Decimal("0.25")
_SEARCH = Decimal("0.025")  # the dev stack's engine: $25 per 1k queries
_SEAT = ModelClass.FAST_EXECUTOR


def _book(**prices: str) -> PlanFacts:
    """The head of the seat's class pricing `prices` (unit name -> USD), one search at `_SEARCH` unless set."""
    units = {Unit.SEARCH: _SEARCH} | {Unit(name): Decimal(usd) for name, usd in prices.items()}
    return PlanFacts(prices={_SEAT: units})


# gpt-6-luna as the dev stack's price book holds it: $0.10 / $0.50 per M, cache reads $0.01 per M, no cache-write tier.
_LUNA = _book(input_token="0.0000001", output_token="0.0000005", cache_read_token="0.00000001")
# The seeded head, Sonnet 5.5: $2 / $10 per M, cache reads $0.20, cache writes $2.50.
_SONNET = _book(
    input_token="0.000002",
    output_token="0.00001",
    cache_read_token="0.0000002",
    cache_write_token="0.0000025",
)
# What six gather seats of a real run (luna, $0.40 cap) each spent, searches included:
# 3 search queries average a seat ($0.075), its model calls $0.0006-0.0053.
_LIVE_SEATS = [Decimal(c) for c in ("0.0529", "0.0777", "0.0006", "0.0803", "0.1020", "0.1298")]


def _seat(book: PlanFacts, measured: list[Decimal] | None = None) -> Decimal:
    return seat_estimate(book, _SEAT, measured=measured or [], fallback=_FLAT)


def test_a_cheap_model_is_projected_at_its_own_price_and_its_searches() -> None:
    # 150k cached reads, 60k uncached in (its 50k writes bill as input: luna has no write tier), 10k out, 3 searches.
    assert _seat(_LUNA) == Decimal("0.0875")


def test_the_estimate_sits_at_the_live_seats_mean_and_under_their_worst() -> None:
    """Calibrated on that run: a seat's searches, not its tokens, are its cost. The tail past the
    mean is the budget-only guard's to hold, and a finished wave's measure raises the next one."""
    assert sum(_LIVE_SEATS) / len(_LIVE_SEATS) <= _seat(_LUNA) <= max(_LIVE_SEATS)


def test_the_seeded_head_prices_its_cache_writes() -> None:
    # 10k in x $2 + 150k x $0.20 + 50k x $2.50 + 10k x $10 per M + 3 x $0.025: above the old flat $0.25.
    assert _seat(_SONNET) == Decimal("0.35")


def test_without_a_search_engine_a_seat_is_its_tokens() -> None:
    no_search = _book(
        input_token="0.0000001", output_token="0.0000005", cache_read_token="0.00000001", search="0"
    )
    assert _seat(no_search) == Decimal("0.0125")


def test_an_unknown_price_keeps_the_flat_estimate() -> None:
    assert _seat(PlanFacts()) == _FLAT


def test_a_free_model_is_never_projected_at_zero() -> None:
    assert _seat(_book(input_token="0", output_token="0", search="0")) == SEAT_FLOOR_USD


def test_finished_seats_raise_an_estimate_they_outspent_never_lower_it() -> None:
    dear = [Decimal("0.10"), Decimal("0.14")]
    assert _seat(_LUNA, dear) == Decimal("0.12")
    assert _seat(_SONNET, dear) == Decimal("0.35")


def _seats(n: int) -> list[Any]:
    spec = {"lane_role": "gather"}
    return [SimpleNamespace(id=uuid.uuid4(), spec=spec, step_state=StepState.READY) for _ in range(n)]


@pytest.fixture
def two_seats(monkeypatch: pytest.MonkeyPatch) -> None:
    from prismoid.db.models.plan import PlanStep

    async def for_task(session: Any, task_id: Any) -> list[Any]:
        return _seats(2)

    async def no_measure(session: Any, task_id: Any, steps: Any) -> list[Decimal]:
        return []

    monkeypatch.setattr(PlanStep, "for_task", for_task)
    monkeypatch.setattr(budget_estimate, "measured_seats", no_measure)
    monkeypatch.setattr(budget_estimate.StepSpec, "model_validate", lambda raw: SimpleNamespace(**raw))


async def _project(monkeypatch: pytest.MonkeyPatch, prices: Any, limit: Decimal = Decimal("0.40")) -> Any:
    async def head_book(session: Any) -> PlanFacts:
        return prices

    monkeypatch.setattr(budget_estimate, "head_book", head_book)
    return await budget_estimate.scatter_projection(
        cast("AsyncSession", None),  # never touched past the patched reads
        cast("Task", SimpleNamespace(id=uuid.uuid4())),
        limit,
        task_spent=Decimal("0.002139"),
        project_limit=None,
        project_spent=Decimal(0),
    )


@pytest.mark.usefixtures("two_seats")
async def test_the_live_demo_wave_now_dispatches(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _project(monkeypatch, _LUNA) is None


@pytest.mark.usefixtures("two_seats")
async def test_an_unpriced_seat_still_parks_the_demo_wave(monkeypatch: pytest.MonkeyPatch) -> None:
    verdict = await _project(monkeypatch, PlanFacts())
    assert verdict is not None and verdict.projected == Decimal("0.50")


@pytest.mark.usefixtures("two_seats")
async def test_the_live_runs_first_wave_of_three_still_fits(monkeypatch: pytest.MonkeyPatch) -> None:
    """That run's first wave was 3 seats under the $0.40 cap: 3 x $0.0875 = $0.26 dispatches."""
    from prismoid.db.models.plan import PlanStep

    async def three(session: Any, task_id: Any) -> list[Any]:
        return _seats(3)

    monkeypatch.setattr(PlanStep, "for_task", three)
    assert await _project(monkeypatch, _LUNA) is None


@pytest.mark.usefixtures("two_seats")
async def test_one_price_moves_the_wave_projection_and_the_leads_seat_line_alike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One price home: a dearer input price raises the boundary's wave and a Lead plan's scatter seats alike."""
    plan = SubmitPlan.model_validate(
        {
            "say": "On it.",
            "plan": {
                "deliverable": "files",
                "steps": [
                    {
                        "label": "s1",
                        "agent": "researcher",
                        "title": "Find them",
                        "goal": "Find the companies.",
                        "recipe": {"kind": "scatter", "target_count": 60},
                    }
                ],
            },
        }
    ).plan
    width = config.current().worker.swarm_max_width
    dearer = _book(input_token="0.000004", output_token="0.0000005", cache_read_token="0.00000001")
    for book in (_LUNA, dearer):
        known = book.model_copy(update={"seats": seat_table(())})
        lead_seats = plan_estimate(plan, LeadFacts(turn=1, origin=TurnOrigin.WEB), known).steps[1]
        wave = await _project(monkeypatch, book, limit=Decimal("0.001"))  # two seats, over any cap this low
        assert lead_seats.seats and lead_seats.usd == _seat(book) * width
        assert wave.projected == (lead_seats.usd / width * 2).quantize(Decimal("0.01"))
    assert _seat(dearer) > _seat(_LUNA)
