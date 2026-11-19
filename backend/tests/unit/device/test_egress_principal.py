# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One relay, two keys: the Docker proxy keys a frame by its source IP;
the device helper's proxy by the frame's own credential, because every device frame is 127.0.0.1."""

from __future__ import annotations

import asyncio
import base64
import logging
import ssl
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from prismoid import egress_relay
from prismoid.ai.frames import egress
from prismoid.device import egress as device_egress
from prismoid.device.egress import DeviceEgress, apply_allow
from prismoid.device.fence_data import REGISTRIES
from prismoid.device.frames import DeviceFrameState, FrameRegistry
from prismoid.schemas.device import FrameSpecIn
from prismoid.schemas.machines import FrameKind


async def _ask(port: int, target: str, auth: str | None = None) -> bytes:
    """One CONNECT through the proxy at `port`; the proxy's whole answer."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    head = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n"
    if auth is not None:
        head += f"Proxy-Authorization: Basic {base64.b64encode(auth.encode()).decode()}\r\n"
    writer.write((head + "\r\n").encode())
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    return answer


@pytest.fixture(autouse=True)
def _clean() -> None:  # pyright: ignore[reportUnusedFunction] - autouse: pytest calls it, not us
    egress._ALLOW.clear()  # pyright: ignore[reportPrivateUsage]
    egress._DENIALS.clear()  # pyright: ignore[reportPrivateUsage]


