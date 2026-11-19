# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Per-provider discipline (ADR-024): shared limiters, retrying clients, the wrap order.

No network. Constructing providers and clients is offline; what these tests pin is the
*shape* - one limiter per provider, limiter outside meter inside fallback, the chain cap -
because every one of those is a property somebody could quietly lose in a refactor and
nothing else in the product would notice until a fleet run took a provider down.
"""

from __future__ import annotations

import httpx2
import pytest

from prismoid.ai.gateway import throttle
from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.resolve import CHAIN_CAP, resolve
from prismoid.conf import settings
from prismoid.schemas.model_class import Family, ModelClass


@pytest.fixture(autouse=True)
def fresh_registries() -> None:
    throttle.reset()


def spec(n: int, provider: str = "local") -> ModelSpec:
    return ModelSpec(
        provider=provider,
        model_id=f"m{n:02d}",
        family=Family(f"fam{n}"),
        protocol="openai_compatible",
        classes=(ModelClass.FAST_EXECUTOR,),
        base_url="http://localhost:11434/v1",
    )


# ── limiters ──────────────────────────────────────────────────────────────────────────────────


def test_one_limiter_per_provider_and_it_is_shared() -> None:
    """The judge queue, the step wave and the media tool must gate on the SAME object - a
    limiter minted per call site limits nothing."""
    a1 = throttle.limiter_for("anthropic")
    a2 = throttle.limiter_for("anthropic")
    b = throttle.limiter_for("openai")
    assert a1 is a2
    assert a1 is not b


def test_limits_come_from_conf_with_the_default_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.gateway, "provider_limits", {"default": 4, "anthropic": 12})
    assert throttle._limit_for("anthropic") == 12  # pyright: ignore[reportPrivateUsage]
    assert throttle._limit_for("nobody-heard-of") == 4  # pyright: ignore[reportPrivateUsage]


def test_limits_divide_by_replicas_and_floor_at_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The conf convention (ApiSettings.replica_count precedent): the fleet-wide ceiling stays
    the configured number when workers scale out - and never rounds to zero."""
    monkeypatch.setattr(settings.gateway, "provider_limits", {"default": 4, "local": 2})
    monkeypatch.setattr(settings.gateway, "replica_count", 3)
    assert throttle._limit_for("default-provider") == 1  # pyright: ignore[reportPrivateUsage]
    assert throttle._limit_for("local") == 1  # pyright: ignore[reportPrivateUsage]


# ── clients ───────────────────────────────────────────────────────────────────────────────────


def test_one_client_per_provider_with_the_retrying_transport() -> None:
    from pydantic_ai.retries import AsyncHTTPX2TenacityTransport

    c1 = throttle.client_for("anthropic")
    c2 = throttle.client_for("anthropic")
    assert c1 is c2, "a fresh client per call is a fresh TLS handshake per call - the bug this fixes"
    assert isinstance(c1._transport, AsyncHTTPX2TenacityTransport)  # pyright: ignore[reportPrivateUsage]


def test_only_retryable_statuses_raise_for_retry() -> None:
    """429/502/503/504 pause the SAME model (Retry-After honoured); a 400/401 is a fact about
    the request and must reach the caller immediately."""
    retryable = httpx2.Response(429, request=httpx2.Request("POST", "http://x"))
    with pytest.raises(httpx2.HTTPStatusError):
        throttle._validate(retryable)  # pyright: ignore[reportPrivateUsage]

    for status in (200, 400, 401, 404):
        throttle._validate(  # pyright: ignore[reportPrivateUsage]
            httpx2.Response(status, request=httpx2.Request("POST", "http://x"))
        )


@pytest.mark.parametrize("status", [500, 529])
async def test_an_overloaded_or_erroring_provider_is_retried_on_the_same_model(status: int) -> None:
    """A 529 (overloaded) or a 500 is the provider's weather, not a fact about the request: the
    SAME model is asked again (Retry-After honoured) before anything reaches the fallback chain.
    Once these burned a whole ladder rung - and a guarded step its model row - per blip."""
    sent: list[str] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(request.content.decode())
        if len(sent) == 1:
            return httpx2.Response(status, headers={"retry-after": "0"})
        return httpx2.Response(200, json={"ok": True})

    async with httpx2.AsyncClient(
        transport=throttle.retrying_transport(httpx2.MockTransport(answer))
    ) as client:
        response = await client.post("http://provider/v1/messages", content=b'{"model": "m"}')
    assert response.status_code == 200
    assert sent == ['{"model": "m"}', '{"model": "m"}'], "the same request, to the same model, twice"


