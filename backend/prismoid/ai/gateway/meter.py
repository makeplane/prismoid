# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`MeteredModel` - **every provider call emits a UsageEvent**, and nothing can slip past.

It is a `WrapperModel`, not a decorator on our call sites, and that distinction is the entire
design. A metering call that the *caller* has to remember is a metering call somebody forgets:
the retry path forgets it, the fallback path forgets it, the streaming path forgets it, and the
ledger quietly stops reconciling. Wrapping the `Model` itself means the meter sits under
pydantic-ai - under retries, under tool loops, under streaming - and **there is no code path from
an agent to a provider that does not pass through it.**

Native units in, `cost_usd` priced at write time, no merging: an "average token" that
blends an image, a second of audio and an input token is a number nobody can audit.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, cast

import httpx2
import logfire
from pydantic_ai.exceptions import ContentFilterError, ModelAPIError, ModelHTTPError
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel

from prismoid.ai.gateway.classes import ModelSpec
from prismoid.ai.gateway.providers import CACHE_KEY_SETTING, HOUR_TTL_SETTINGS
from prismoid.schemas.ids import ConversationId, ProjectId, StepId, TaskId
from prismoid.schemas.model_class import Family
from prismoid.schemas.usage import Unit, UsageRecord

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage, ModelResponse
    from pydantic_ai.settings import ModelSettings

# The usage emitter lives HERE, in the module that calls it - not in `deps`. `gateway` sits *below*
# `deps` in the engine contract, and `just layers` refused the upward import. Same lesson the
# tool journal taught, and the same better outcome: the meter needs one callable, not a
# dependency object it could reach anything through.
EmitUsage = Callable[[UsageRecord], Awaitable[None]]

# A request that answered only after this long says so: with a failure's line, it accounts for a
# step's silent minutes (a request that idled out, failed over, and idled out again logged nothing).
SLOW_REQUEST_S: Final = 60.0
# What a model request can die of on the wire - the errors a fallback hops on, and the raw stream
# errors an SDK lets out unwrapped (google-genai). Anything else (a stream consumer's own error) is
# not the provider's, and is not said here.
_PROVIDER_ERRORS: Final = (ModelAPIError, ContentFilterError, httpx2.HTTPError)
# The flex tier's twin of each unit it discounts: a row that prices every twin of what it prices may be
# sent `service_tier: flex`, and a response served flex bills these (`MeteredModel._for_row`, `units_of`).
_FLEX: Final = {
    Unit.INPUT_TOKEN: Unit.FLEX_INPUT_TOKEN,
    Unit.OUTPUT_TOKEN: Unit.FLEX_OUTPUT_TOKEN,
    Unit.CACHE_READ_TOKEN: Unit.FLEX_CACHE_READ_TOKEN,
}


def failed_error(exc: BaseException) -> str:
    """What a failure line says of the error itself: its type, and its HTTP status when it has
    one. Never its text - a provider's error body can echo the key it was sent."""
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__} {status}" if status is not None else type(exc).__name__


def failure_reason(exc: BaseException) -> str:
    """Why a model request failed, in one word a log can be filtered by. `idle_timeout`: no byte
    for the request's timeout (`providers.CLASS_TIMEOUT_S` - an idle bound, not a wall clock)."""
    if isinstance(exc, ContentFilterError):
        return "content_filter"
    if isinstance(exc, ModelHTTPError):
        if exc.status_code == 429:
            return "rate_limited"
        if exc.status_code in (400, 413):
            return "refused"
        return "provider_error" if exc.status_code >= 500 else f"http_{exc.status_code}"
    cause: BaseException | None = exc
    while cause is not None:  # the SDK's error wraps the transport's: the cause names the timeout
        if isinstance(cause, httpx2.ReadTimeout):
            return "idle_timeout"
        if isinstance(cause, httpx2.TimeoutException):
            return "timeout"
        cause = cause.__cause__ or cause.__context__
    return "connection"


