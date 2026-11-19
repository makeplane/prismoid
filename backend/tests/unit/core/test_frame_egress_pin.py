# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The dev egress proxy resolves and refuses inward answers before it dials."""

from __future__ import annotations

import pytest

from prismoid import egress_relay  # the Docker proxy's resolve-and-vet, shared with the device's


async def test_a_loopback_answer_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_getaddrinfo(host: str, port: int, **_: object):
        return [(None, None, None, None, ("127.0.0.1", 443))]

    monkeypatch.setattr(egress_relay, "_getaddrinfo", fake_getaddrinfo)
    assert await egress_relay.resolve_pinned("api.internal.example") == (None, "private address")


async def test_a_metadata_answer_is_refused_even_beside_a_public_one(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_getaddrinfo(host: str, port: int, **_: object):
        return [
            (None, None, None, None, ("93.184.216.34", 443)),
            (None, None, None, None, ("169.254.169.254", 443)),
        ]

    monkeypatch.setattr(egress_relay, "_getaddrinfo", fake_getaddrinfo)
    # one bad answer poisons the name
    assert await egress_relay.resolve_pinned("mixed.example") == (None, "private address")


async def test_a_public_answer_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_getaddrinfo(host: str, port: int, **_: object):
        return [(None, None, None, None, ("93.184.216.34", 443))]

    monkeypatch.setattr(egress_relay, "_getaddrinfo", fake_getaddrinfo)
    assert await egress_relay.resolve_pinned("example.com") == ("93.184.216.34", None)


@pytest.mark.parametrize("ip", ["10.0.0.5", "192.168.1.1", "172.16.0.9", "100.64.0.1", "::1", "fe80::1"])
async def test_every_private_range_is_refused(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    async def fake_getaddrinfo(host: str, port: int, **_: object):
        return [(None, None, None, None, (ip, 443))]

    monkeypatch.setattr(egress_relay, "_getaddrinfo", fake_getaddrinfo)
    assert await egress_relay.resolve_pinned("h.example") == (None, "private address")


async def test_a_dns_failure_is_journaled_as_unresolvable_not_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NXDOMAIN and "nothing resolved" are not the same failure as a refused inward answer - a
    caller reading the denial reason should not be told the resolver found something private
    when it found nothing at all."""

    async def fake_getaddrinfo(host: str, port: int, **_: object):
        raise OSError("Name or service not known")

    monkeypatch.setattr(egress_relay, "_getaddrinfo", fake_getaddrinfo)
    assert await egress_relay.resolve_pinned("nxdomain.example") == (None, "unresolvable")


async def test_an_empty_answer_is_journaled_as_unresolvable(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_getaddrinfo(host: str, port: int, **_: object):
        return []

    monkeypatch.setattr(egress_relay, "_getaddrinfo", fake_getaddrinfo)
    assert await egress_relay.resolve_pinned("empty.example") == (None, "unresolvable")


async def test_resolve_and_vet_returns_every_vetted_literal_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dial retry (both proxies) needs every vetted answer, not just the first, so an
    unroutable first literal does not fail a dial a later one would have completed."""

    async def fake_getaddrinfo(host: str, port: int, **_: object):
        return [(None, None, None, None, ("93.184.216.34", 443)), (None, None, None, None, ("1.1.1.1", 443))]

    monkeypatch.setattr(egress_relay, "_getaddrinfo", fake_getaddrinfo)
    literals, reason = await egress_relay._resolve_and_vet("multi.example", 443)  # pyright: ignore[reportPrivateUsage]
    assert literals == ["93.184.216.34", "1.1.1.1"]
    assert reason is None
