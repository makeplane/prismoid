# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One catalog row, as a value - the only thing the gateway needs to know about a model.

`ai/` is persistence-free, so the gateway never reads `model_catalog`. `operations`
loads the rows and hands them down as these. The engine is therefore testable, and extractable,
with a list of dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.usage import Unit


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """A routable model. Everything here came from a row; nothing here is in code."""

    provider: str
    model_id: str
    family: Family
    protocol: str
    classes: tuple[ModelClass, ...]
    context_window: int = 0
    base_url: str | None = None

    # USD per single unit, by native unit. A per-token price genuinely is this small, which is why
    # it is Decimal(20,12) in the database and Decimal here - a float would round the cent away
    # before the invoice ever saw it.
    prices: dict[Unit, Decimal] = field(default_factory=dict[Unit, Decimal])
    price_version: int = 1

    @property
    def ref(self) -> str:
        return f"{self.provider}:{self.model_id}"

    def serves(self, model_class: ModelClass) -> bool:
        return model_class in self.classes

    def cost(self, units: dict[Unit, int]) -> Decimal:
        """Price at **write time**, from the book that was current.

        An unpriced unit costs zero *and that is a bug we can see*: the ledger shows a call with
        tokens and no money, which is exactly the shape of "somebody added a model and forgot the
        price". Silently guessing a price would hide it.
        """
        total = Decimal("0")
        for unit, count in units.items():
            total += self.prices.get(unit, Decimal("0")) * Decimal(count)
        return total