async def test_a_request_fact_is_never_retried() -> None:
    """A 400 is about the request: retrying it three times only triples the log noise."""
    sent: list[int] = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        sent.append(1)
        return httpx2.Response(400, json={"error": "bad"})

    async with httpx2.AsyncClient(
        transport=throttle.retrying_transport(httpx2.MockTransport(answer))
    ) as client:
        response = await client.post("http://provider/v1/messages", content=b"{}")
    assert response.status_code == 400
    assert len(sent) == 1


# ── the wrap order (limiter outside meter, inside fallback) ───────────────────────────────────


def test_resolve_wraps_limiter_outside_meter_inside_fallback() -> None:
    """Limiter outside the meter: queue wait is never inside a metered call. Meter innermost:
    Rule 8's attribution. Per-candidate inside the fallback: a fallback to B gates on B's pool,
    never holding A's slot."""
    from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
    from pydantic_ai.models.fallback import FallbackModel

    from prismoid.ai.gateway.meter import MeteredModel

    chain = resolve(ModelClass.FAST_EXECUTOR, [spec(1), spec(2)])
    assert isinstance(chain, FallbackModel)
    for candidate in chain.models:
        assert isinstance(candidate, ConcurrencyLimitedModel)
        assert isinstance(candidate.wrapped, MeteredModel)


def test_a_single_candidate_is_still_limited_and_metered() -> None:
    from pydantic_ai.models.concurrency import ConcurrencyLimitedModel

    from prismoid.ai.gateway.meter import MeteredModel

    model = resolve(ModelClass.FAST_EXECUTOR, [spec(1)])
    assert isinstance(model, ConcurrencyLimitedModel)
    assert isinstance(model.wrapped, MeteredModel)


def test_a_fleet_pin_is_not_exempt_from_the_limiter() -> None:
    """Eleven seats on one provider still queue politely - a pin fixes the MODEL, not a
    right-of-way."""
    from pydantic_ai.models.concurrency import ConcurrencyLimitedModel

    pinned = resolve(ModelClass.FAST_EXECUTOR, [spec(1)], pin=spec(1).ref)
    assert isinstance(pinned, ConcurrencyLimitedModel)


# ── the chain cap ─────────────────────────────────────────────────────────────────────────────


def test_an_uncapped_catalog_builds_only_chain_cap_candidates() -> None:
    """Seventy enabled rows must never mean seventy constructed providers per step."""
    from pydantic_ai.models.fallback import FallbackModel

    chain = resolve(ModelClass.FAST_EXECUTOR, [spec(i) for i in range(7)])
    assert isinstance(chain, FallbackModel)
    assert len(chain.models) == CHAIN_CAP


def test_an_operator_chained_routing_table_beats_the_cap() -> None:
    """An explicit chain is operator intent: six chained refs build six candidates."""
    from pydantic_ai.models.fallback import FallbackModel

    catalog = [spec(i) for i in range(7)]
    order = {ModelClass.FAST_EXECUTOR.value: [s.ref for s in catalog[:6]]}
    chain = resolve(ModelClass.FAST_EXECUTOR, catalog, order=order)
    assert isinstance(chain, FallbackModel)
    assert len(chain.models) == 6


# ── per-class timeouts ────────────────────────────────────────────────────────────────────────


def test_the_request_wall_clock_follows_the_model_class() -> None:
    """A judge that takes 90 s is stuck; a reasoner that takes 200 s is thorough."""
    from prismoid.ai.gateway.providers import default_model_settings

    assert default_model_settings(ModelClass.JUDGE)["timeout"] == 90.0
    assert default_model_settings(ModelClass.FRONTIER_REASONER)["timeout"] == 300.0
    assert default_model_settings()["timeout"] == 300.0


# ── the quality dial ──────────────────────────────────────────────────────────────────────────


