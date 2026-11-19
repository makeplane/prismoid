# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Per-provider discipline - one limiter and one retrying client per provider, process-wide.

Without it every `resolve()` would build fresh SDK clients (a TLS handshake per candidate), nothing
would bound how many streams hit one provider at once, and a 429 would reach `FallbackModel`, which
handles it by silently switching to a different model: the answer's author changes because a rate
limiter blinked.

Two module-scope registries fix all three, and they are deliberately **process state, not rows**
(`ai/` stays persistence-free - an integer per provider is deployment tuning, not routing intent):

* `limiter_for(provider)` - a shared `ConcurrencyLimiter`. Wrapped OUTSIDE the meter so queue wait
  is never inside a metered call, and per-candidate INSIDE the fallback so each candidate gates on
  **its own provider's** pool - a fallback from A to B must not hold A's slot while B streams.
* `client_for(provider)` - a shared keep-alive `httpx2.AsyncClient` whose transport retries the
  SAME model on 429/500/502/503/504/529, honouring `Retry-After` (`wait_retry_after`), bounded at three
  attempts - and only THEN lets `ModelAPIError` reach `FallbackModel` and change models (a status
  the retries gave up on is a `ModelHTTPError` from every provider). Retrying the same model is a
  pause; switching models is a different answer. The order matters.

Limits are conf (`PRISMOID_GATEWAY_PROVIDER_LIMITS`, per process, ÷ `replica_count` - the same
convention as the API throttles).

**The retry policy, whole** (ADR-024). Between a step and a provider there are three tiers and no
others: (1) this transport retries the SAME model on `RETRYABLE_STATUSES`, `RETRY_ATTEMPTS` at most -
the SDKs' own retries are off (`SDK_MAX_RETRIES`); (2) the chain fails over to the next row only on
what `fails_over` says (no answer, a retryable status, a key that provider refuses, or a prompt too
long for that row - never another 400),
and a run that failed over stays on the row that answered (`resolve.StickyFallback`); (3) the step
ladder (`operations/execution`) decides another attempt from the death's `FailureKind`
(`schemas/failures.ADVICE`; `ai/context/retry.py` words the carried note). Worst case for one model
request: `RETRY_ATTEMPTS` × the chain's length upstream calls, and only the first request of a run
pays it - every later one starts on the row that answered.
"""

from __future__ import annotations

import time
from contextlib import AbstractAsyncContextManager
from typing import Final

import httpx2
import logfire
from pydantic_ai.concurrency import ConcurrencyLimiter

from prismoid.ai import config

# Statuses worth retrying the SAME model for: rate limits, a provider's own error (500), gateway
# hiccups, and Anthropic's "overloaded" (529). A 400/401/403 is a fact about the request, not the
# weather - retrying it three times just triples the log noise.
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504, 529})
RETRY_ATTEMPTS = 3
RETRY_MAX_WAIT_S = 60.0
# Every provider SDK is built with this retry budget: the transport above is the one retrier.
SDK_MAX_RETRIES: Final = 0
# A key refusal fails over too: it is a fact about that provider's account, and the next row may be
# another provider's key (`FailureKind.KEY_REFUSED` is "401/403 from every model tried").
_KEY_REFUSED: Final = frozenset({401, 403})

# A wait for a provider slot longer than this is said (provider, seconds, the queue behind it):
# queue time sits outside the meter by design, so without the line it is silent time.
SLOT_WAIT_LOG_S: Final = 5.0
# A provider's per-process stream ceiling when conf names neither it nor a `default`.
DEFAULT_PROVIDER_LIMIT: Final = 4
# The client's outer bounds: `ModelSettings.timeout` (`providers.CLASS_TIMEOUT_S`) is the real one.
CLIENT_TIMEOUT_S: Final = 600.0
CONNECT_TIMEOUT_S: Final = 10.0
# The connection pool's floor (twice the stream limit otherwise), so a low limit never starves the pool.
MIN_CONNECTIONS: Final = 8

_LIMITERS: dict[str, ConcurrencyLimiter] = {}
_CLIENTS: dict[str, httpx2.AsyncClient] = {}


def _limit_for(provider: str) -> int:
    """The per-process ceiling for one provider, from conf, ÷ replicas, floor 1."""
    gateway = config.current().gateway
    limits = gateway.provider_limits
    raw = limits.get(provider, limits.get("default", DEFAULT_PROVIDER_LIMIT))
    return max(1, raw // max(1, gateway.replica_count))


class _SaysItsWait(ConcurrencyLimiter):
    """The provider's limiter, which says a long wait for its slot (`SLOT_WAIT_LOG_S`)."""

    def __init__(self, provider: str, max_running: int) -> None:
        super().__init__(max_running, name=f"gateway:{provider}")
        self.provider = provider

    async def acquire(self, source: str) -> None:
        started = time.monotonic()
        await super().acquire(source)
        waited = time.monotonic() - started
        if waited >= SLOT_WAIT_LOG_S:
            logfire.warn(
                "gateway: waited {waited_s}s for a {provider} slot",
                provider=self.provider,
                waited_s=round(waited, 1),
                source=source,
                still_waiting=self.waiting_count,
                max_running=self.max_running,
            )