async def test_principal_hook_defaults_to_source_ip() -> None:
    """The Docker path is unchanged: the source IP is the key, and a denial carries it."""
    egress._ALLOW["127.0.0.1"] = {"allowed.example"}  # pyright: ignore[reportPrivateUsage]
    server = await asyncio.start_server(egress.handle_proxy, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        answer = await _ask(port, "evil.example:443", auth="device-t1:whatever")
    assert answer.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    assert b'"host": "evil.example"' in answer
    assert egress._DENIALS == [  # pyright: ignore[reportPrivateUsage]
        {"ip": "127.0.0.1", "host": "evil.example", "reason": "not in allowlist ['allowed.example']"}
    ]


async def test_the_relay_keys_a_frame_by_whatever_principal_it_is_given() -> None:
    denied: list[str] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await egress_relay.relay(
            reader,
            writer,
            principal_of=lambda _w, _auth: "frame-a",
            allowed_for=lambda principal: {"allowed.example"} if principal == "frame-a" else set(),
            on_deny=lambda principal, _host, _reason: denied.append(principal),
        )

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        await _ask(server.sockets[0].getsockname()[1], "evil.example:443")
    assert denied == ["frame-a"]


@pytest.fixture
async def device(tmp_path: Path) -> AsyncIterator[tuple[DeviceEgress, FrameRegistry]]:
    registry = FrameRegistry(tmp_path / "home")
    proxy = DeviceEgress(registry.frames)
    await proxy.serve()
    try:
        yield proxy, registry
    finally:
        await proxy.close()


def _make(registry: FrameRegistry, task_id: str, allow: list[str] | None = None) -> DeviceFrameState:
    spec = FrameSpecIn(task_id=task_id, kind=FrameKind.MACHINE, allow_domains=allow or [])
    return registry.create(spec)


def test_frame_list_starts_with_registries(tmp_path: Path) -> None:
    frame = _make(FrameRegistry(tmp_path / "home"), "t1", ["example.com"])
    assert frame.allow == {*REGISTRIES, "example.com"}
    status, _ = apply_allow(frame, ["docs.example"], 2)
    assert status == 200 and frame.allow == {*REGISTRIES, "docs.example"}
    status, body = apply_allow(frame, ["late.example"], 2)
    assert status == 409 and body == {"ok": False, "error": "stale_version", "version": 2}
    assert apply_allow(frame, ["x.example"], None)[0] == 200 and frame.egress_version == 2


async def test_device_proxy_refuses_host_not_in_frame_list(
    device: tuple[DeviceEgress, FrameRegistry],
) -> None:
    proxy, registry = device
    frame = _make(registry, "t1")
    answer = await _ask(proxy.port, "evil.example:443", auth=f"{frame.ref}:{frame.token}")
    assert answer.startswith(b"HTTP/1.1 403 Forbidden\r\n")
    assert list(frame.denials) == [
        {"ref": frame.ref, "host": "evil.example", "reason": f"not in allowlist {sorted(frame.allow)}"}
    ]


async def test_device_proxy_passes_a_listed_host_to_the_vet(
    device: tuple[DeviceEgress, FrameRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A listed host clears the list and meets the shared resolve-and-vet (here: an inward answer)."""

    async def inward(host: str, port: int, **_: object):
        return [(None, None, None, None, ("127.0.0.1", port))]

    monkeypatch.setattr(egress_relay, "_getaddrinfo", inward)
    proxy, registry = device
    frame = _make(registry, "t1")
    await _ask(proxy.port, "pypi.org:443", auth=f"{frame.ref}:{frame.token}")
    assert [(d["host"], d["reason"]) for d in frame.denials] == [("pypi.org", "private address")]


async def test_a_wrong_or_missing_credential_reaches_nothing(
    device: tuple[DeviceEgress, FrameRegistry],
) -> None:
    proxy, registry = device
    a, b = _make(registry, "a", ["a.example"]), _make(registry, "b")
    for auth in (f"{a.ref}:{b.token}", f"{a.ref}:", "chrome:nope", f"{a.ref}"):
        answer = await _ask(proxy.port, "a.example:443", auth=auth)
        assert answer.startswith(b"HTTP/1.1 403 Forbidden\r\n")
        assert b"no allowlist registered" in answer
    assert not a.denials and not b.denials


async def test_no_credential_is_challenged(device: tuple[DeviceEgress, FrameRegistry]) -> None:
    """git's libcurl sends the proxy credential only after a 407; the Docker path never asks."""
    proxy, registry = device
    _make(registry, "a", ["a.example"])
    answer = await _ask(proxy.port, "a.example:443")
    assert answer.startswith(b"HTTP/1.1 407 Proxy Authentication Required\r\n")
    assert b'Proxy-Authenticate: Basic realm="prismoid"' in answer


async def test_chrome_is_checked_against_the_union_of_live_frames(
    device: tuple[DeviceEgress, FrameRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    proxy, registry = device
    _make(registry, "a", ["a.example"])
    _make(registry, "b", ["b.example"])
    assert proxy.allowed_for("chrome") == {*REGISTRIES, "a.example", "b.example", *proxy.base}
    registry.destroy("device-b")
    assert "b.example" not in proxy.allowed_for("chrome")
    answer = await _ask(proxy.port, "b.example:443", auth=f"chrome:{proxy.chrome_token}")
    assert answer.startswith(b"HTTP/1.1 403 Forbidden\r\n") and b"not in allowlist" in answer


def _hello(alpn: list[str]) -> bytes:
    """A real ClientHello from Python's TLS, offering `alpn`."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    if alpn:
        context.set_alpn_protocols(alpn)
    outgoing = ssl.MemoryBIO()
    tls = context.wrap_bio(ssl.MemoryBIO(), outgoing, server_hostname="cdn.example")
    with pytest.raises(ssl.SSLWantReadError):
        tls.do_handshake()
    return outgoing.read()


@pytest.mark.parametrize(
    ("alpn", "offered"),
    [(["h2", "http/1.1"], True), (["http/1.1", "h2"], True), (["http/1.1"], False), ([], False)],
)
def test_offers_h2_reads_a_real_client_hello(alpn: list[str], offered: bool) -> None:
    assert egress_relay.offers_h2(_hello(alpn)) is offered


@pytest.mark.parametrize("junk", [b"", b"\x16", b"GET / HTTP/1.1\r\n\r\n", b"\x16\x03\x01\x00\x05\x01\xff"])
def test_offers_h2_is_no_for_anything_unreadable(junk: bytes) -> None:
    assert not egress_relay.offers_h2(junk)


def test_offers_h2_survives_any_truncation() -> None:
    hello = _hello(["h2"])
    for cut in range(len(hello)):
        assert egress_relay.offers_h2(hello[:cut]) in (True, False)


async def _tunnel(port: int, target: str, auth: str, first: bytes) -> bytes:
    """A CONNECT that then speaks `first`: everything the proxy answers."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    encoded = base64.b64encode(auth.encode()).decode()
    writer.write(
        f"CONNECT {target} HTTP/1.1\r\nProxy-Authorization: Basic {encoded}\r\n\r\n".encode() + first
    )
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    return answer


async def _forward(port: int, url: str, auth: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    encoded = base64.b64encode(auth.encode()).decode()
    writer.write(f"GET {url} HTTP/1.1\r\nProxy-Authorization: Basic {encoded}\r\n\r\n".encode())
    await writer.drain()
    answer = await asyncio.wait_for(reader.read(), timeout=5)
    writer.close()
    return answer


@pytest.fixture
async def upstream(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[int, list[bytes]]]:
    """A public host, as far as the proxy can tell: it records what reaches it, and answers."""
    heard: list[bytes] = []

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        heard.append(await reader.read(65536))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()

    async def public(host: str, port: int) -> tuple[list[str], str | None]:
        return ["127.0.0.1"], None

    monkeypatch.setattr(egress_relay, "_resolve_and_vet", public)
    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    async with server:
        yield server.sockets[0].getsockname()[1], heard


async def test_an_asset_tunnel_is_chromes_and_must_open_as_https(
    device: tuple[DeviceEgress, FrameRegistry], upstream: tuple[int, list[bytes]]
) -> None:
    """A pass admits a tunnel only for what Chrome's https opens with: a WebSocket's own hello, plain
    `ws:` and a TURN relay's bytes hear a refusal, and the host hears nothing."""
    proxy, _ = device
    port, heard = upstream
    target, chrome = f"cdn.example:{port}", f"chrome:{proxy.chrome_token}"
    proxy.pass_asset("cdn.example", port)

    https = _hello(["h2", "http/1.1"])
    assert (await _tunnel(proxy.port, target, chrome, https)).startswith(
        b"HTTP/1.1 200 Connection Established"
    )
    assert heard == [https]

    heard.clear()
    for first in (
        _hello(["http/1.1"]),
        b"GET /socket HTTP/1.1\r\nUpgrade: websocket\r\n\r\n",
        b"\0\1\0\0stun",
    ):
        answer = await _tunnel(proxy.port, target, chrome, first)
        assert b"403 Forbidden" in answer and egress_relay.ASSET_TUNNEL_REFUSED.encode() in answer
    assert heard == []


async def test_no_tunnel_at_the_socket_door_takes_a_pass(
    device: tuple[DeviceEgress, FrameRegistry], upstream: tuple[int, list[bytes]]
) -> None:
    """A WebSocket is a tunnel at this door, and it never met the fence: a pass is not for it, even
    with the https hello it could never send."""
    proxy, _ = device
    port, heard = upstream
    proxy.pass_asset("cdn.example", port)
    answer = await _ask(proxy.socket_port, f"cdn.example:{port}", auth=f"chrome:{proxy.chrome_token}")
    assert answer.startswith(b"HTTP/1.1 403 Forbidden\r\n") and heard == []


async def test_an_http_asset_goes_through_the_socket_door_only(
    device: tuple[DeviceEgress, FrameRegistry], upstream: tuple[int, list[bytes]]
) -> None:
    proxy, _ = device
    port, heard = upstream
    chrome = f"chrome:{proxy.chrome_token}"
    url = f"http://cdn.example:{port}/i.png"
    assert (await _forward(proxy.socket_port, url, chrome)).startswith(b"HTTP/1.1 403"), "no pass yet"
    proxy.pass_asset("cdn.example", port)
    assert (await _forward(proxy.port, url, chrome)).startswith(b"HTTP/1.1 403"), "the https door tunnels"
    assert heard == []
    assert (await _forward(proxy.socket_port, url, chrome)).startswith(b"HTTP/1.1 200")
    assert len(heard) == 1


async def test_a_pass_is_chromes_alone_and_widens_nothing(
    device: tuple[DeviceEgress, FrameRegistry], upstream: tuple[int, list[bytes]]
) -> None:
    """A frame's shell, on the same proxy, meets its list; the union stays the lists' own."""
    proxy, registry = device
    port, heard = upstream
    frame = _make(registry, "t1", ["a.example"])
    proxy.pass_asset("cdn.example", port)
    answer = await _tunnel(proxy.port, f"cdn.example:{port}", f"{frame.ref}:{frame.token}", _hello(["h2"]))
    assert answer.startswith(b"HTTP/1.1 403 Forbidden\r\n") and heard == []
    assert frame.denials[-1]["host"] == "cdn.example"
    assert "cdn.example" not in frame.allow and "cdn.example" not in proxy.allowed_for("chrome")


async def test_a_pass_names_one_host_and_one_port(
    device: tuple[DeviceEgress, FrameRegistry], upstream: tuple[int, list[bytes]]
) -> None:
    proxy, _ = device
    port, heard = upstream
    chrome, hello = f"chrome:{proxy.chrome_token}", _hello(["h2"])
    proxy.pass_asset("cdn.example", port)
    for target in (f"api.cdn.example:{port}", f"example:{port}", f"cdn.example:{port + 1}"):
        assert (await _tunnel(proxy.port, target, chrome, hello)).startswith(b"HTTP/1.1 403"), target
    assert heard == []
    proxy.pass_asset("CDN.example.", port)  # a name is one name however it is spelled
    assert (await _tunnel(proxy.port, f"cdn.example:{port}", chrome, hello)).startswith(b"HTTP/1.1 200")


async def test_a_pass_ends(
    device: tuple[DeviceEgress, FrameRegistry],
    upstream: tuple[int, list[bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proxy, _ = device
    port, heard = upstream
    monkeypatch.setattr(device_egress, "ASSET_PASS_S", -1.0)
    proxy.pass_asset("cdn.example", port)
    answer = await _tunnel(proxy.port, f"cdn.example:{port}", f"chrome:{proxy.chrome_token}", _hello(["h2"]))
    assert answer.startswith(b"HTTP/1.1 403") and heard == []


def test_passes_are_capped_oldest_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(device_egress, "_ASSET_PASSES", 2)
    proxy = DeviceEgress(FrameRegistry(tmp_path / "home").frames)
    for name in ("a.example", "b.example", "c.example"):
        proxy.pass_asset(name, 443)
    assert [proxy._passed(True, "chrome", n, 443, True) for n in ("a.example", "b.example", "c.example")] == [  # pyright: ignore[reportPrivateUsage]
        False,
        True,
        True,
    ]


async def test_a_public_name_resolving_inward_is_refused_for_an_asset(
    device: tuple[DeviceEgress, FrameRegistry], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass lifts the list and nothing after it: the resolve-and-vet still refuses."""

    async def inward(host: str, port: int, **_: object):
        return [(None, None, None, None, ("10.0.0.5", port))]

    monkeypatch.setattr(egress_relay, "_getaddrinfo", inward)
    proxy, _ = device
    proxy.pass_asset("cdn.example", 443)
    answer = await _tunnel(proxy.port, "cdn.example:443", f"chrome:{proxy.chrome_token}", _hello(["h2"]))
    assert answer.startswith(b"HTTP/1.1 403 Forbidden\r\n") and b"private address" in answer


def test_the_seed_requests_refusal_is_silent_and_any_other_is_not(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The seed is refused by design, once per Chrome: a DENY line for it would be noise that reads
    as a fence event. Any other refusal of Chrome's still says so."""
    proxy = DeviceEgress(FrameRegistry(tmp_path / "home").frames)
    with caplog.at_level(logging.WARNING, logger="prismoid.device.egress"):
        proxy.on_deny("chrome", device_egress.SEED_HOST, "not in allowlist")
        assert not caplog.records
        proxy.on_deny("chrome", "off.example", "not in allowlist")
    assert "off.example" in caplog.text


async def test_the_socket_door_challenges_as_the_other_does(
    device: tuple[DeviceEgress, FrameRegistry],
) -> None:
    proxy, registry = device
    _make(registry, "a", ["a.example"])
    answer = await _ask(proxy.socket_port, "a.example:443")
    assert answer.startswith(b"HTTP/1.1 407 Proxy Authentication Required\r\n")


async def test_the_proxy_url_carries_the_frame_credential(tmp_path: Path) -> None:
    registry = FrameRegistry(tmp_path / "home")
    proxy = DeviceEgress(registry.frames)  # the port is bound at once, before it serves
    frame = _make(registry, "t1")
    assert proxy.url_for(frame) == f"http://{frame.ref}:{frame.token}@127.0.0.1:{proxy.port}"
    await proxy.close()
