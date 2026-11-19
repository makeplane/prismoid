# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Resolve and pin: how a worker- or API-process tool dials a name somebody else chose.

A name vetted at parse time and then handed to the resolver again at connect time is the
DNS-rebinding gap: the second answer is the one dialed. So the name is resolved ONCE per request,
every answer goes through `schemas/egress.refuse_ip`, and the connection is made to the vetted
address itself, the name riding the Host header and the TLS SNI (so the certificate is still
proved for the name). `web.fetch` walks its own redirects with these helpers; the
connectors door mounts `PinnedTransport` under
every MCP and OAuth client, so each request a client makes, a followed redirect included, is
re-resolved and re-vetted.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

import httpx

from prismoid.schemas.egress import ip_literal, is_localhost, refuse_ip

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]

# What one client reads from a server somebody else chose, per response and over the client's whole
# life (a paginated listing is many responses). Anyone can point a connector at a server they run,
# and its answers are read in the shared API and worker processes, so past either the server is cut
# off as unreachable instead of being buffered.
RESPONSE_MAX_BYTES: Final = 16 * 1024 * 1024
CLIENT_MAX_BYTES: Final = 128 * 1024 * 1024


async def default_resolver(host: str, port: int) -> Sequence[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


class PinRefused(Exception):
    """The name must not be dialed. `str()` names the answer that refused it (the fetch tool
    shows the model which address it hit); `public` leaves the address out, for a message a
    person reads back off a row, where an internal name's address is reconnaissance."""

    def __init__(self, host: str, reason: str, answer: str = "") -> None:
        self.host, self.reason, self.answer = host, reason, answer
        shown = f"{host} resolves to {answer}, which {reason}" if answer else f"{host} {reason}"
        super().__init__(shown)

    @property
    def public(self) -> str:
        return f"{self.host} is not an address this server will dial: it {self.reason}"


async def resolve_pinned(host: str, port: int, *, allow_loopback: bool, resolver: Resolver) -> str:
    """The first address `resolve_pinned_all` vetted: the one a single-address caller dials."""
    return (await resolve_pinned_all(host, port, allow_loopback=allow_loopback, resolver=resolver))[0]


async def resolve_pinned_all(host: str, port: int, *, allow_loopback: bool, resolver: Resolver) -> list[str]:
    """Resolve, classify EVERY answer, and return the vetted addresses in resolver order.

    All-answers, not first-good: an attacker who mixes one public A record with one private one
    controls which the client dials on retry. And the returned address is what actually gets
    connected to - resolving to vet and then dialing the *name* again is the rebinding TOCTOU
    this function exists to close."""
    literal = ip_literal(host)
    if literal is not None:
        reason = refuse_ip(literal, allow_loopback=allow_loopback)
        if reason is not None:
            raise PinRefused(host, reason)
        return [str(literal)]
    try:
        answers = list(dict.fromkeys(await resolver(host, port)))
    except OSError as exc:  # gaierror ⊂ OSError: the name does not resolve
        raise PinRefused(host, f"does not resolve ({exc})") from exc
    if not answers:
        raise PinRefused(host, "does not resolve")
    for raw in answers:
        addr = ipaddress.ip_address(raw.split("%")[0])  # scope ids off link-local IPv6
        reason = refuse_ip(addr, allow_loopback=allow_loopback)
        if reason is not None:
            raise PinRefused(host, reason, raw)
    return answers


@dataclass(slots=True)
class _Ledger:
    left: int  # bytes this client may still read, over every response


class _Capped(httpx.AsyncByteStream):
    """A response body that stops at its cap: past it, or past the client's ledger, the read fails
    as a transport error, so the caller's own handling sees a server that went away."""

    def __init__(
        self, inner: httpx.AsyncByteStream, request: httpx.Request, cap: int, ledger: _Ledger
    ) -> None:
        self._inner, self._request, self._cap, self._ledger = inner, request, cap, ledger

    async def __aiter__(self) -> AsyncIterator[bytes]:
        seen = 0
        async for chunk in self._inner:
            seen += len(chunk)
            self._ledger.left -= len(chunk)
            if seen > self._cap or self._ledger.left < 0:
                raise httpx.ReadError(
                    f"{self._request.url.host} sent more than a client reads", request=self._request
                )
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class PinnedTransport(httpx.AsyncBaseTransport):
    """An httpx transport that pins every request it carries: https only (plain http only to a
    loopback name, and only where loopback is allowed), the host resolved and every answer
    vetted, the request sent to the vetted address with the name on Host and SNI. A refusal is an
    `httpx.ConnectError` carrying `PinRefused.public`, so a client's own error handling sees an
    unreachable server.

    One connection pool per hostname: httpcore keys its pool on the ADDRESS after pinning, and
    the certificate was proved for the SNI name, which the key ignores - two names on one address
    must never share a connection proved for the other.

    Bounded: the request asks for an unencoded body and a coded one is refused (the client would
    inflate it without a limit), and a body is read only to `max_response_bytes`, and the client's
    bodies together only to `max_client_bytes`."""

    def __init__(
        self,
        *,
        allow_loopback: bool,
        resolver: Resolver | None = None,
        inner: Callable[[], httpx.AsyncBaseTransport] = httpx.AsyncHTTPTransport,
        max_response_bytes: int = RESPONSE_MAX_BYTES,
        max_client_bytes: int = CLIENT_MAX_BYTES,
    ) -> None:
        self._allow_loopback = allow_loopback
        # Looked up when the transport is made, not when this module was: the tests' DNS seam.
        self._resolver = resolver or default_resolver
        self._inner = inner
        self._pools: dict[str, httpx.AsyncBaseTransport] = {}
        self._max_response = max_response_bytes
        self._ledger = _Ledger(max_client_bytes)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        host = url.host
        loopback_name = self._allow_loopback and is_localhost(host)
        if url.scheme != "https" and not (url.scheme == "http" and loopback_name):
            raise httpx.ConnectError(f"{host}: only https servers are dialed", request=request)
        port = url.port or (443 if url.scheme == "https" else 80)
        try:
            addresses = await resolve_pinned_all(
                host, port, allow_loopback=self._allow_loopback, resolver=self._resolver
            )
        except PinRefused as refused:
            raise httpx.ConnectError(refused.public, request=request) from None
        pool = self._pools.get(host)
        if pool is None:
            pool = self._pools[host] = self._inner()
        # Every answer was vetted, so any of them may be dialed: one that does not answer falls
        # through to the next, as a plain client does (macOS lists ::1 before 127.0.0.1 for
        # localhost, and a server bound to one family is still reached).
        for pinned in addresses[:-1]:
            with contextlib.suppress(httpx.ConnectError):
                return await self._send(pool, request, pinned)
        return await self._send(pool, request, addresses[-1])

    async def _send(
        self, pool: httpx.AsyncBaseTransport, request: httpx.Request, pinned: str
    ) -> httpx.Response:
        url = request.url
        host = url.host
        # A copy goes to the address; the caller's request keeps naming the host. httpx hands that
        # request back as `response.request`, and a client following a redirect resolves a
        # relative Location against it and checks the hop's origin against it: rewritten in place,
        # the hop would be dialed at the address with the address as its SNI, and an absolute
        # same-origin Location would look like it left the origin.
        extensions = dict(request.extensions)
        if url.scheme == "https":
            extensions["sni_hostname"] = host
        headers = request.headers.copy()
        headers["Accept-Encoding"] = "identity"
        pinned_request = httpx.Request(
            request.method,
            url.copy_with(host=pinned),
            headers=headers,
            stream=request.stream,
            extensions=extensions,
        )
        response = await pool.handle_async_request(pinned_request)
        response.request = request
        refusal = self._refusal(response)
        if refusal:
            await response.aclose()
            raise httpx.ReadError(f"{host} {refusal}", request=request)
        response.stream = _Capped(
            cast(httpx.AsyncByteStream, response.stream), request, self._max_response, self._ledger
        )
        return response

    def _refusal(self, response: httpx.Response) -> str:
        """Why a response is not read at all, or "" to read it under the caps."""
        coding = response.headers.get("content-encoding", "").strip().lower()
        if coding not in ("", "identity"):
            return f"sent a {coding}-encoded body after an unencoded one was asked for"
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_response:
            return f"declared a {declared}-byte body, over the {self._max_response} a client reads"
        return ""

    async def aclose(self) -> None:
        pools, self._pools = list(self._pools.values()), {}
        for pool in pools:
            await pool.aclose()


def pinned_client(
    *,
    allow_loopback: bool,
    resolver: Resolver | None = None,
    headers: dict[str, str] | None = None,
    timeout: httpx.Timeout | None = None,
    auth: httpx.Auth | None = None,
    max_response_bytes: int = RESPONSE_MAX_BYTES,
    **_ignored: Any,
) -> httpx.AsyncClient:
    """An `httpx.AsyncClient` over `PinnedTransport`. `follow_redirects` is off (a caller that
    follows does it hop by hop, and every hop comes back through the transport) and
    `trust_env` is off, so no ambient proxy variable decides where a vetted connection goes.
    Extra keywords an MCP client factory is called with (`follow_redirects`) are ignored."""
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout or httpx.Timeout(30.0, read=300.0),
        auth=auth,
        follow_redirects=False,
        trust_env=False,
        transport=PinnedTransport(
            allow_loopback=allow_loopback, resolver=resolver, max_response_bytes=max_response_bytes
        ),
    )
