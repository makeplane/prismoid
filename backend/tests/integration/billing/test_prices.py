# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Versioned repricing - a new book, never a rewritten invoice.

The one property that makes repricing safe to run against a live ledger: `put_book` writes a **new
version** and copies the rest of the book forward, so a one-model reprice changes one model and leaves
every past `usage_events` row priced exactly as it was billed. These pin the version bump, the
copy-forward, the intact history, and that new calls (via `catalog()`) pick up the new version.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from prismoid.db.session import session_scope
from prismoid.errors import ValidationFailed
from prismoid.operations import models as ops
from prismoid.operations import prices
from prismoid.schemas.usage import Unit

pytestmark = pytest.mark.integration


@pytest.fixture
async def book() -> tuple[str, str]:
    """Two priced models of this test's own (repriced, unchanged): whatever catalog the database
    holds, the reprice names a model that is there. The window cleanup removes both."""
    tag = uuid.uuid4().hex[:8]
    refs: list[str] = []
    async with session_scope() as session:
        for name in ("repriced", "unchanged"):
            entry = await ops.add_model(
                session,
                provider="local",
                model_id=f"prices-{name}-{tag}",
                family="local",
                protocol="openai_compatible",
                classes=["fast-executor"],
                prices={Unit.INPUT_TOKEN.value: Decimal("0.0000002")},
            )
            refs.append(f"{entry.provider}:{entry.model_id}")
    return refs[0], refs[1]


async def test_reprice_bumps_version_copies_forward_and_keeps_history(book: tuple[str, str]) -> None:
    repriced, unchanged = book  # `unchanged` is a different model - must ride forward untouched
    async with session_scope() as session:
        base = await prices.get_book(session)
        base_v = base["version"]
        old_price = base["prices"][repriced]["input_token"]

        new_v = await prices.put_book(session, {repriced: {"input_token": "9.99"}})
        assert new_v == base_v + 1, "a reprice is version N+1, not an edit of N"

        current = await prices.get_book(session)
        assert current["version"] == new_v
        assert Decimal(current["prices"][repriced]["input_token"]) == Decimal("9.99"), "override applied"
        assert current["prices"][unchanged] == base["prices"][unchanged], "other models copy forward"

        history = await prices.get_book(session, base_v)
        assert history["prices"][repriced]["input_token"] == old_price, "March's book is intact in April"


async def test_catalog_meters_new_calls_at_the_new_version(book: tuple[str, str]) -> None:
    repriced = book[0]
    async with session_scope() as session:
        before = await ops.catalog(session, enabled_only=False)
        base_v = before[0].price_version

        new_v = await prices.put_book(session, {repriced: {"input_token": "1.0"}})

        after = await ops.catalog(session, enabled_only=False)
        assert new_v == base_v + 1
        assert all(spec.price_version == new_v for spec in after), "every new call meters at the new book"
        repriced = next(s for s in after if s.ref == repriced)
        assert repriced.prices[Unit.INPUT_TOKEN] == Decimal("1.0")


async def test_an_empty_or_malformed_reprice_is_rejected(book: tuple[str, str]) -> None:
    repriced = book[0]
    async with session_scope() as session:
        with pytest.raises(ValidationFailed):
            await prices.put_book(session, {})  # a reprice must change something
        with pytest.raises(ValidationFailed):
            await prices.put_book(
                session, {"no-colon-ref": {"input_token": "1.0"}}
            )  # ref must be provider:model
        with pytest.raises(ValidationFailed):
            await prices.put_book(
                session, {repriced: {"not_a_unit": "1.0"}}
            )  # unit typo is not a new dimension