def units_of(usage: object, *, flex: bool = False) -> dict[Unit, int]:
    """pydantic-ai usage -> native units, one-for-one, never summed (cached input is its own unit); audio
    and reasoning counts sit inside those totals, so map REASONING_TOKEN only together with its price."""
    counts = {
        name: int(getattr(usage, name, 0) or 0)
        for name in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    }
    # `input_tokens` is the WHOLE prompt - pydantic-ai folds cache reads and cache writes into it,
    # for every provider - so the input rate bills only the uncached remainder. Mapping the total
    # billed every cached token twice: once at the input rate, again at its cache rate.
    uncached = max(counts["input_tokens"] - counts["cache_read_tokens"] - counts["cache_write_tokens"], 0)
    # Anthropic reports the 1-hour slice of the cache writes apart (`details`); it bills at its own rate.
    details: dict[str, int] = getattr(usage, "details", None) or {}
    hour = min(details.get("ephemeral_1h_input_tokens", 0), counts["cache_write_tokens"])
    out: dict[Unit, int] = {}
    for unit, value in (
        (Unit.INPUT_TOKEN, uncached),
        (Unit.OUTPUT_TOKEN, counts["output_tokens"]),
        (Unit.CACHE_READ_TOKEN, counts["cache_read_tokens"]),
        (Unit.CACHE_WRITE_TOKEN, counts["cache_write_tokens"] - hour),
        (Unit.CACHE_WRITE_1H_TOKEN, hour),
    ):
        if value:
            out[_FLEX.get(unit, unit) if flex else unit] = value
    out[Unit.REQUEST] = 1
    return out


def flex_priced(spec: ModelSpec) -> bool:
    """Does this row price the flex tier, every twin of the token units it prices?"""
    priced = [unit for unit in _FLEX if unit in spec.prices]
    return bool(priced) and all(_FLEX[unit] in spec.prices for unit in priced)


def served_flex(details: dict[str, Any] | None, settings: ModelSettings | None) -> bool:
    """Was this call served on the flex tier: the tier the response reports, else the one asked for
    (OpenAI and Gemini serve flex or refuse it, never quietly upgrade)."""
    tier = (details or {}).get("service_tier") or (settings or {}).get("service_tier")
    return tier == "flex"


@dataclass(frozen=True, slots=True)
class MeterContext:
    """Who to bill. Carried into the wrapper so the ledger row is attributed at time of spend."""

    task_id: TaskId | None = None
    step_id: StepId | None = None
    project_id: ProjectId | None = None
    conversation_id: ConversationId | None = None  # a chat reply's spend - no task, no step


async def record_units(
    spec: ModelSpec, units: dict[Unit, int], emit: EmitUsage | None, ctx: MeterContext
) -> None:
    """The ONE UsageRecord composer - chat calls and media calls price through the same lines.

    `MeteredModel` delegates here; the media lane's `units_emitter` binds here. Two composers
    would be two places for the price_version/attribution/cost logic to drift apart, and the
    ledger is the one table where drift is money.
    """
    if emit is None:
        return
    await emit(
        UsageRecord(
            task_id=ctx.task_id,
            step_id=ctx.step_id,
            project_id=ctx.project_id,
            conversation_id=ctx.conversation_id,
            provider=spec.provider,
            model=spec.model_id,
            family=spec.family,
            units=units,
            cost_usd=spec.cost(units),
            price_version=spec.price_version,
            at=dt.datetime.now(dt.UTC),
        )
    )


def units_emitter(
    spec: ModelSpec, emit: EmitUsage | None, ctx: MeterContext
) -> Callable[[dict[str, int]], Awaitable[None]]:
    """A per-candidate emitter for a NON-chat provider call (the media lane).

    The tool layer sits BESIDE the gateway in the engine contract and cannot import `ModelSpec`
    - so `build()` binds one of these per image candidate and hands the tool a plain callable
    taking `{"image": 2, "request": 1}`. Every provider call still emits a UsageEvent: native units in,
    priced at write time, attributed to whoever actually generated."""

    async def _emit(units: dict[str, int]) -> None:
        await record_units(spec, {Unit(name): count for name, count in units.items()}, emit, ctx)

    return _emit


