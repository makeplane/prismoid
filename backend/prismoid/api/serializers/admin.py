# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - the model catalog's request/response shapes. `*In` = request, `*Out` = response.

Adding a model is **data, not a deploy**: this is the body that carries the row. Prices arrive as
strings (`"0.0000002"`) and are parsed to `Decimal` here, at the edge - a float price loses the cent
the ledger law promises, so JSON's number type never touches a price.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class ModelAddIn(BaseModel):
    """A model to add to the catalog. **Added disabled** - enabling is a second, deliberate act."""

    provider: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    family: str = Field(min_length=1)
    protocol: str = Field(min_length=1)  # anthropic | openai | google | openai_compatible
    classes: list[str] = Field(min_length=1)  # model classes it serves
    context_window: int = 0
    base_url: str | None = None  # for any OpenAI-compatible provider (T2)
    prices: dict[str, str] = Field(default_factory=dict)  # unit → usd-per-unit, as a string


class ModelPatchIn(BaseModel):
    """PATCH /admin/models/{ref}: enable/disable and/or re-class, each optional, one request.

    Replaced `ModelEnableIn`: "adding a model is data" implies EDITING one is too, and
    `set_classes` living only as a DB-direct CLI helper made re-classing the one catalog edit
    that could not ride the API. A None field is "leave it alone", never "clear it".
    """

    enabled: bool | None = None
    classes: list[str] | None = Field(default=None, min_length=1)


class PricesIn(BaseModel):
    """A reprice: `{ref: {unit: usd_per_unit}}`, prices as strings so JSON never rounds a cent.
    Copy-forward - name only what changes; the rest of the book carries into the new version."""

    prices: dict[str, dict[str, str]] = Field(min_length=1)


class PricesOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int
    prices: dict[str, dict[str, str]]


class RoutingIn(BaseModel):
    """A routing table: `{capability_class: [provider:model_id, …]}`. The chains you send ARE the new
    table - a class you omit falls back to alphabetical."""

    chains: dict[str, list[str]] = Field(min_length=1)


class RoutingOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: int
    chains: dict[str, list[str]]
    # Every model class that EXISTS, routed or not (`ModelClass`, via routing.to_out).
    # The chains say what is configured; this says what there is to configure - the difference
    # a client must not guess at, and the reason the console can stop hand-keeping the list.
    classes: list[str]


# The catalog's read shape (ModelOut / model_out) lives in serializers/models.py now - the
# fleet seat picker made it a non-admin read, and one shape must not have two homes.


def parse_prices(prices: dict[str, str]) -> dict[str, Decimal]:
    """Strings → Decimals, at the edge. A price that is not a valid decimal is a 422, not a silent 0."""
    return {unit: Decimal(value) for unit, value in prices.items()}
