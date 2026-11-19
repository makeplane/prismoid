# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`resolve()` - a model class → a metered, fallback-chained model.

This is the only thing application code ever asks the gateway for, and it asks in the **only
vocabulary it is allowed to speak**: a `ModelClass`. It never names a model, never
names a provider, and never knows which one served it.

The chain is `FallbackModel(Metered(a), Metered(b), …)`. Three properties fall out, and all matter:

* **The meter is inside the fallback, not outside it.** If provider A rate-limits and B answers,
  the ledger records **B**, because the wrapper that fired is B's. A meter wrapped around the
  outside would attribute B's tokens to A's price, and the bill would be wrong in a way nobody
  would ever catch.
* **Order is data** - the chain comes from the catalog the caller was handed, and `ai/` never
  chooses it. Changing who is tried first is a row, not a release.
* **A run that failed over stays failed over** (`StickyFallback`): once row A browned out and B
  answered, every later request of the run starts at B, and A is tried again only after the rest
  of the chain. The model is built per agent, so the state lives exactly one run.

**Where the order comes from:** the active `routing_table` version (`operations.routing.order_for`,
edited through `PUT /v1/admin/routing-table` and seeded by `prismoid models seed` from the starter
catalog's `routing:` block) gives each class its chain; `chain_for` puts those refs first, in that
order. A class with no chain - or an enabled row its chain does not name - falls in behind in
catalog order, which is alphabetical (`ORDER BY provider, model_id`): an accident, not intent, so
every class in the starter catalog carries an explicit chain.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import logfire
from pydantic_ai.models.fallback import FallbackModel

from prismoid.ai.gateway import throttle
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.meter import EmitUsage, MeterContext, MeteredModel
from prismoid.ai.gateway.providers import ClassSettings, build_model
from prismoid.errors import ModelMisconfigured, PolicyDenied, ProviderUnavailable
from prismoid.schemas.model_class import DEGRADES_TO, EXECUTOR_CLASSES, Family, JudgeFamily, ModelClass
from prismoid.schemas.usage import Unit

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage, ModelRequestAttempt, ModelResponse
    from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
    from pydantic_ai.settings import ModelSettings

# How many chain candidates `resolve()` will actually construct when the operator has not
# explicitly chained more in the routing table. See the comment at the build site.
CHAIN_CAP = 4


def chain_for(
    model_class: ModelClass,
    catalog: list[ModelSpec],
    *,
    order: list[str] | None = None,
    exclude_family: Family | None = None,
    quality: str | None = None,
) -> list[ModelSpec]:
    """Which models can serve this class, in the order we will try them.

    `order` is the routing table's chain for this class: refs (`provider:model_id`) lead in the
    order given, and models not in the chain fall in behind them keeping the catalog's alphabetical
    order. An empty/absent `order` ⇒ pure alphabetical, which is the original behaviour.

    `exclude_family` is how cross-family verification is enforced: the verifier is
    handed the producer's family and told to be anything else. A model grading its own homework is
    not verification, and this is the line that prevents it.

    `quality` is the requirement dial: `"economy"`/`"max"` re-rank ONLY the unchained
    tail by a static price comparator - an explicitly-chained ref is operator intent and outranks
    the dial; anything else (None, "balanced") changes nothing. Unpriced models sort LAST under
    both dials: a missing price must never be rewarded as "free" (the enabled-but-unpriced hole
    `models doctor` exists to surface). The judge lane never passes a quality - verification
    integrity is not a cost dial. (`prefer_family` once lived here with zero
    callers - removed; this comparator is the ranking hook it never became.)
    """
    candidates = [m for m in catalog if m.serves(model_class)]
    if exclude_family is not None:
        candidates = [m for m in candidates if m.family != exclude_family]
    if order:  # routing table: chained refs lead in order; the rest keep their (alphabetical) place
        rank = {ref: i for i, ref in enumerate(order)}
        candidates.sort(key=lambda m: rank.get(m.ref, len(order)))

    if quality in ("economy", "max"):
        chained = set(order or ())
        head = [m for m in candidates if m.ref in chained]
        tail = [m for m in candidates if m.ref not in chained]

        def blended(m: ModelSpec) -> Decimal | None:
            """One comparable number per model: a call-shaped token blend, or the image unit."""
            if model_class is ModelClass.IMAGE_GEN:
                return m.prices.get(Unit.IMAGE)
            input_price = m.prices.get(Unit.INPUT_TOKEN)
            output_price = m.prices.get(Unit.OUTPUT_TOKEN)
            if input_price is None or output_price is None:
                return None
            return input_price + 3 * output_price  # output-weighted: generation is what varies

        reverse = quality == "max"

        def sort_key(m: ModelSpec) -> tuple[int, Decimal]:
            price = blended(m)
            if price is None:
                return (1, Decimal(0))  # unpriced last, under BOTH dials
            return (0, -price if reverse else price)

        tail.sort(key=sort_key)
        candidates = head + tail
    return candidates


def _seats(
    model_class: ModelClass,
    catalog: list[ModelSpec],
    *,
    order: dict[str, list[str]] | None,
    quality: str | None,
) -> list[ModelSpec]:
    """The class's chain as rows this host can build."""
    return [
        spec
        for spec in chain_for(
            model_class, catalog, order=(order or {}).get(model_class.value), quality=quality
        )
        if _builds(spec)
    ]


def served_here(model_class: ModelClass, catalog: list[ModelSpec]) -> bool:
    """Whether a row this host can build serves the class: `resolve` would not refuse it."""
    return bool(_seats(model_class, catalog, order=None, quality=None))


def seatable(catalog: list[ModelSpec]) -> list[ModelSpec]:
    """The rows a fleet may seat, in catalog order: those serving an executor class that this host can build."""
    return [m for m in catalog if any(m.serves(c) for c in EXECUTOR_CLASSES) and _builds(m)]


@dataclass(frozen=True, slots=True)
class JudgeSeats:
    """The judge chain for one maker's work, and how it stands to the maker."""

    family: JudgeFamily
    rows: tuple[ModelSpec, ...]
    settings: ClassSettings = field(default_factory=ClassSettings)  # how the judge class is asked


def judge_seats(
    catalog: list[ModelSpec],
    *,
    maker_family: Family,
    maker_model: str,
    order: dict[str, list[str]] | None = None,
    settings: ClassSettings | None = None,
) -> JudgeSeats:
    """The judge rows this host can build, furthest from the maker first: another family, else another
    model of its family, else its own model (`maker_model` is its `provider:model_id` ref)."""
    rows = _seats(ModelClass.JUDGE, catalog, order=order, quality=None)
    for family in (JudgeFamily.OTHER, JudgeFamily.SAME, JudgeFamily.SELF):
        seats = tuple(
            s
            for s in rows
            if JudgeFamily.between(
                maker=maker_model, maker_family=maker_family, judge=s.ref, judge_family=s.family
            )
            is family
        )
        if seats:
            return JudgeSeats(family, seats, settings or ClassSettings())
    return JudgeSeats(JudgeFamily.NONE, (), settings or ClassSettings())


def _builds(spec: ModelSpec) -> bool:
    """Whether this host can build the row - `build_model`'s own answer, so no seat is offered that
    cannot serve (no key for it) and `resolve`'s skip has one home."""
    try:
        build_model(
            provider=spec.provider, model_id=spec.model_id, protocol=spec.protocol, base_url=spec.base_url
        )
    except (ProviderUnavailable, ModelMisconfigured):
        return False
    return True


def resolve(
    model_class: ModelClass,
    catalog: list[ModelSpec],
    *,
    emit_usage: EmitUsage | None = None,
    context: MeterContext | None = None,
    exclude_family: Family | None = None,
    order: dict[str, list[str]] | None = None,
    pin: str | None = None,
    quality: str | None = None,
) -> Model:
    """The one call; raises rather than serve what the caller did not ask for. `order` breaks the class's
    tie; a `pin` (fleet seat) bypasses the class filter and gets no fallback, but stays rate-limited."""
    from pydantic_ai.models.concurrency import ConcurrencyLimitedModel

    if pin is not None:
        specs = [m for m in catalog if m.ref == pin]
        if not specs:
            raise PolicyDenied(
                f"the plan pinned {pin!r} but no enabled catalog row has that ref - "
                "re-enable it or replan: the catalog is the roster"
            )
    else:
        specs = chain_for(
            model_class,
            catalog,
            order=(order or {}).get(model_class.value),
            exclude_family=exclude_family,
            quality=quality,
        )
        lower = DEGRADES_TO.get(model_class)
        if lower is not None and not any(_builds(spec) for spec in specs):
            logfire.warn(
                "no {cls!r} model can be built on this host; it runs on {lower!r}",
                cls=model_class.value,
                lower=lower.value,
            )
            return resolve(
                lower,
                catalog,
                emit_usage=emit_usage,
                context=context,
                exclude_family=exclude_family,
                order=order,
                quality=quality,
            )
    if not specs:
        raise PolicyDenied(
            f"no enabled model serves {model_class.value!r}"
            + (f" outside family {exclude_family!r}" if exclude_family else "")
            + ". Add one with `prismoid models add … --classes "
            + model_class.value
            + "` - it is a row, not a deploy."
        )

    # Cap what gets BUILT. Every candidate below is a provider object; at a seventy-row catalog
    # an uncapped chain builds dozens per step of which one answers. Four is a real chain (primary
    # + three understudies); an operator who explicitly chained more in the routing table gets
    # exactly what they wrote.
    chained = sum(1 for s in specs if s.ref in set((order or {}).get(model_class.value) or ()))
    cap = len(specs) if pin is not None else max(CHAIN_CAP, chained)

    ctx = context or MeterContext()
    candidates: list[Model] = []
    unbuilt: list[str] = []
    for spec in specs:
        if len(candidates) == cap:
            break
        try:
            built = build_model(
                provider=spec.provider, model_id=spec.model_id, protocol=spec.protocol, base_url=spec.base_url
            )
        except (ProviderUnavailable, ModelMisconfigured) as refused:
            # A row this host cannot build (no key for it: `models seed` enables every family it
            # ships) is a family down, and one family down must cost latency, never the answer -
            # the chain is the candidates that can be built.
            unbuilt.append(str(refused))
            continue
        # Limiter OUTSIDE the meter: queue wait must never sit inside a metered call (a future
        # duration column would bill waiting as thinking). Meter innermost keeps
        # each call's spend on the row that served it. Per-candidate inside the fallback: a fallback from A to B gates on B's
        # provider pool, never holding A's slot.
        candidates.append(
            ConcurrencyLimitedModel(
                MeteredModel(built, spec, emit_usage, ctx), throttle.limiter_for(spec.provider)
            )
        )
    if not candidates:
        raise PolicyDenied(f"no {model_class.value!r} model can be built on this host: {'; '.join(unbuilt)}")

    if len(candidates) == 1:
        return candidates[0]
    return StickyFallback(*candidates)


def head_row(model: Model) -> ModelSpec | None:
    """The row `model`'s next request goes to first (a sticky chain's current head); None when `model`
    was not built by `resolve()`."""
    from pydantic_ai.models.wrapper import WrapperModel

    while True:
        if isinstance(model, MeteredModel):
            return model.spec
        if isinstance(model, FallbackModel):
            model = model.models[0]
        elif isinstance(model, WrapperModel):
            model = model.wrapped
        else:
            return None


class StickyFallback(FallbackModel):
    """The chain, failing over only on `throttle.fails_over`, and staying on the row that answered
    after a failover: the rows it moved past go to the back, so a brown-out costs one request its waits."""

    def __init__(self, *models: Model) -> None:
        super().__init__(*models, fallback_on=throttle.fails_over)

    def _stay(self, tried: list[Model], failed: list[ModelRequestAttempt] | None) -> None:
        """Rotate `tried` (the order this request walked) so the row that answered leads."""
        hops = len(failed or ())
        if 0 < hops < len(tried):
            self.models = [*tried[hops:], *tried[:hops]]

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        tried = self.models
        response = await super().request(messages, model_settings, model_request_parameters)
        self._stay(tried, response.failed_attempts)
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: Any = None,
    ) -> AsyncGenerator[StreamedResponse]:
        tried = self.models
        async with super().request_stream(
            messages, model_settings, model_request_parameters, run_context
        ) as stream:
            self._stay(tried, stream.failed_attempts)
            yield stream
