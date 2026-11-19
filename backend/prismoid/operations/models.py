# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The model catalog - **adding a model is data, not a deploy**.

`operations` is the composer: it reads the catalog rows and the price book from Postgres and hands
the engine a list of `ModelSpec` values. `ai/` never touches a database, which is exactly
why the engine can be handed a fake catalog in a test and a real one in production without
knowing the difference.

**Designed for forty models tomorrow.** A new model on a known provider is one INSERT. A new
provider that speaks OpenAI (DeepSeek, xAI, Ollama, vLLM, a startup that launched this morning) is
one INSERT plus a `--base-url` - *zero code*. Only a genuinely novel SDK costs a function, and that
function goes in `ai/gateway/providers.py` where the audit can see it.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.providers import api_key_for
from prismoid.db.models.model_catalog import ModelCatalogEntry
from prismoid.db.models.price_book import PriceBookEntry
from prismoid.errors import Conflict, NotFound
from prismoid.operations import routing as route_ops
from prismoid.schemas.model_class import Family, ModelClass
from prismoid.schemas.usage import Unit

# The per-step read cache (the deferred "catalog cache", the small honest
# version). `execute_step` re-read the catalog (3 queries) + prices on EVERY attempt for data
# that changes on `prismoid models add`. A tiny TTL (conf `catalog_cache_s`, 0 disables)
# bounds staleness to hot-reload order; every mutator below calls `invalidate_catalog_cache()`
# so same-process admin writes are visible immediately. Values only - `ModelSpec` is frozen.
_CATALOG_CACHE: dict[bool, tuple[float, list[ModelSpec]]] = {}


def invalidate_catalog_cache() -> None:
    _CATALOG_CACHE.clear()


async def routed_catalog(session: AsyncSession) -> tuple[list[ModelSpec], dict[str, list[str]]]:
    """The enabled catalog and the routing table's per-class order - what `build()` and
    `chain_for()` take. A caller that seats a model reads both, or its class falls back to
    alphabetical order (which put Haiku 4.5 ahead of the chained Haiku 5.5)."""
    return await catalog(session, enabled_only=True), await route_ops.order_for(session)


async def catalog(session: AsyncSession, *, enabled_only: bool = True) -> list[ModelSpec]:
    """The routable catalog, as engine values. `resolve()` builds its chains from exactly this."""
    from prismoid.conf import settings

    ttl = settings.worker.catalog_cache_s
    if ttl > 0:
        hit = _CATALOG_CACHE.get(enabled_only)
        if hit is not None and (time.monotonic() - hit[0]) < ttl:
            return hit[1]
    specs = await _catalog_read(session, enabled_only=enabled_only)
    if ttl > 0:
        _CATALOG_CACHE[enabled_only] = (time.monotonic(), specs)
    return specs


async def _catalog_read(session: AsyncSession, *, enabled_only: bool) -> list[ModelSpec]:
    rows = await ModelCatalogEntry.all(session, enabled_only=enabled_only)
    version = await PriceBookEntry.current_version(session)  # the book in force, from data not a constant
    prices = await PriceBookEntry.current(session, version)

    specs: list[ModelSpec] = []
    for row in rows:
        key = (row.provider, row.model_id)
        specs.append(
            ModelSpec(
                provider=row.provider,
                model_id=row.model_id,
                family=Family(row.family),
                protocol=row.protocol,
                classes=tuple(ModelClass(c) for c in row.classes),
                context_window=row.context_window,
                base_url=row.base_url,
                prices=prices.get(key, {}),
                price_version=version,  # every call this spec meters records the version that priced it
            )
        )
    return specs


