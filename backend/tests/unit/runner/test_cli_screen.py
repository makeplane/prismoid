# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`prismoid tasks screen`: who drives the browser, taking and handing back control, and `--shot`,
one frame saved from a terminal over the `websockets` client. The screen is played by a tiny
one-connection server on `socket` + `threading` that builds its frames independently of the
code under test."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import socket
import struct
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from prismoid.runner import client
from prismoid.runner.cli import machine
from prismoid.runner.main import app
from prismoid.schemas.screen import decode, encode

RFC_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_TEXT, OP_BINARY, OP_CLOSE = 0x1, 0x2, 0x8


def _frame(opcode: int, payload: bytes = b"", *, fin: bool = True, masked: bool = False) -> bytes:
    """A frame as a server writes it, built here and not by the client library."""
    b0 = (0x80 if fin else 0) | opcode
    bit = 0x80 if masked else 0
    n = len(payload)
    if n < 126:
        head = bytes([b0, bit | n])
    elif n < 65536:
        head = bytes([b0, bit | 126]) + struct.pack("!H", n)
    else:
        head = bytes([b0, bit | 127]) + struct.pack("!Q", n)
    if not masked:
        return head + payload
    key = b"\x01\x02\x03\x04"
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


class Peer:
    """The server's side of the one accepted connection."""

    def __init__(self, sock: socket.socket, buffered: bytes) -> None:
        self.sock = sock
        self.buf = buffered

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("the client went away")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def frame(self) -> tuple[bool, int, bytes, bool]:
        """(fin, opcode, unmasked payload, was it masked)."""
        b0, b1 = self._read(2)
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack("!H", self._read(2))
        elif n == 127:
            (n,) = struct.unpack("!Q", self._read(8))
        masked = bool(b1 & 0x80)
        key = self._read(4) if masked else b""
        data = self._read(n)
        if masked:
            data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
        return bool(b0 & 0x80), b0 & 0x0F, data, masked

    def send(self, raw: bytes) -> None:
        self.sock.sendall(raw)


class Server:
    """One connection on a thread: answer the upgrade (or refuse it), then play `script(peer)`."""

    def __init__(
        self,
        script: Callable[[Peer], object] = lambda p: None,
        *,
        status: int = 101,
        accept: str | None = None,
    ) -> None:
        self.script, self.status, self.accept = script, status, accept
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.port = self.listener.getsockname()[1]
        self.request = ""
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def _run(self) -> None:
        try:
            conn, _ = self.listener.accept()
            conn.settimeout(5)
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += conn.recv(4096)
                head, _, rest = data.partition(b"\r\n\r\n")
                self.request = head.decode()
                found = re.search(r"(?im)^sec-websocket-key:\s*(\S+)", self.request)
                assert found is not None
                if self.status != 101:
                    conn.sendall(f"HTTP/1.1 {self.status} Forbidden\r\nContent-Length: 0\r\n\r\n".encode())
                    return
                accept = (
                    self.accept
                    or base64.b64encode(hashlib.sha1(found[1].encode() + RFC_GUID).digest()).decode()
                )
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                    ).encode()
                )
                self.script(Peer(conn, rest))
        except BaseException as e:  # surfaced by join()
            self.error = e
        finally:
            self.listener.close()

    def join(self) -> None:
        self.thread.join(5)
        assert not self.thread.is_alive(), "the test server is stuck"
        if self.error:
            raise self.error


# ── prismoid tasks screen ─────────────────────────────────────────────────────────────────────

CONTROL = {
    "holder": {"id": "a1", "name": "Ada"},
    "private": True,
    "since": "2026-10-02T12:00:00Z",
    "until": "2026-10-02T12:10:00Z",
    "cap_at": "2026-10-02T12:10:00Z",
}


