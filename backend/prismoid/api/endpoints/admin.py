# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Admin - routing table, model catalog, price book. Adding a model is data.

The model catalog admin (list/add/enable) and the price book. Every route is
`AdminDep` - the catalog is operator territory, and a mis-typed `base_url` routed to production is
exactly the mistake scope exists to keep behind a deliberate hand. Routing-table admin is not built yet.
"""

from __future__ import annotations

from prismoid.api.deps import AdminDep, SessionDep
from prismoid.api.serializers.admin import (
    ModelAddIn,
    ModelPatchIn,
    PricesIn,
    PricesOut,
    RoutingIn,
    RoutingOut,
    parse_prices,
)
from prismoid.api.serializers.models import ModelOut, model_out
from prismoid.errors import ValidationFailed
from prismoid.operations import admin_ops
from prismoid.operations import models as ops
from prismoid.operations import prices as price_ops
from prismoid.operations import routing as route_ops


async def get_routing(actor: AdminDep, session: SessionDep) -> RoutingOut:
    return RoutingOut(**route_ops.to_out(await route_ops.get_routing(session)))


async def put_routing(body: RoutingIn, actor: AdminDep, session: SessionDep) -> RoutingOut:
    routing = await admin_ops.put_routing_audited(session, actor, body.chains)  # new version + audit row
    return RoutingOut(**route_ops.to_out(routing))


async def list_models(actor: AdminDep, session: SessionDep) -> list[ModelOut]:
    return [model_out(e) for e in await ops.list_models(session)]


async def add_model(body: ModelAddIn, actor: AdminDep, session: SessionDep) -> ModelOut:
    try:
        prices = parse_prices(body.prices)
    except Exception as exc:  # a price that is not a decimal is a 422, not a silent zero
        raise ValidationFailed(f"a price is not a valid decimal: {exc}", {"prices": "not decimal"}) from exc
    entry = await admin_ops.add_model_audited(
        session,
        actor,
        provider=body.provider,
        model_id=body.model_id,
        family=body.family,
        protocol=body.protocol,
        classes=body.classes,
        context_window=body.context_window,
        base_url=body.base_url,
        prices=prices,
        enabled=False,  # **added disabled** - enabling is a second, deliberate act
    )
    return model_out(entry)


async def patch_model(id: str, body: ModelPatchIn, actor: AdminDep, session: SessionDep) -> ModelOut:
    """Enable/disable and/or re-class one catalog row. `id` is the provider:model_id ref.
    (Was `set_model_enabled`; grew classes with ADR-024 so every catalog edit rides the API.)

    ONE transaction (validates classes before writing): the two old sequential ops could commit the
    enable, then 422 on a bad class - a half-applied PATCH the client saw fail. A missing row is
    `patch_model`'s own 404, so the old `assert entry is not None` (which -O strips) is gone."""
    if body.enabled is None and body.classes is None:
        raise ValidationFailed("nothing to change", {"enabled": "or classes required"})
    try:
        entry = await admin_ops.patch_model_audited(
            session, actor, id, enabled=body.enabled, classes=body.classes
        )
    except ValueError as exc:  # an unknown class is a typo, not a new class
        raise ValidationFailed(f"unknown capability class: {exc}", {"classes": "unknown"}) from exc
    return model_out(entry)


async def get_prices(actor: AdminDep, session: SessionDep, version: int | None = None) -> PricesOut:
    return PricesOut(**await price_ops.get_book(session, version))  # ?version=N reads a past book


async def put_prices(body: PricesIn, actor: AdminDep, session: SessionDep) -> PricesOut:
    new_version = await admin_ops.put_prices_audited(session, actor, body.prices)  # version N+1 + audit row
    return PricesOut(**await price_ops.get_book(session, new_version))