async def add_model(
    session: AsyncSession,
    *,
    provider: str,
    model_id: str,
    family: str,
    protocol: str,
    classes: list[str],
    context_window: int = 0,
    base_url: str | None = None,
    prices: dict[str, Decimal] | None = None,
    enabled: bool = False,
) -> ModelCatalogEntry:
    """One catalog row, added DISABLED: `--evaluate` runs the conformance check and `--enable` is a second,
    deliberate act, so a typo never routes traffic."""
    invalidate_catalog_cache()  # writers clear the read cache
    if await ModelCatalogEntry.get(session, provider, model_id) is not None:
        raise Conflict(f"{provider}:{model_id} is already in the catalog")

    for c in classes:
        ModelClass(c)  # raises ValueError on an unknown class - a typo is not a new class

    entry = ModelCatalogEntry(
        id=uuid.uuid4(),
        provider=provider,
        model_id=model_id,
        family=family,
        protocol=protocol,
        classes=classes,
        context_window=context_window,
        base_url=base_url,
        enabled=enabled,
    )
    session.add(entry)

    book_version = await PriceBookEntry.current_version(session)  # a new model joins the book in force
    for unit, usd in (prices or {}).items():
        Unit(unit)  # same: an unknown unit is a typo, not a new billing dimension
        session.add(
            PriceBookEntry(
                id=uuid.uuid4(),
                version=book_version,
                provider=provider,
                model=model_id,
                unit=unit,
                usd_per_unit=usd,
            )
        )

    await session.commit()
    return entry


async def seed_catalog(session: AsyncSession, entries: Sequence[Mapping[str, object]]) -> int:
    """Add each model that is not already in the catalog - idempotent, for a fresh deployment's
    first run (like `skills seed`). The `entries` are DATA the caller read from a YAML the operator
    controls, never model strings in code, so this stays audit-clean. Returns how many were
    added; re-running adds nothing.

    A seed enables what it adds because running it IS the operator's deliberate act - the
    "disabled until enabled" rule guards against a model routing traffic the moment it is *typed*,
    not against a catalog the operator explicitly asked to stand up.

    Seed is FIRST-RUN; `import_catalog` is MAINTENANCE (it updates existing rows too, under its
    own price discipline). Fixing a wrong price in a seeded row is an import, never a re-seed.
    """
    invalidate_catalog_cache()  # writers clear the read cache
    added = 0
    for e in entries:
        provider, model_id = str(e["provider"]), str(e["model_id"])
        if await ModelCatalogEntry.get(session, provider, model_id) is not None:
            continue  # already there - a re-seed never overwrites (matches skills seed)

        raw_prices = e.get("prices")
        prices: dict[str, Decimal] = (
            {str(u): Decimal(str(v)) for u, v in raw_prices.items()} if isinstance(raw_prices, dict) else {}
        )
        raw_classes = e.get("classes")
        classes = [str(c) for c in raw_classes] if isinstance(raw_classes, list) else []
        context = e.get("context")
        await add_model(
            session,
            provider=provider,
            model_id=model_id,
            family=str(e.get("family") or provider),
            protocol=str(e.get("protocol") or provider),
            classes=classes,
            context_window=int(context) if isinstance(context, int) else 0,
            base_url=str(e["base_url"]) if e.get("base_url") else None,
            prices=prices,
            enabled=bool(e.get("enabled", False)),
        )
        added += 1
    return added


async def reclass_untouched(
    session: AsyncSession, entries: Sequence[Mapping[str, object]], was: Mapping[str, Sequence[Sequence[str]]]
) -> int:
    """Carry an existing deployment forward with its starter: a row whose classes still read exactly
    as an earlier starter left them (`was`, the starter's `superseded.classes`) takes the classes
    `entries` give it now. A row an operator re-classed matches no earlier starter and is never
    touched, and neither is a row's `enabled` or its prices. Commits per row; returns how many
    rows it re-classed."""
    changed = 0
    for e in entries:
        ref = f"{e['provider']}:{e['model_id']}"
        raw = e.get("classes")
        classes = [str(c) for c in raw] if isinstance(raw, list) else []
        row = await ModelCatalogEntry.get(session, str(e["provider"]), str(e["model_id"]))
        if row is None or set(row.classes) == set(classes):
            continue
        if any(set(row.classes) == set(old) for old in was.get(ref, ())):
            await patch_model(session, ref, classes=classes)
            changed += 1
    return changed