def _fake(
    monkeypatch: pytest.MonkeyPatch, *, control: dict[str, Any] | None = CONTROL, machine: bool = True
) -> list[tuple[str, str, Any]]:
    seen: list[tuple[str, str, Any]] = []

    def fake_request(method: str, path: str, **kw: Any) -> Any:
        seen.append((method, path, kw.get("body")))
        if method == "GET":
            return {
                "id": "t1",
                "machine": {"state": "idle", "profile": "browser", "control": control} if machine else None,
            }
        if path.endswith("/ticket"):
            return {"ticket": "st_abc.def", "expires_in": 30}
        if method == "DELETE":
            return None
        return CONTROL

    monkeypatch.setattr(client, "request", fake_request)
    return seen


def _run(*args: str) -> Any:
    return CliRunner().invoke(app, ["tasks", "screen", *args])


@pytest.fixture(autouse=True)
def screen_conn_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """This terminal's screen connection lives beside its credentials: here, a temporary home."""
    path = tmp_path / "home" / ".prismoid" / "screen-conn"
    monkeypatch.setattr(client, "SCREEN_CONN", path)
    return path


def test_claim_and_shot_are_this_terminals_one_connection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, screen_conn_home: Path
) -> None:
    """The mask hides a private lease from every connection but the holder's own: a
    `--shot` under this terminal's own private claim must be that connection, or it is masked
    from the person who holds the browser (seen live). Kept like the credentials, owner-only."""
    seen = _fake(monkeypatch)
    assert _run("t1", "--claim", "--private", "--json").exit_code == 0
    claimed = seen[-1][2]["conn"]
    assert screen_conn_home.read_text() == claimed and screen_conn_home.stat().st_mode & 0o777 == 0o600

    def script(peer: Peer) -> None:
        peer.send(_frame(OP_BINARY, encode({"t": "masked", "holder": None})))
        peer.send(_frame(OP_BINARY, encode({"t": "frame", "seq": 1, "w": 1, "h": 1, "fmt": "jpeg"}, b"\xff")))
        while peer.frame()[1] != OP_CLOSE:
            pass

    server = _shot_server(script, monkeypatch)
    shots = _fake(monkeypatch)
    assert _run("t1", "--shot", str(tmp_path / "x.jpg")).exit_code == 0
    server.join()
    assert shots[-1][:2] == ("POST", "/v1/tasks/t1/screen/ticket") and shots[-1][2] == {"conn": claimed}
    assert _run("t1", "--claim", "--json").exit_code == 0
    assert shots[-1][2]["conn"] == claimed, "a re-claim rebinds the same connection"


def test_screen_shows_the_lease_and_the_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)
    r = _run("t1", "--json")
    assert r.exit_code == 0 and json.loads(r.stdout) == {"state": "idle", "control": CONTROL}
    r = _run("t1")
    assert "Idle" in r.stdout and "Ada is driving (private) until 2026-10-02T12:10:00Z" in r.stdout
    _fake(monkeypatch, control=None)
    assert "nobody; the agent has the browser" in _run("t1").stdout
    _fake(monkeypatch, machine=False)
    r = _run("t1", "--json")
    assert json.loads(r.stdout) == {"state": None, "control": None}


def test_claim_posts_a_fresh_conn_and_the_private_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    r = _run("t1", "--claim", "--private", "--json")
    assert r.exit_code == 0 and json.loads(r.stdout)["holder"]["name"] == "Ada"
    method, path, body = seen[-1]
    assert (method, path, body["private"]) == ("POST", "/v1/tasks/t1/screen/control", True)
    assert re.fullmatch(r"[A-Za-z0-9_-]{8,64}", body["conn"])


def test_renew_release_and_end(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake(monkeypatch)
    assert _run("t1", "--renew", "--json").exit_code == 0
    assert seen[-1][:2] == ("POST", "/v1/tasks/t1/screen/control/renew")
    r = _run("t1", "--release", "--json")
    assert r.exit_code == 0 and json.loads(r.stdout) == {"control": None}
    assert seen[-1][:2] == ("DELETE", "/v1/tasks/t1/screen/control")
    assert "control ended" in _run("t1", "--end").stdout


def test_one_action_at_a_time_and_private_only_with_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)
    assert _run("t1", "--claim", "--release").exit_code == client.EXIT_ERROR
    assert _run("t1", "--private").exit_code == client.EXIT_ERROR


