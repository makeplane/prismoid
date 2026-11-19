# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Docker API's wire, as the filtering proxy (`docker_proxy.py`) reads and writes it: a request
head it is sure it reads as the daemon will, a body framed exactly, the proxy's own calls to the
daemon, and the shape of its answers. Every refusal is Docker's own error shape, `{"message": ...}`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import quote

# One logger for the proxy, however many files it is told in.
LOG = logging.getLogger("prismoid.device.docker_proxy")

HEAD_LIMIT: Final = 64 * 1024
_BODY_CAP: Final = 4 * 1024 * 1024  # a create body the proxy reads to vet; a build context streams
_WHOLE_CAP: Final = 1024 * 1024  # a create's or a removal's answer, read whole to journal it
CALL_TIMEOUT_S: Final = 30.0
TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_STREAM_TYPES: Final = frozenset(
    {"application/vnd.docker.raw-stream", "application/vnd.docker.multiplexed-stream"}
)
_BAD_HEADER = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
_HOP_BY_HOP: Final = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-connection",
        "proxy-authorization",
        "proxy-authenticate",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "expect",
    }
)
_STATUS_TEXT: Final = {
    400: "Bad Request",
    403: "Forbidden",
    404: "Not Found",
    413: "Payload Too Large",
    500: "Internal Server Error",
    502: "Bad Gateway",
    503: "Service Unavailable",
}


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Head:
    first: str  # the request or status line
    headers: tuple[tuple[str, str], ...]

    def values(self, name: str) -> list[str]:
        return [v for n, v in self.headers if n.lower() == name]

    def tokens(self, name: str) -> set[str]:
        return {t.strip().lower() for v in self.values(name) for t in v.split(",")}


def parse_head(raw: bytes) -> Head:
    """A head the proxy is sure it reads as the daemon will: CRLF lines only, no folding, no bare
    CR or LF and no control character anywhere (Go's reader ends a line at a bare LF)."""
    lines = raw.removesuffix(b"\r\n\r\n").decode("latin-1").split("\r\n")
    if any(_BAD_HEADER.search(line) or "\r" in line for line in lines):
        raise BadRequest("a malformed request head")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if not sep or not TOKEN.match(name):
            raise BadRequest("a malformed request header")
        headers.append((name, value.strip()))
    return Head(lines[0], tuple(headers))


def request_line(head: Head) -> tuple[str, str]:
    parts = head.first.split(" ")
    if len(parts) != 3 or not TOKEN.match(parts[0]) or parts[2] not in ("HTTP/1.1", "HTTP/1.0"):
        raise BadRequest("a malformed request line")
    return parts[0].upper(), parts[1]


def body_length(head: Head) -> int | None:
    """The body's length, or None for a chunked one; ambiguous framing is refused, never guessed."""
    chunked, lengths = head.values("transfer-encoding"), head.values("content-length")
    if chunked:
        if lengths or [v.lower() for v in chunked] != ["chunked"]:
            raise BadRequest("an ambiguous request body")
        return None
    if len(lengths) > 1 or (lengths and not re.fullmatch(r"[0-9]{1,15}", lengths[0])):
        raise BadRequest("an ambiguous request body")
    return int(lengths[0]) if lengths else 0


async def chunks(reader: asyncio.StreamReader) -> AsyncIterator[bytes]:
    """A chunked body's data, chunk by chunk; its trailers are read and dropped."""
    while True:
        line = await reader.readuntil(b"\r\n")
        size = line[:-2].split(b";")[0].strip()
        if not re.fullmatch(rb"[0-9a-fA-F]{1,15}", size):
            raise BadRequest("a malformed chunk")
        if not (n := int(size, 16)):
            while await reader.readuntil(b"\r\n") != b"\r\n":
                pass
            return
        data = await reader.readexactly(n)
        if await reader.readexactly(2) != b"\r\n":
            raise BadRequest("a malformed chunk")
        yield data