async def import_catalog(
    session: AsyncSession, entries: Sequence[Mapping[str, object]], *, reprice: bool = False
) -> dict[str, int]:
    """Fleet management at ~70 rows: upsert the catalog from operator YAML.

    The contrast with `seed_catalog` is deliberate and documented in both places: **seed is
    first-run** (adds what is missing, never updates - matching skills seed), **import is
    maintenance** (updates classes/context/base_url/family/protocol on existing rows too). Two
    lines import never crosses:

    * it NEVER touches `enabled` on an existing row - enabling stays a deliberate act;
    * it never silently rewrites the price book: a price that DIFFERS from the book in force is
      refused with the fix named, unless `reprice=True` routes the changed keys through
      `prices.put_book` (version N+1, history untouched). Missing price keys are added to the
      current version either way - filling a hole is not rewriting history.
    """
    invalidate_catalog_cache()  # writers clear the read cache
    from prismoid.operations import prices as price_ops

    added = updated = 0
    repriced: dict[str, dict[str, str]] = {}
    version = await PriceBookEntry.current_version(session)
    book = await PriceBookEntry.current(session, version)

    for e in entries:
        provider, model_id = str(e["provider"]), str(e["model_id"])
        raw_prices = e.get("prices")
        wanted_prices: dict[str, Decimal] = (
            {str(u): Decimal(str(v)) for u, v in raw_prices.items()} if isinstance(raw_prices, dict) else {}
        )
        raw_classes = e.get("classes")
        classes = [str(c) for c in raw_classes] if isinstance(raw_classes, list) else []
        for c in classes:
            ModelClass(c)

        row = await ModelCatalogEntry.get(session, provider, model_id)
        if row is None:
            context = e.get("context")
            await add_model(
                session,
                provider=provider,
                model_id=model_id,
                family=str(e.get("family") or provider),
                protocol=str(e.get("protocol") or provider),
                classes=classes,
                context_window=int(context) if isinstance(context, int) else 0,
                base_url=str(e["base_url"]) if e.get("base_url") else None,
                prices=wanted_prices,
                enabled=bool(
                    e.get("enabled", False)
                ),  # a NEW row may arrive enabled - operator YAML is the act
            )
            added += 1
            continue

        if classes:
            row.classes = classes
        if e.get("family"):
            row.family = str(e["family"])
        if e.get("protocol"):
            row.protocol = str(e["protocol"])
        if isinstance(context := e.get("context"), int):
            row.context_window = int(context)
        row.base_url = str(e["base_url"]) if e.get("base_url") else row.base_url
        updated += 1

        current = book.get((provider, model_id), {})
        changed: dict[str, str] = {}
        for unit, usd in wanted_prices.items():
            Unit(unit)
            existing = current.get(Unit(unit))
            if existing is None:
                session.add(
                    PriceBookEntry(
                        id=uuid.uuid4(),
                        version=version,
                        provider=provider,
                        model=model_id,
                        unit=unit,
                        usd_per_unit=usd,
                    )
                )
            elif existing != usd:
                changed[unit] = str(usd)
        if changed:
            if not reprice:
                raise Conflict(
                    f"{provider}:{model_id} price differs from book v{version} for {sorted(changed)} - "
                    "a changed price is a REPRICE (version N+1), never an overwrite. "
                    "Re-run with --reprice to write the new book."
                )
            repriced[f"{provider}:{model_id}"] = changed

    if repriced:
        await price_ops.put_book(session, repriced)
    await session.commit()
    return {"added": added, "updated": updated, "repriced": len(repriced)}


