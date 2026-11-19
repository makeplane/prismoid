# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The egress door (ADR-046): the pure refusal grammar both network doors share.

Microsecond tests, no sockets - parsing only. The connectors door's own cases stay in
`test_connector_oauth_urls.py` (its import path is unchanged, and that is itself asserted
here); these pin the shared primitives and the web door's temper.
"""

from __future__ import annotations

import ipaddress

import pytest

from prismoid.schemas.egress import (
    check_server_url,
    ip_literal,
    is_localhost,
    refuse_ip,
    strip_control,
    vet_fetch_url,
)

# Every spelling of "this machine / this network" a resolver would short-circuit. The list is
# the point: an SSRF guard that knows two spellings is a guard with a hole per libc quirk.
LOOPBACK_SPELLINGS = [
    "http://127.0.0.1/",
    "http://127.0.0.1./",
    "http://127.1/",
    "http://0x7f000001/",
    "http://2130706433/",
    "http://0177.0.0.1/",
    "https://[::1]/",
    "https://[::ffff:127.0.0.1]/",
    "http://localhost/",
]

INSIDE_THE_NETWORK = [
    "http://169.254.169.254/latest/meta-data/",  # the cloud metadata door, by name
    "http://10.0.0.7/",
    "http://192.168.1.1/admin",
    "http://172.16.0.9/",
    "https://[fd00::1]/",
    "https://[fe80::1]/",
    "http://0.0.0.0/",
    "http://224.0.0.1/",
    # RFC 6598 carrier-grade NAT: not private, not reserved, not link-local, not multicast,
    # not unspecified - so the hand-written enumeration let it through.
    # It is Tailscale's entire mesh and several clouds' internal endpoints.
    "http://100.64.0.1/",
    "http://100.127.255.254/",
    "https://[::ffff:100.64.0.1]/",  # …and the same range wearing the IPv4-mapped costume
    "http://198.18.0.1/",  # RFC 2544 benchmarking - likewise none of the five
    "http://192.0.0.171/",  # RFC 7335 IPv4 service continuity
]


@pytest.mark.parametrize("url", LOOPBACK_SPELLINGS + INSIDE_THE_NETWORK)
def test_the_web_door_refuses_every_inside_spelling(url: str) -> None:
    with pytest.raises(ValueError):
        vet_fetch_url(url)


def test_the_web_door_accepts_the_public_web() -> None:
    assert vet_fetch_url("https://example.com/a?b=c") == "https://example.com/a?b=c"
    assert vet_fetch_url("http://example.com/") == "http://example.com/"  # http vets; the tool upgrades
    assert vet_fetch_url("https://example.com:8443/x") == "https://example.com:8443/x"
    assert vet_fetch_url("https://93.184.216.34/x") == "https://93.184.216.34/x"  # a public literal is fine


def test_the_web_door_strips_the_fragment() -> None:
    # The fragment never crosses the wire, and the page cache keys on what was actually fetched.
    assert vet_fetch_url("https://example.com/doc#section-3") == "https://example.com/doc"


@pytest.mark.parametrize(
    ("url", "phrase"),
    [
        ("ftp://example.com/x", "only http"),
        ("https:///nohost", "fetched from"),
        ("https://user:pw@example.com/", "credentials"),
        ("https://1.2.3.4.5/", "not a valid web address"),
    ],
)
def test_the_web_door_names_its_reasons(url: str, phrase: str) -> None:
    with pytest.raises(ValueError, match=phrase):
        vet_fetch_url(url)


def test_dev_may_open_loopback_and_only_loopback() -> None:
    assert vet_fetch_url("http://localhost:8000/x", allow_insecure_localhost=True)
    assert vet_fetch_url("http://127.0.0.1:8000/x", allow_insecure_localhost=True)
    with pytest.raises(ValueError):  # private stays private even in dev
        vet_fetch_url("http://10.0.0.7/", allow_insecure_localhost=True)


@pytest.mark.parametrize(
    "addr",
    ["93.184.216.34", "8.8.8.8", "1.1.1.1", "2606:4700:4700::1111", "2a00:1450:4001:80e::200e"],
)
def test_the_public_internet_is_still_reachable(addr: str) -> None:
    """The other half of the widened rule: `is_global` must not refuse the actual web."""
    assert refuse_ip(ipaddress.ip_address(addr)) is None


def test_the_globally_routable_exceptions_stay_named() -> None:
    """Two ranges ARE `is_global` and still must not be dialed - so the union keeps naming them:
    IPv4 multicast, and the reserved space NAT64 translates through."""
    assert ipaddress.ip_address("224.0.0.1").is_global and refuse_ip(ipaddress.ip_address("224.0.0.1"))
    nat64 = ipaddress.ip_address("64:ff9b::a00:1")  # 10.0.0.1 on a NAT64 network
    assert nat64.is_global and refuse_ip(nat64)


def test_refuse_ip_unwraps_the_ipv4_mapped_costume() -> None:
    assert refuse_ip(ipaddress.ip_address("::ffff:10.0.0.1")) is not None
    assert refuse_ip(ipaddress.ip_address("::ffff:93.184.216.34")) is None
    assert refuse_ip(ipaddress.ip_address("127.0.0.1"), allow_loopback=True) is None
    assert refuse_ip(ipaddress.ip_address("127.0.0.1")) is not None


def test_ip_literal_knows_the_c_library_spellings() -> None:
    assert str(ip_literal("0x7f000001")) == "127.0.0.1"
    assert str(ip_literal("127.1")) == "127.0.0.1"
    assert ip_literal("example.com") is None
    assert is_localhost("LOCALHOST")


def test_the_connectors_door_still_answers_at_its_old_address() -> None:
    """The move re-exported, not relocated, the connectors' public API."""
    from prismoid.schemas.connectors import check_server_url as via_connectors

    assert via_connectors is check_server_url
    with pytest.raises(ValueError, match="dev-only"):
        check_server_url("https://127.0.0.1/mcp", allow_insecure_localhost=False)
    with pytest.raises(ValueError, match="must be https"):
        check_server_url("http://example.com/mcp", allow_insecure_localhost=True)


def test_strip_control_removes_what_prompts_must_not_carry() -> None:
    assert strip_control("a\x00b\x1bc\nd\te") == "abc\nd\te"  # newline and tab survive; C0 noise dies