async def read_body(reader: asyncio.StreamReader, length: int | None) -> bytes:
    if length is not None:
        if length > _BODY_CAP:
            raise BadRequest("the request body is too large to vet", 413)
        return await reader.readexactly(length)
    body = bytearray()
    async for data in chunks(reader):
        body += data
        if len(body) > _BODY_CAP:
            raise BadRequest("the request body is too large to vet", 413)
    return bytes(body)


async def copy_body(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, length: int | None) -> None:
    """Forward a body exactly as framed: the bytes after it are never the daemon's to read."""
    if length is None:
        async for data in chunks(reader):
            writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            await writer.drain()
        writer.write(b"0\r\n\r\n")
        return
    while length:
        data = await reader.read(min(length, 65536))
        if not data:
            raise asyncio.IncompleteReadError(b"", length)
        writer.write(data)
        await writer.drain()
        length -= len(data)


def forward_head(method: str, target: str, head: Head, *, length: int | None, upgrade: str | None) -> bytes:
    lines = [f"{method} {target} HTTP/1.1"]
    lines += [f"{n}: {v}" for n, v in head.headers if n.lower() not in _HOP_BY_HOP]
    if length is None:
        lines.append("Transfer-Encoding: chunked")
    elif length or method in ("POST", "PUT"):
        lines.append(f"Content-Length: {length}")
    lines += ["Connection: Upgrade", f"Upgrade: {upgrade}"] if upgrade else ["Connection: close"]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def status_of(head: Head) -> int:
    parts = head.first.split(" ")
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0


def error_bytes(status: int, message: str) -> bytes:
    body = json.dumps({"message": message}).encode()
    text = _STATUS_TEXT.get(status, "Error")
    head = f"HTTP/1.1 {status} {text}\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
    return (head + "Connection: close\r\n\r\n").encode() + body


async def call(
    upstream: str, method: str, path: str, body: object = None, wait_s: float = CALL_TIMEOUT_S
) -> tuple[int, Any]:
    """One request of the proxy's own to the daemon: (status, its JSON body or None)."""

    async def go() -> tuple[int, Any]:
        reader, writer = await asyncio.open_unix_connection(upstream, limit=HEAD_LIMIT)
        try:
            data = b"" if body is None else json.dumps(body).encode()
            kind = "Content-Type: application/json\r\n" if body is not None else ""
            head = f"{method} {path} HTTP/1.1\r\nHost: docker\r\nConnection: close\r\n{kind}"
            writer.write(f"{head}Content-Length: {len(data)}\r\n\r\n".encode() + data)
            await writer.drain()
            response = parse_head(await reader.readuntil(b"\r\n\r\n"))
            if "chunked" in response.tokens("transfer-encoding"):
                raw = b"".join([chunk async for chunk in chunks(reader)])
            else:
                raw = await reader.read()
        finally:
            writer.close()
        try:
            return status_of(response), json.loads(raw) if raw else None
        except ValueError:
            return status_of(response), None

    return await asyncio.wait_for(go(), wait_s)


UPSTREAM_ERRORS = (
    OSError,
    asyncio.IncompleteReadError,
    asyncio.LimitOverrunError,
    TimeoutError,
    BadRequest,
)


async def read_whole(up_reader: asyncio.StreamReader) -> tuple[bytes, int, bytes]:
    """A small answer read whole (up to `_WHOLE_CAP`): (its bytes, its status, its decoded body)."""
    data = bytearray()
    while len(data) <= _WHOLE_CAP and (chunk := await up_reader.read(65536)):
        data += chunk
    head, _, body = bytes(data).partition(b"\r\n\r\n")
    response = parse_head(head + b"\r\n\r\n")
    if "chunked" in response.tokens("transfer-encoding"):
        try:
            decoded, rest = bytearray(), body
            while (line_end := rest.find(b"\r\n")) > 0 and (size := int(rest[:line_end].split(b";")[0], 16)):
                decoded += rest[line_end + 2 : line_end + 2 + size]
                rest = rest[line_end + 4 + size :]
            body = bytes(decoded)
        except ValueError:  # a malformed chunk size
            body = b""
    return bytes(data), status_of(response), body


async def relay_answer(writer: asyncio.StreamWriter, data: bytes, up_reader: asyncio.StreamReader) -> None:
    writer.write(data)
    await writer.drain()
    while chunk := await up_reader.read(65536):  # past the cap: relayed as it comes
        writer.write(chunk)
        await writer.drain()