async def doctor(session: AsyncSession) -> list[dict[str, str]]:
    """The catalog's health, as findings - the checks a 70-row fleet actually needs.

    Read-only, instrument-style (DB + local env, like `models evaluate`). Every finding names
    the fix; an empty list is the clean bill.
    """
    findings: list[dict[str, str]] = []
    rows = await ModelCatalogEntry.all(session, enabled_only=False)
    version = await PriceBookEntry.current_version(session)
    book = await PriceBookEntry.current(session, version)
    enabled = [r for r in rows if r.enabled]

    for row in enabled:
        prices = book.get((row.provider, row.model_id), {})
        if not prices:
            findings.append(
                {
                    "check": "enabled-but-unpriced",
                    "ref": f"{row.provider}:{row.model_id}",
                    "detail": f"enabled and serving {row.classes} with ZERO price rows - every call meters $0 "
                    f"silently. Fix: `prismoid models import <yaml>` with prices, or PUT /admin/price-book.",
                }
            )
        if "image-gen" in row.classes and Unit.IMAGE not in prices:
            findings.append(
                {
                    "check": "image-model-unpriced",
                    "ref": f"{row.provider}:{row.model_id}",
                    "detail": "serves image-gen with no `image` unit price - generated figures bill $0.",
                }
            )
        if not api_key_for(row.provider):
            findings.append(
                {
                    "check": "key-missing",
                    "ref": f"{row.provider}:{row.model_id}",
                    "detail": f"no API key resolves for provider {row.provider!r} - every call will 401 mid-run. "
                    f"Fix: set PRISMOID_GATEWAY_{row.provider.upper()}_API_KEY (or the first-party key).",
                }
            )

    for model_class in ModelClass:
        if not any(model_class.value in r.classes for r in enabled):
            findings.append(
                {
                    "check": "class-unserved",
                    "ref": model_class.value,
                    "detail": f"no enabled model serves {model_class.value!r} - any step asking for it fails. "
                    f"Fix: `prismoid models add … --classes {model_class.value}` + enable.",
                }
            )

    # A disabled row is parked on purpose (a starter row waiting for its key): its chain skips it
    # until it is enabled. Only a ref with no row behind it at all is dead.
    refs = {f"{r.provider}:{r.model_id}" for r in rows}
    routing = await route_ops.get_routing(session)
    for class_name, chain in routing["chains"].items():
        for ref in chain:
            if ref not in refs:
                findings.append(
                    {
                        "check": "routing-dead-ref",
                        "ref": ref,
                        "detail": f"routing chain for {class_name!r} names {ref!r}, which is not in the "
                        "catalog - the chain silently skips it. Fix: add the row or PUT a new table.",
                    }
                )
    return findings


async def patch_model(
    session: AsyncSession,
    ref: str,
    *,
    enabled: bool | None = None,
    classes: list[str] | None = None,
) -> ModelCatalogEntry:
    """Enable/disable and/or re-class one catalog row in ONE transaction; classes are validated before any
    write (`ValueError`, a 422), so a PATCH never half-applies."""
    invalidate_catalog_cache()  # writers clear the read cache
    provider, _, model_id = ref.partition(":")
    entry = await ModelCatalogEntry.get(session, provider, model_id)
    if entry is None:
        raise NotFound(f"no model {ref!r} in the catalog")

    if classes is not None:
        for c in classes:
            ModelClass(c)  # raises ValueError → 422 BEFORE any field is touched

    if enabled is not None:
        entry.enabled = enabled
    if classes is not None:
        entry.classes = classes
    await session.commit()
    return entry


async def set_enabled(session: AsyncSession, model_ref: str, enabled: bool) -> ModelCatalogEntry:
    """Enable/disable one catalog row. A thin call over `patch_model`, the one home for a
    catalog edit."""
    invalidate_catalog_cache()  # writers clear the read cache
    return await patch_model(session, model_ref, enabled=enabled)


async def set_classes(session: AsyncSession, model_ref: str, classes: list[str]) -> ModelCatalogEntry:
    """Change which model classes a model serves (a row, not a release); a thin call over `patch_model`."""
    invalidate_catalog_cache()  # writers clear the read cache
    return await patch_model(session, model_ref, classes=classes)


async def list_models(session: AsyncSession) -> list[ModelCatalogEntry]:
    return await ModelCatalogEntry.all(session, enabled_only=False)
