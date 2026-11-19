# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a run is projected to cost: its size in the meter's units, priced at its class's routed head row.

The one home for a run's projected cost. The Lead's plan estimate and the budget boundary's wave projection
both read it, so the seat calibration and the price reader exist once. Prices are the catalog's: each row
carries the price book in force, and a head row prices a run as the meter bills it, the flex tier included.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from math import ceil
from typing import Final

from pydantic_ai.usage import RunUsage

from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.meter import flex_priced, units_of
from prismoid.ai.gateway.providers import ClassSettings
from prismoid.ai.gateway.resolve import chain_for
from prismoid.schemas.model_class import ModelClass
from prismoid.schemas.plan import PlanFacts, ProposedStep, RecipeKind, StepCost, step_cost
from prismoid.schemas.sizing import DOCUMENT_SIZES, WORDS_PER_PAGE
from prismoid.schemas.usage import Unit


@dataclass(frozen=True, slots=True)
class RunSize:
    """A run's size: requests, the whole prompt across them and the parts of it read from and written to
    the cache, output (with medium-effort thinking, ~250 tokens a request), searches, generated images, and
    how many runs it counts."""

    requests: int
    input: int
    cache_read: int
    cache_write: int
    output: int
    searches: int = 0
    images: int = 0
    runs: int = 1

    def times(self, n: int) -> RunSize:
        """`n` runs of this size."""
        return RunSize(*(n * getattr(self, f) for f in self.__slots__))

    def units(self, prices: Mapping[Unit, Decimal], *, flex: bool) -> dict[Unit, int]:
        """The native units the meter bills for a run this size on a row with `prices`: a cache tier the
        row does not price is billed as input, as a provider without that tier bills it."""
        usage = RunUsage(
            input_tokens=self.input,
            cache_read_tokens=self.cache_read if Unit.CACHE_READ_TOKEN in prices else 0,
            cache_write_tokens=self.cache_write if Unit.CACHE_WRITE_TOKEN in prices else 0,
            output_tokens=self.output,
        )
        units = units_of(usage, flex=flex) | {Unit.REQUEST: self.requests}
        units |= {Unit.SEARCH: self.searches} if self.searches else {}
        return units | ({Unit.IMAGE: self.images} if self.images else {})


# A step agent's target run, anchored to measured token counts: a loop reads 80% of its prompt from the
# cache and writes 12%. A bot or another seat is sized as the maker, the largest.
RUN_SIZES: Final[Mapping[str, RunSize]] = {
    "researcher": RunSize(10, 80_000, 64_000, 9_600, 5_000, searches=8),
    "writer": RunSize(2, 35_000, 28_000, 4_200, 3_500),
    "maker": RunSize(10, 110_000, 88_000, 13_200, 12_500),
    "operator": RunSize(5, 35_000, 28_000, 4_200, 2_450),
}
# A scatter seat, calibrated on a real run (six seats, 4-9 calls each): ~150k read back, ~50k written, 3 searches.
SEAT_SIZE: Final = RunSize(6, 210_000, 150_000, 50_000, 10_000, searches=3)
# One generated figure: a single image-model request.
FIGURE_SIZE: Final = RunSize(1, 0, 0, 0, 0, images=1)
# A free or near-free model still projects a cent a seat: never a wave estimated at $0.
SEAT_FLOOR_USD: Final = Decimal("0.01")


def step_size(step: ProposedStep) -> RunSize:
    """A step's run: its agent's size, a long-form document's outline and parts each a writer run."""
    size = RUN_SIZES.get(step.agent, RUN_SIZES["maker"])
    recipe = step.recipe
    if recipe is None or recipe.kind is not RecipeKind.LONGFORM or recipe.target_pages is None:
        return size
    bucket = next(b for b, s in DOCUMENT_SIZES.items() if recipe.target_pages <= s.cap)
    words = DOCUMENT_SIZES[bucket].section_target_words or WORDS_PER_PAGE
    return size.times(1 + ceil(recipe.target_pages * WORDS_PER_PAGE / words))


def with_prices(
    known: PlanFacts,
    catalog_rows: list[ModelSpec],
    *,
    search: Decimal,
    order: Mapping[str, list[str]] | None = None,
    settings: Mapping[str, ClassSettings] | None = None,
) -> PlanFacts:
    """`known` with each class's prices from its routed head row and one `search`, and the classes that row
    serves on flex."""
    prices: dict[ModelClass, dict[Unit, Decimal]] = {}
    flex: list[ModelClass] = []
    for model_class in ModelClass:
        chain = chain_for(model_class, catalog_rows, order=(order or {}).get(model_class.value))
        if not chain:
            continue
        prices[model_class] = dict(chain[0].prices) | {Unit.SEARCH: search}
        asked = (settings or {}).get(model_class.value)
        if asked is not None and asked.service_tier == "flex" and flex_priced(chain[0]):
            flex.append(model_class)
    return known.model_copy(update={"prices": prices, "flex": tuple(flex)})


def priced(
    label: str,
    model_class: ModelClass,
    size: RunSize,
    prices: Mapping[Unit, Decimal],
    *,
    flex: bool = False,
    seats: bool = False,
) -> StepCost:
    """One estimate line: a run of `size` at a row's `prices`, as the meter bills it."""
    return step_cost(label, model_class, size.units(prices, flex=flex), dict(prices), seats=seats)


def class_line(
    label: str,
    model_class: ModelClass,
    size: RunSize,
    known: PlanFacts,
    *,
    seats: bool = False,
) -> StepCost:
    """One estimate line at its class's head row prices in `known`."""
    return priced(
        label,
        model_class,
        size,
        known.prices.get(model_class, {}),
        flex=model_class in known.flex,
        seats=seats,
    )


def seat_estimate(
    known: PlanFacts, model_class: ModelClass, *, measured: Sequence[Decimal], fallback: Decimal
) -> Decimal:
    """One scatter seat's projected cost: `SEAT_SIZE` at its class's prices (floored, `fallback` when
    unpriced), raised never lowered by the task's `measured` seats (fail-closed on money)."""
    line = class_line("seat", model_class, SEAT_SIZE, known)
    projected = max(SEAT_FLOOR_USD, line.usd) if line.priced else fallback
    return max(projected, sum(measured, Decimal(0)) / len(measured)) if measured else projected