async def send_answer(writer: asyncio.StreamWriter, status: int, message: str) -> None:
    writer.write(error_bytes(status, message))
    await writer.drain()


def created_id(body: bytes) -> str | None:
    """The id (a volume's name) a create's 201 names."""
    try:
        doc = json.loads(body)
    except ValueError:
        return None
    ident = doc.get("Id") or doc.get("Name") if isinstance(doc, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return ident if isinstance(ident, str) and ident else None


def lookup_path(kind: str, ident: str) -> str:
    """The request that inspects a container, a network or a volume."""
    where = {"container": "/containers/{}/json", "network": "/networks/{}", "volume": "/volumes/{}"}[kind]
    return where.format(quote(ident, safe=""))


def labels_of(kind: str, doc: dict[str, Any]) -> dict[str, Any]:
    """An inspect document's labels (a container's are its config's)."""
    labels = (doc.get("Config") or {}).get("Labels") if kind == "container" else doc.get("Labels")  # pyright: ignore[reportUnknownMemberType]
    return labels if isinstance(labels, dict) else {}  # pyright: ignore[reportUnknownVariableType]


def removal_path(kind: str, ident: str) -> str:
    """The request that removes one of a frame's objects: a container with its anonymous volumes."""
    tail = "?force=1&v=1" if kind == "container" else ""
    return f"/{kind}s/{quote(ident, safe='')}{tail}"


def daemon_refusal(status: int, doc: object, what: str) -> tuple[int, str]:
    """The daemon's own refusal of a lookup (its 404 is what compose waits for), or a 502."""
    message = doc.get("message") if isinstance(doc, dict) else None  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    if status == 404:
        return 404, str(message or f"no such {what}")  # pyright: ignore[reportUnknownArgumentType]
    return 502, f"Docker answered {status} when Prismoid's Docker proxy looked up {what}"


async def relay_response(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, up_reader: asyncio.StreamReader
) -> None:
    """The daemon's answer, to its end (it closes: the request said `Connection: close`). Anything
    the client sends meanwhile, or its going away, ends it: one request per connection."""

    async def down() -> None:
        while data := await up_reader.read(65536):
            writer.write(data)
            await writer.drain()

    relay, watch = asyncio.create_task(down()), asyncio.create_task(reader.read(1))
    try:
        await asyncio.wait({relay, watch}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (relay, watch):
            task.cancel()
        await asyncio.gather(relay, watch, return_exceptions=True)


async def relay_hijacked(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    up_reader: asyncio.StreamReader,
    up_writer: asyncio.StreamWriter,
) -> None:
    """Attach, exec and BuildKit: once the daemon answers 101 (or a raw stream), bytes both ways
    until the daemon ends it. Any other answer is relayed and the connection ends: the client's
    next bytes would be a request nobody vetted."""
    raw = await up_reader.readuntil(b"\r\n\r\n")
    response = parse_head(raw)
    writer.write(raw)
    await writer.drain()
    status, kinds = (
        status_of(response),
        {v.split(";")[0].strip().lower() for v in response.values("content-type")},
    )
    if not (status == 101 or (status == 200 and kinds & _STREAM_TYPES)):
        await relay_response(reader, writer, up_reader)
        return

    async def up() -> None:
        while data := await reader.read(65536):
            up_writer.write(data)
            await up_writer.drain()
        if up_writer.can_write_eof():
            up_writer.write_eof()  # stdin closed: the container's output still flows

    async def down() -> None:
        while data := await up_reader.read(65536):
            writer.write(data)
            await writer.drain()

    stdin, stdout = asyncio.create_task(up()), asyncio.create_task(down())
    try:
        done, _ = await asyncio.wait({stdin, stdout}, return_when=asyncio.FIRST_COMPLETED)
        if stdin in done and stdin.exception() is None and stdout not in done:
            await stdout
    finally:
        for task in (stdin, stdout):
            task.cancel()
        await asyncio.gather(stdin, stdout, return_exceptions=True)