def limiter_for(provider: str) -> ConcurrencyLimiter:
    """The ONE limiter for this provider in this process. Every model call - step agents, the
    judge queue, light agents, the media tool - gates on the same object, which is the point."""
    limiter = _LIMITERS.get(provider)
    if limiter is None:
        limiter = _SaysItsWait(provider, _limit_for(provider))
        _LIMITERS[provider] = limiter
    return limiter


def throttled(provider: str) -> AbstractAsyncContextManager[None]:
    """The limiter as a context manager - for callers that are not pydantic-ai `Model`s (the
    media tool). Same pool as the chat lane: an image burst and a section wave share the ceiling."""
    from pydantic_ai.concurrency import get_concurrency_context

    return get_concurrency_context(limiter_for(provider), f"gateway:{provider}")


def fails_over(exc: Exception) -> bool:
    """Whether a failed model request moves the chain to its next row: no answer at all, a retryable
    status, a refused key, or a prompt past that row's context window (the next row may hold it); any
    other refusal of the request itself (400, 413, 422, ...) raises at once, so a bad schema never loops."""
    from pydantic_ai.exceptions import (
        ModelAPIError,
        ModelContextWindowExceededError,
        ModelHTTPError,
        ModelRequestRejectedError,
    )

    if isinstance(exc, ModelContextWindowExceededError):
        return True
    if isinstance(exc, ModelHTTPError):
        return exc.status_code in RETRYABLE_STATUSES or exc.status_code in _KEY_REFUSED
    return isinstance(exc, ModelAPIError) and not isinstance(exc, ModelRequestRejectedError)


def _validate(response: httpx2.Response) -> None:
    if response.status_code in RETRYABLE_STATUSES:
        response.raise_for_status()


def client_for(provider: str) -> httpx2.AsyncClient:
    """The ONE keep-alive client for this provider in this process.

    The transport retries retryable statuses on the same connection pool, waiting what
    `Retry-After` says (capped) or exponential backoff when the header is absent. Lives for the
    process. httpx2, not httpx: the provider SDKs (openai 3, anthropic 1, google-genai) and
    pydantic-ai's retrying transport speak it.
    """
    client = _CLIENTS.get(provider)
    if client is None:
        limit = _limit_for(provider)
        client = httpx2.AsyncClient(
            transport=retrying_transport(),
            timeout=httpx2.Timeout(CLIENT_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
            limits=httpx2.Limits(
                max_connections=max(limit * 2, MIN_CONNECTIONS), max_keepalive_connections=limit
            ),
        )
        _CLIENTS[provider] = client
    return client


def retrying_transport(wrapped: httpx2.AsyncBaseTransport | None = None) -> httpx2.AsyncBaseTransport:
    """Retry a `RETRYABLE_STATUSES` reply on the same model (`Retry-After`, capped, else backoff) at
    most `RETRY_ATTEMPTS` times over `wrapped` (the network by default); anything else passes at once."""
    from pydantic_ai.retries import AsyncHTTPX2TenacityTransport, RetryConfig, wait_retry_after
    from tenacity import retry_if_exception_type, stop_after_attempt

    return AsyncHTTPX2TenacityTransport(
        RetryConfig(
            retry=retry_if_exception_type(httpx2.HTTPStatusError),
            wait=wait_retry_after(max_wait=RETRY_MAX_WAIT_S),
            stop=stop_after_attempt(RETRY_ATTEMPTS),
            reraise=True,
        ),
        wrapped=wrapped,
        validate_response=_validate,
    )


def reset() -> None:
    """Testing seam: drop the registries so a test can change conf and observe fresh limits.
    Clients are not `aclose`d - the objects this replaces were never closed either, and tests
    never send bytes through them."""
    _LIMITERS.clear()
    _CLIENTS.clear()