def flat_emitter(
    *,
    provider: str,
    item: str,
    family: str,
    unit: Unit,
    unit_price_usd: Decimal,
    emit: EmitUsage | None,
    ctx: MeterContext,
) -> Callable[[int], Awaitable[None]]:
    """The flat-priced sibling of `units_emitter` - for a provider that is not a model at all
    (the search lane: an engine bills per query, priced from conf, no catalog spec).

    Composed HERE beside `record_units` so the ledger's shape stays owned by one module - the
    same drift argument, honored rather than bent. `price_version=0` is the recorded marker for
    "conf-priced, not PriceBook" (PriceBook versions start at 1); repricing an engine is an env
    change, and old rows keep the price they were written at, exactly like models."""

    async def _emit(count: int) -> None:
        if emit is None or count <= 0:
            return
        await emit(
            UsageRecord(
                task_id=ctx.task_id,
                step_id=ctx.step_id,
                project_id=ctx.project_id,
                conversation_id=ctx.conversation_id,
                provider=provider,
                model=item,
                family=Family(family),
                units={unit: count, Unit.REQUEST: 1},
                cost_usd=(unit_price_usd * count).quantize(Decimal("0.000001")),
                price_version=0,
                at=dt.datetime.now(dt.UTC),
            )
        )

    return _emit


class MeteredModel(WrapperModel):
    """Wraps one model. Emits one `UsageRecord` per provider call."""

    def __init__(self, wrapped: Model, spec: ModelSpec, emit: EmitUsage | None, ctx: MeterContext) -> None:
        super().__init__(wrapped)
        self.spec = spec
        self.emit = emit
        self.ctx = ctx

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        started = time.monotonic()
        model_settings = self._for_row(model_settings)
        try:
            response = await super().request(messages, model_settings, model_request_parameters)
        except _PROVIDER_ERRORS as exc:
            self._failed(exc, started)
            raise
        self._answered(started)
        await self._record(response.usage, flex=served_flex(response.provider_details, model_settings))
        return response

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: Any = None,
    ) -> AsyncGenerator[StreamedResponse]:
        started = time.monotonic()
        model_settings = self._for_row(model_settings)
        try:
            async with super().request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as stream:
                try:
                    yield stream
                finally:
                    # AFTER the stream drains - or is CUT (a wall clock's cancellation, a caller's
                    # error): the tokens streamed so far were billed, so they are recorded, so a
                    # timed-out reply still lands a UsageEvent. Usage is only
                    # complete at the end - which is exactly why a caller-side meter forgets the
                    # streaming path, and exactly why this is a wrapper.
                    await self._record(
                        stream.usage, flex=served_flex(stream.provider_details, model_settings)
                    )
        except _PROVIDER_ERRORS as exc:
            self._failed(exc, started)
            raise
        self._answered(started)

    def _for_row(self, settings: ModelSettings | None) -> ModelSettings | None:
        """The shared settings made this row's: the cache key keyed on its model too, and the 1-hour
        cache and the flex tier only on a row that prices them (never an unbilled hour or discount)."""
        if settings is None:
            return None
        hour = Unit.CACHE_WRITE_1H_TOKEN in self.spec.prices
        out = {
            k: "5m" if k in HOUR_TTL_SETTINGS and v == "1h" and not hour else v for k, v in settings.items()
        }
        if out.get("service_tier") == "flex" and not flex_priced(self.spec):
            del out["service_tier"]
        if (prefix := out.get(CACHE_KEY_SETTING)) is not None:
            out[CACHE_KEY_SETTING] = (
                "prismoid-" + hashlib.sha256(f"{self.spec.ref}:{prefix}".encode()).hexdigest()[:32]
            )
        return cast("ModelSettings", out)

    def _failed(self, exc: BaseException, started: float) -> None:
        """One line per failed request - a fallback hop is one of these - so a step's silent
        minutes have a row, a reason and a duration in the worker log and the trace."""
        logfire.warn(
            "gateway: {ref} failed ({reason}) after {elapsed_s}s",
            ref=self.spec.ref,
            reason=failure_reason(exc),
            elapsed_s=round(time.monotonic() - started, 1),
            error=failed_error(exc),
            task_id=str(self.ctx.task_id or ""),
            step_id=str(self.ctx.step_id or ""),
        )

    def _answered(self, started: float) -> None:
        elapsed = time.monotonic() - started
        if elapsed >= SLOW_REQUEST_S:
            logfire.info(
                "gateway: {ref} answered after {elapsed_s}s",
                ref=self.spec.ref,
                elapsed_s=round(elapsed, 1),
                task_id=str(self.ctx.task_id or ""),
                step_id=str(self.ctx.step_id or ""),
            )

    async def _record(self, usage: object, *, flex: bool) -> None:
        await record_units(self.spec, units_of(usage, flex=flex), self.emit, self.ctx)