@pytest.mark.parametrize(
    ("status", "exit_code"), [(403, client.EXIT_DENIED), (404, client.EXIT_ERROR), (409, client.EXIT_ERROR)]
)
def test_refusals_map_to_exit_codes(monkeypatch: pytest.MonkeyPatch, status: int, exit_code: int) -> None:
    monkeypatch.setattr(client, "current_token", lambda: "cs_test")
    envelope = {"error": {"code": "refused", "message": "Ada is driving the browser."}}
    monkeypatch.setattr(httpx, "request", lambda *a, **kw: httpx.Response(status, json=envelope))
    r = _run("t1", "--claim")
    assert r.exit_code == exit_code
    assert "Ada is driving the browser." in r.output


def _shot_server(script: Callable[[Peer], object], monkeypatch: pytest.MonkeyPatch) -> Server:
    server = Server(script)
    _fake(monkeypatch)
    monkeypatch.setattr(client, "api_url", lambda: f"http://127.0.0.1:{server.port}")
    return server


def test_shot_saves_the_first_frame(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    jpeg = b"\xff\xd8\xff\xe0" + b"j" * 2000
    hello: list[dict[str, Any]] = []

    def script(peer: Peer) -> None:
        for _ in range(2):  # hello, then the hint asking for a fresh frame
            hello.append(decode(peer.frame()[2])[0])
        peer.send(_frame(OP_BINARY, encode({"t": "status", "chromium": "running", "busy": False})))
        peer.send(
            _frame(
                OP_BINARY,
                encode({"t": "frame", "seq": 1, "ts": 0, "w": 1280, "h": 900, "fmt": "jpeg"}, jpeg),
            )
        )
        while peer.frame()[1] != OP_CLOSE:
            pass
        peer.send(_frame(OP_CLOSE, struct.pack("!H", 1000)))

    server = _shot_server(script, monkeypatch)
    out = tmp_path / "shot.jpg"
    r = _run("t1", "--shot", str(out), "--json")
    server.join()
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout) == {"path": str(out), "bytes": len(jpeg), "w": 1280, "h": 900}
    assert out.read_bytes() == jpeg
    assert server.request.startswith("GET /v1/tasks/t1/screen?ticket=st_abc.def HTTP/1.1")
    assert [h["t"] for h in hello] == ["hello", "hint"] and hello[1]["refresh"] is True


