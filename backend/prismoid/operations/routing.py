# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Routing table - which model a model class tries first, and what it falls back to.

Until now the chain order was `ORDER BY provider, model_id` - alphabetical, and true only by luck
(`anthropic` sorts before `local`). This is the table that makes order **data**: a per-class chain of
`provider:model_id` refs, versioned so a routing change is a diff you can read and revert, active so the
one in force is a query, not an argument. `order_for()` is what `resolve()` reads to break the
alphabetical tie; a class with no chain still falls back to alphabetical, so an empty table changes
nothing (the original behaviour) and a `put` changes exactly what it names. Each row also carries
its class's `settings` (reasoning effort, service tier): seeded from the starter catalog, carried
across every put, read by `settings_for()` into the requests that class makes.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.gateway.providers import ClassSettings
from prismoid.db.models.routing import RoutingTable
from prismoid.errors import ValidationFailed
from prismoid.schemas.model_class import ModelClass


class Routing(TypedDict):
    version: int
    chains: dict[str, list[str]]  # model class → ordered [provider:model_id, …]


async def get_routing(session: AsyncSession) -> Routing:
    """The active routing table: the version in force and each chained class's chain."""
    rows = await RoutingTable.active_rows(session)
    version = await RoutingTable.active_version(session)
    return {"version": version, "chains": {r.capability_class: list(r.chain) for r in rows if r.chain}}


async def put_routing(
    session: AsyncSession, actor_id: uuid.UUID | None, chains: dict[str, list[str]]
) -> Routing:
    """Install a new active version: the chains sent ARE the new table (an omitted class has none);
    class settings carry forward untouched. Versioned, so revertible."""
    if not chains:
        raise ValidationFailed("a routing put must set at least one class's chain", {"chains": "empty"})
    for cap, chain in chains.items():
        ModelClass(cap)  # an unknown class is a typo, not a new model class
        for ref in chain:
            if ":" not in ref:
                raise ValidationFailed(f"chain ref must be provider:model_id, got {ref!r}", {"ref": ref})
    settings = {
        r.capability_class: dict(r.settings) for r in await RoutingTable.active_rows(session) if r.settings
    }
    await _install(session, actor_id, chains, settings)
    return await get_routing(session)


async def _install(
    session: AsyncSession,
    actor_id: uuid.UUID | None,
    chains: Mapping[str, Sequence[str]],
    settings: Mapping[str, Mapping[str, object]],
) -> None:
    """Write the next version, one row per class that has a chain or settings, and make it the active one."""
    invalidate_order_cache()  # writers clear the read cache
    await RoutingTable.deactivate_all(session)
    next_version = await RoutingTable.max_version(session) + 1
    for cap in sorted(chains.keys() | settings.keys()):
        session.add(
            RoutingTable(
                id=uuid.uuid4(),
                version=next_version,
                capability_class=cap,
                chain=list(chains.get(cap, ())),
                settings=dict(settings.get(cap, {})),
                active=True,
                created_by=actor_id,
            )
        )
    await session.commit()


async def seed_routing(
    session: AsyncSession,
    chains: Mapping[str, Sequence[str]],
    *,
    settings: Mapping[str, Mapping[str, object]] | None = None,
    superseded: Mapping[str, Sequence[Sequence[str]]] | None = None,
    superseded_settings: Mapping[str, Sequence[Mapping[str, object]]] | None = None,
) -> int:
    """First-run chains and class settings, installed only where none exist or still read as an earlier
    starter shipped them; an operator's choice is never overwritten. Returns classes chained."""
    rows = await RoutingTable.active_rows(session)
    current = {r.capability_class: list(r.chain) for r in rows if r.chain}
    old = superseded_settings or {}
    held = {r.capability_class: dict(r.settings) for r in rows if r.settings}
    stale = {cap for cap, value in held.items() if any(value == dict(was) for was in old.get(cap, ()))}
    held = {cap: value for cap, value in held.items() if cap not in stale}
    was = superseded or {}
    missing = {
        cap: list(chain)
        for cap, chain in chains.items()
        if cap not in current
        or (current[cap] != list(chain) and any(current[cap] == list(old) for old in was.get(cap, ())))
    }
    unset = {cap: dict(value) for cap, value in (settings or {}).items() if cap not in held}
    if missing or unset or stale:
        await _install(session, None, {**current, **missing}, {**held, **unset})
    return len(missing)


# The same tiny TTL as the catalog cache - this read ran once per step attempt. The cached values are
# treated read-only by their readers (`resolve()` re-ranks into new lists; settings are frozen).
_order_cache: tuple[float, dict[str, list[str]], dict[str, ClassSettings]] | None = None


def invalidate_order_cache() -> None:
    global _order_cache
    _order_cache = None


async def _active(session: AsyncSession) -> tuple[dict[str, list[str]], dict[str, ClassSettings]]:
    """The active table's chains and class settings, through the read cache."""
    import time

    from prismoid.conf import settings as conf

    global _order_cache
    ttl = conf.worker.catalog_cache_s
    if ttl > 0 and _order_cache is not None and (time.monotonic() - _order_cache[0]) < ttl:
        return _order_cache[1], _order_cache[2]
    rows = await RoutingTable.active_rows(session)
    chains = {r.capability_class: list(r.chain) for r in rows if r.chain}
    asked = {r.capability_class: ClassSettings.read(r.settings, model_class=r.capability_class) for r in rows}
    if ttl > 0:
        _order_cache = (time.monotonic(), chains, asked)
    return chains, asked


async def order_for(session: AsyncSession) -> dict[str, list[str]]:
    """The per-class order `resolve()` reads. Empty ⇒ every class falls back to alphabetical."""
    return (await _active(session))[0]


async def settings_for(session: AsyncSession) -> dict[str, ClassSettings]:
    """Each class's settings, what `build()` hands `default_model_settings`. A class absent ⇒ provider defaults."""
    return (await _active(session))[1]


def to_out(routing: Routing) -> dict[str, Any]:
    """The wire shape plus the complete class domain, which the chains alone cannot state: the enum is
    the source, and this is the one place it crosses the wire."""
    return {
        "version": routing["version"],
        "chains": routing["chains"],
        "classes": [c.value for c in ModelClass],
    }