def priced(n: int, in_price: str, out_price: str, provider: str = "p") -> ModelSpec:
    from decimal import Decimal

    from prismoid.schemas.usage import Unit

    return ModelSpec(
        provider=provider,
        model_id=f"m{n:02d}",
        family=Family(f"fam{n}"),
        protocol="openai_compatible",
        classes=(ModelClass.FAST_EXECUTOR,),
        base_url="http://localhost:11434/v1",
        prices={Unit.INPUT_TOKEN: Decimal(in_price), Unit.OUTPUT_TOKEN: Decimal(out_price)},
    )


def test_economy_ranks_cheapest_first_and_max_ranks_dearest_first() -> None:
    from prismoid.ai.gateway.resolve import chain_for

    cheap, mid, dear = (
        priced(1, "0.000001", "0.000005"),
        priced(2, "0.000003", "0.00001"),
        priced(3, "0.000005", "0.000025"),
    )
    catalog = [dear, cheap, mid]

    economy = chain_for(ModelClass.FAST_EXECUTOR, catalog, quality="economy")
    assert [m.model_id for m in economy] == ["m01", "m02", "m03"]

    best = chain_for(ModelClass.FAST_EXECUTOR, catalog, quality="max")
    assert [m.model_id for m in best] == ["m03", "m02", "m01"]


def test_balanced_and_none_change_nothing() -> None:
    from prismoid.ai.gateway.resolve import chain_for

    catalog = [priced(3, "0.000005", "0.000025"), priced(1, "0.000001", "0.000005")]
    untouched = chain_for(ModelClass.FAST_EXECUTOR, catalog)
    assert chain_for(ModelClass.FAST_EXECUTOR, catalog, quality="balanced") == untouched
    assert chain_for(ModelClass.FAST_EXECUTOR, catalog, quality=None) == untouched


def test_an_explicitly_chained_ref_outranks_the_dial() -> None:
    """Operator intent beats the dial: the routing table's head never moves."""
    from prismoid.ai.gateway.resolve import chain_for

    cheap, dear = priced(1, "0.000001", "0.000005"), priced(3, "0.000005", "0.000025")
    chain = chain_for(ModelClass.FAST_EXECUTOR, [cheap, dear], order=[dear.ref], quality="economy")
    assert [m.model_id for m in chain] == ["m03", "m01"], "the chained dear model stays first"


def test_an_unpriced_model_ranks_last_under_both_dials() -> None:
    """A missing price must never be rewarded as 'free' - the enabled-but-unpriced hole."""
    from prismoid.ai.gateway.resolve import chain_for

    unpriced = ModelSpec(
        provider="p",
        model_id="m99",
        family=Family("fam99"),
        protocol="openai_compatible",
        classes=(ModelClass.FAST_EXECUTOR,),
        base_url="http://localhost:11434/v1",
    )
    catalog = [unpriced, priced(1, "0.000001", "0.000005")]
    assert chain_for(ModelClass.FAST_EXECUTOR, catalog, quality="economy")[-1].model_id == "m99"
    assert chain_for(ModelClass.FAST_EXECUTOR, catalog, quality="max")[-1].model_id == "m99"


async def test_a_stream_that_stops_early_gives_its_provider_slot_back() -> None:
    """A step that stops reading a stream (cancelled, steered, an error mid-answer) must hand its
    slot back, or a few such steps fill the provider's pool and every later request waits forever.
    pydantic-ai before 2.53 kept the slot when it was released on another task (GHSA-6fqq-452j-qhrp)."""
    import asyncio

    from pydantic_ai import Agent
    from pydantic_ai.concurrency import ConcurrencyLimiter
    from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
    from pydantic_ai.models.test import TestModel

    one_slot = ConcurrencyLimiter(1, name="gateway:test")
    agent = Agent(ConcurrencyLimitedModel(TestModel(custom_output_text="word " * 50), one_slot))

    async def stop_after_first_chunk() -> None:
        async with agent.run_stream("hi") as result:
            async for _ in result.stream_text(delta=True):
                break

    for _ in range(3):
        await asyncio.wait_for(stop_after_first_chunk(), timeout=5)
    await asyncio.wait_for(agent.run("hi"), timeout=5)