def test_shot_says_computer_use_is_off_on_4410(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def script(peer: Peer) -> None:
        peer.send(_frame(OP_CLOSE, struct.pack("!H", 4410) + b"off"))
        peer.frame()  # the echo

    server = _shot_server(script, monkeypatch)
    r = _run("t1", "--shot", str(tmp_path / "x.jpg"))
    server.join()
    assert r.exit_code == client.EXIT_ERROR and "Computer use is off." in r.output
    assert not (tmp_path / "x.jpg").exists()


def test_shot_behind_a_private_lease_names_the_holder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def script(peer: Peer) -> None:
        peer.send(_frame(OP_BINARY, encode({"t": "masked", "holder": "Bob"})))
        while peer.frame()[1] != OP_CLOSE:
            pass

    server = _shot_server(script, monkeypatch)
    r = _run("t1", "--shot", str(tmp_path / "x.jpg"))
    server.join()
    assert r.exit_code == client.EXIT_ERROR and "Bob is signing in privately" in r.output


def test_shot_on_a_machine_with_no_screen_says_so_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A sandbar Machine never sends a frame: the relay's `no_screen` ends the
    shot with the reason, never a 15 s "timed out waiting for the server"."""

    def script(peer: Peer) -> None:
        peer.send(_frame(OP_BINARY, encode({"t": "hello", "v": 1})))
        peer.send(_frame(OP_BINARY, encode({"t": "status", "upstream": "no_screen"})))
        while peer.frame()[1] != OP_CLOSE:
            pass

    server = _shot_server(script, monkeypatch)
    r = _run("t1", "--shot", str(tmp_path / "x.jpg"))
    server.join()
    assert r.exit_code == client.EXIT_ERROR and "This Machine has no live screen yet." in r.output
    assert not (tmp_path / "x.jpg").exists()


def test_shot_reads_an_unmasked_socket_as_unmasked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every attach says its mask state: `masked{holder: null}` is no mask,
    so the frame after it is saved."""
    jpeg = b"\xff\xd8\xff\xe0" + b"j" * 100

    def script(peer: Peer) -> None:
        peer.send(_frame(OP_BINARY, encode({"t": "hello", "v": 1})))
        peer.send(_frame(OP_BINARY, encode({"t": "masked", "holder": None})))
        header = {"t": "frame", "seq": 1, "ts": 0, "w": 1280, "h": 900, "fmt": "jpeg"}
        peer.send(_frame(OP_BINARY, encode(header, jpeg)))
        while peer.frame()[1] != OP_CLOSE:
            pass
        peer.send(_frame(OP_CLOSE, struct.pack("!H", 1000)))

    server = _shot_server(script, monkeypatch)
    out = tmp_path / "x.jpg"
    r = _run("t1", "--shot", str(out))
    server.join()
    assert r.exit_code == 0, r.output
    assert out.read_bytes() == jpeg


def test_claim_says_a_non_private_lease_ends_on_silence(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake(monkeypatch)
    assert "ends after a minute without input; use --private or --renew" in _run("t1", "--claim").stdout
    assert "minute" not in _run("t1", "--claim", "--private").stdout


# ── fix round 1: no ticket, no raw names, a real deadline, a careful write ─────────────────────

EVIL = "Ada\x1b[31m‮evil"


def test_a_bad_api_url_scheme_never_prints_or_mints_a_ticket(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _fake(monkeypatch)
    monkeypatch.setattr(client, "api_url", lambda: "localhost:8000")
    r = _run("t1", "--shot", str(tmp_path / "x.jpg"))
    assert r.exit_code == client.EXIT_ERROR and "st_" not in r.output
    assert seen == []  # refused before the ticket was minted


def test_a_holder_name_is_sanitised_in_state_and_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    evil = {**CONTROL, "holder": {"id": "a1", "name": EVIL}}
    _fake(monkeypatch, control=evil)
    out = _run("t1").stdout
    assert "\x1b" not in out and "‮" not in out and "Ada[31mevil is driving" in out


def test_a_masked_holder_name_is_sanitised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def script(peer: Peer) -> None:
        peer.send(_frame(OP_BINARY, encode({"t": "masked", "holder": {"id": "b", "name": EVIL}})))
        while peer.frame()[1] != OP_CLOSE:
            pass

    server = _shot_server(script, monkeypatch)
    r = _run("t1", "--shot", str(tmp_path / "x.jpg"))
    server.join()
    assert "\x1b" not in r.output and "‮" not in r.output and "Ada[31mevil" in r.output


def test_a_close_reason_is_sanitised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def script(peer: Peer) -> None:
        peer.send(_frame(OP_CLOSE, struct.pack("!H", 4999) + EVIL.encode()))
        peer.frame()

    server = _shot_server(script, monkeypatch)
    r = _run("t1", "--shot", str(tmp_path / "x.jpg"))
    server.join()
    assert r.exit_code == client.EXIT_ERROR and "\x1b" not in r.output and "‮" not in r.output


def _frame_script(header: dict[str, Any], payload: bytes) -> Callable[[Peer], None]:
    def script(peer: Peer) -> None:
        peer.send(_frame(OP_BINARY, encode(header, payload)))
        while peer.frame()[1] != OP_CLOSE:
            pass

    return script


@pytest.mark.parametrize(
    ("header", "payload"),
    [
        ({"t": "frame", "seq": 1, "ts": 0, "w": 1, "h": 1, "fmt": "png"}, b"x"),
        ({"t": "frame", "seq": 1, "ts": 0, "w": 1, "h": 1, "fmt": "jpeg"}, b""),
    ],
)
def test_shot_refuses_a_non_jpeg_or_empty_frame(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, header: dict[str, Any], payload: bytes
) -> None:
    server = _shot_server(_frame_script(header, payload), monkeypatch)
    out = tmp_path / "x.jpg"
    r = _run("t1", "--shot", str(out))
    server.join()
    assert r.exit_code == client.EXIT_ERROR and not out.exists()


def test_shot_write_failure_is_a_clean_error_and_without_size_prints_no_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    head = {"t": "frame", "seq": 1, "ts": 0, "fmt": "jpeg"}
    server = _shot_server(_frame_script(head, b"\xff\xd8j"), monkeypatch)
    r = _run("t1", "--shot", str(tmp_path / "no" / "dir" / "x.jpg"), "--json")
    server.join()
    assert r.exit_code == client.EXIT_ERROR and "cannot write" in r.output and "Traceback" not in r.output
    server = _shot_server(_frame_script(head, b"\xff\xd8j"), monkeypatch)
    r = _run("t1", "--shot", str(tmp_path / "ok.jpg"))
    server.join()
    assert r.exit_code == 0 and "None" not in r.stdout and "saved a frame" in r.stdout


# ── the socket's failures, as the terminal says them ──────────────────────────────────────────


@pytest.mark.parametrize(("status", "exit_code"), [(403, client.EXIT_DENIED), (500, client.EXIT_ERROR)])
def test_a_refused_upgrade_maps_to_its_exit_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: int, exit_code: int
) -> None:
    server = Server(status=status)
    _fake(monkeypatch)
    monkeypatch.setattr(client, "api_url", lambda: f"http://127.0.0.1:{server.port}")
    r = _run("t1", "--shot", str(tmp_path / "x.jpg"))
    server.join()
    assert r.exit_code == exit_code and f"http {status}" in r.output
    assert "st_abc" not in r.output, "an error never carries the URL, which holds the ticket"


def test_a_wrong_accept_fails_the_handshake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = Server(accept="bm90IHRoZSByaWdodCBvbmU=")
    _fake(monkeypatch)
    monkeypatch.setattr(client, "api_url", lambda: f"http://127.0.0.1:{server.port}")
    r = _run("t1", "--shot", str(tmp_path / "x.jpg"))
    server.join()
    assert r.exit_code == client.EXIT_ERROR and "handshake failed" in r.output
    assert "st_abc" not in r.output


def test_silence_past_the_deadline_is_a_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    hold = threading.Event()
    server = _shot_server(lambda peer: hold.wait(3), monkeypatch)
    monkeypatch.setattr(machine, "SHOT_TIMEOUT_S", 0.3)
    r = _run("t1", "--shot", str(tmp_path / "x.jpg"), "--json")
    hold.set()
    server.join()
    assert r.exit_code == client.EXIT_TIMEOUT and "timed out" in r.output


def test_a_text_message_is_not_the_screens_and_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    jpeg = b"\xff\xd8j"

    def script(peer: Peer) -> None:
        peer.send(_frame(OP_TEXT, b"hello"))
        peer.send(_frame(OP_BINARY, encode({"t": "frame", "seq": 1, "w": 2, "h": 2, "fmt": "jpeg"}, jpeg)))
        while peer.frame()[1] != OP_CLOSE:
            pass
        peer.send(_frame(OP_CLOSE, struct.pack("!H", 1000)))

    server = _shot_server(script, monkeypatch)
    out = tmp_path / "x.jpg"
    r = _run("t1", "--shot", str(out))
    server.join()
    assert r.exit_code == 0, r.output
    assert out.read_bytes() == jpeg


def test_client_frames_are_masked(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    masks: list[bool] = []

    def script(peer: Peer) -> None:
        masks.extend(peer.frame()[3] for _ in range(2))  # hello, hint
        peer.send(_frame(OP_BINARY, encode({"t": "frame", "seq": 1, "w": 1, "h": 1, "fmt": "jpeg"}, b"\xff")))
        while peer.frame()[1] != OP_CLOSE:
            pass

    server = _shot_server(script, monkeypatch)
    assert _run("t1", "--shot", str(tmp_path / "x.jpg")).exit_code == 0
    server.join()
    assert masks == [True, True]
