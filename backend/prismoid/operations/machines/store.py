# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Machine's checkpoint store (ADR-063): what a sleep writes, a wake reads.

Everything moves through worker temp files (spooled: memory up to 16 MiB, then disk), so an
archive never sits whole in memory, and to the `snapshots` bucket by explicit key.

* `/work`: the driver writes a plain tar into a `CappedWriter` (the cap counts the tar's bytes,
  and the driver stops reading its provider's stream past it); it is gzipped and hashed into a
  second spool, then uploaded. The row keeps the key, the SHA-256
  and the size; a wake re-hashes the download before unpacking.
* `/browser`: the plain tar is encrypted in 4 MiB chunks - each a Fernet token behind a 4-byte
  big-endian length - with `operations/cipher`, so `ai/` never sees a key. Fernet authenticates
  every chunk, and each chunk's plaintext opens with a header that authenticates the SEQUENCE:
  the object's key (a 2-byte length, then the key's UTF-8), the chunk's index (4 bytes, from 0)
  and a final flag (1 byte, set on the last chunk; an empty archive still seals one final chunk).
  A wake opens the object under the key it read it from, so a tampered chunk, a gap, a reorder, a
  cut at a chunk boundary, a 0-byte object, anything after the final chunk, or another Machine's
  object copied onto this key (one cipher key covers every Machine) fails to open - before a byte
  is unpacked.

Checkpoints are cache: every failure here is typed (`Oversize`, `CheckpointLost`) so a
caller degrades to a rebuild, never a failed task.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import shutil
import struct
import tarfile
import tempfile
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from typing import IO, Any, Final

from prismoid import storage
from prismoid.ai.frames.driver import ArchiveTooLarge, Checkpoints, FrameDriver
from prismoid.ai.frames.layout import BROWSER, WORK
from prismoid.conf import settings
from prismoid.errors import PrismoidError
from prismoid.operations import cipher

CHUNK: Final = 4 * 1024 * 1024
_SPOOL: Final = 16 * 1024 * 1024
# A 4 MiB chunk and its header seal to a ~5.3 MiB token (base64 of the plaintext plus 73 bytes); a
# length header past this is corrupt, and reading it would pull the rest of the object into memory.
_MAX_TOKEN: Final = 2 * CHUNK
_SEQUENCE: Final = struct.Struct(">IB")  # a chunk's index, then 1 on the final chunk (else 0)


class Oversize(Exception):
    """The archive passed the cap; `seen` is how far it got before it was abandoned."""

    def __init__(self, seen: int) -> None:
        super().__init__(f"the archive passed {seen} bytes")
        self.seen = seen


class CheckpointLost(Exception):
    """A stored archive that is missing, does not match its hash, or will not decrypt."""


@dataclass(frozen=True, slots=True)
class Stored:
    key: str
    sha256: str
    size: int  # the stored object's bytes
    raw_bytes: int  # the tar's bytes before compression or encryption


class CappedWriter(io.RawIOBase):
    """A write-only stream that counts what passes into `sink` and raises `Oversize` at the byte
    that crosses `cap`. (typeshed's `IO[bytes]` is nominal, so the `archive` calls below carry a
    pyright ignore: a driver only ever `write`s into `out`.)"""

    def __init__(self, sink: IO[bytes], cap: int) -> None:
        self._sink, self._cap, self.written = sink, cap, 0

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        self.written += len(data)
        if self.written > self._cap:
            raise Oversize(self.written)
        self._sink.write(data)
        return len(data)


class _Hashing(io.RawIOBase):
    def __init__(self, sink: IO[bytes]) -> None:
        self._sink, self.digest, self.written = sink, hashlib.sha256(), 0

    def writable(self) -> bool:
        return True

    def write(self, data: Any) -> int:
        self.digest.update(data)
        self._sink.write(data)
        self.written += len(data)
        return len(data)


def _spool() -> IO[bytes]:
    return tempfile.SpooledTemporaryFile(max_size=_SPOOL)


def _bucket() -> str:
    return settings.worker.snapshot_bucket


def _gzip_and_hash(src: IO[bytes], dst: IO[bytes]) -> tuple[str, int]:
    sink = _Hashing(dst)
    with gzip.GzipFile(fileobj=sink, mode="wb", mtime=0) as gz:
        shutil.copyfileobj(src, gz, CHUNK)
    return sink.digest.hexdigest(), sink.written


def _hash(src: IO[bytes]) -> str:
    digest = hashlib.sha256()
    while block := src.read(CHUNK):
        digest.update(block)
    src.seek(0)
    return digest.hexdigest()


def _context(context: str) -> bytes:
    """The header's fixed prefix: the object key, length-prefixed so no key reads as another's."""
    raw = context.encode()
    return struct.pack(">H", len(raw)) + raw


def encrypt_stream(src: IO[bytes], dst: IO[bytes], *, context: str, chunk: int = CHUNK) -> int:
    """Seal `src` in `chunk`-byte chunks bound to `context` (the object's key). It reads one chunk
    ahead, so the last chunk knows it is last; an empty `src` still seals one final chunk."""
    head, total, index = _context(context), 0, 0
    block = src.read(chunk)
    while True:
        ahead = src.read(chunk) if block else b""
        final = not ahead
        token = cipher.encrypt_bytes(head + _SEQUENCE.pack(index, final) + block)
        dst.write(struct.pack(">I", len(token)))
        dst.write(token)
        total += 4 + len(token)
        if final:
            return total
        block, index = ahead, index + 1


def decrypt_stream(src: IO[bytes], dst: IO[bytes], *, context: str) -> None:
    """Open what `encrypt_stream` sealed under the same `context`, or raise `CheckpointLost`: every
    chunk must carry `context` and the next index from 0, and the stream must end exactly at the
    final chunk. When it raises, `dst` may hold a prefix of the plaintext: unpack nothing from it."""
    head = _context(context)
    body = len(head) + _SEQUENCE.size
    index, final = 0, False
    while header := src.read(4):
        if final:
            raise CheckpointLost("the browser archive runs on past its final chunk")
        if len(header) < 4:
            raise CheckpointLost("the browser archive is truncated")
        (size,) = struct.unpack(">I", header)
        if size > _MAX_TOKEN:
            raise CheckpointLost("the browser archive is corrupt")
        token = src.read(size)
        if len(token) != size:
            raise CheckpointLost("the browser archive is truncated")
        try:
            plain = cipher.decrypt_bytes(token)
        except cipher.CipherError as bad:
            raise CheckpointLost(str(bad)) from bad
        if len(plain) < body or not plain.startswith(head):
            raise CheckpointLost("a browser archive chunk belongs to another object")
        seen, flag = _SEQUENCE.unpack_from(plain, len(head))
        if seen != index or flag > 1:
            raise CheckpointLost("the browser archive's chunks are out of sequence")
        dst.write(memoryview(plain)[body:])
        index, final = index + 1, flag == 1
    if not final:
        raise CheckpointLost("the browser archive is truncated")  # also a 0-byte object


async def _archive(frame: FrameDriver, path: str, raw: IO[bytes], cap: int) -> int:
    """The directory's plain tar into `raw`, capped twice: the driver stops reading its provider's
    stream past `cap` (a driver that spools first would otherwise read it whole), and what reaches
    `raw` is counted too. Either is `Oversize`, and nothing is uploaded. Returns the tar's bytes."""
    if not isinstance(frame, Checkpoints):  # a `none` frame is never archived
        raise PrismoidError(f"a {frame.driver} frame keeps no checkpoint to archive")
    capped = CappedWriter(raw, cap)
    try:
        await frame.archive(path, capped, limit=cap)  # pyright: ignore[reportArgumentType]
    except ArchiveTooLarge as big:
        raise Oversize(big.seen) from big
    return capped.written


async def save_work(frame: FrameDriver, key: str, cap: int) -> Stored:
    with _spool() as raw, _spool() as packed:
        written = await _archive(frame, WORK, raw, cap)
        raw.seek(0)
        sha, size = await asyncio.to_thread(_gzip_and_hash, raw, packed)
        packed.seek(0)
        await storage.aput_file(key, packed, _bucket())
        return Stored(key=key, sha256=sha, size=size, raw_bytes=written)


async def save_browser(frame: FrameDriver, key: str, cap: int) -> Stored:
    with _spool() as raw, _spool() as sealed:
        written = await _archive(frame, BROWSER, raw, cap)
        raw.seek(0)
        size = await asyncio.to_thread(encrypt_stream, raw, sealed, context=key)
        sealed.seek(0)
        sha = await asyncio.to_thread(_hash, sealed)
        await storage.aput_file(key, sealed, _bucket())
        return Stored(key=key, sha256=sha, size=size, raw_bytes=written)


# What a stored object that is not an archive raises as the driver reads it (`archives.rewrite`
# opens it `r|*` before a byte reaches the frame): the object is the one the sleep stored - its
# hash matched - so no retry can change it. A failure to download, or the frame refusing a good
# archive, is anything else, and stays retryable.
_UNREADABLE: Final = (tarfile.TarError, EOFError, zlib.error, gzip.BadGzipFile)


async def _unpack(frame: FrameDriver, path: str, key: str, src: IO[bytes]) -> None:
    if not isinstance(frame, Checkpoints):  # a `none` frame is never restored
        raise PrismoidError(f"a {frame.driver} frame keeps no checkpoint to restore")
    try:
        await frame.unpack(path, src)
    except _UNREADABLE as broken:
        raise CheckpointLost(f"{key} could not be unpacked: {broken}") from broken


async def load_work(frame: FrameDriver, key: str, sha256: str) -> None:
    with _spool() as packed:
        try:
            await storage.aget_file(key, packed, _bucket())
        except storage.Missing as gone:
            raise CheckpointLost(f"{key} is missing") from gone
        packed.seek(0)
        if await asyncio.to_thread(_hash, packed) != sha256:
            raise CheckpointLost(f"{key} does not match its hash")
        await _unpack(frame, WORK, key, packed)  # `r|*` reads the gzip itself


async def load_browser(frame: FrameDriver, key: str) -> None:
    with _spool() as sealed, _spool() as plain:
        try:
            await storage.aget_file(key, sealed, _bucket())
        except storage.Missing as gone:
            raise CheckpointLost(f"{key} is missing") from gone
        sealed.seek(0)
        # The whole object opens (under this key) before a byte is unpacked.
        await asyncio.to_thread(decrypt_stream, sealed, plain, context=key)
        plain.seek(0)
        await _unpack(frame, BROWSER, key, plain)


async def copy(src_key: str, dst_key: str) -> None:
    try:
        await storage.acopy_key(src_key, dst_key, _bucket())
    except storage.Missing as gone:
        raise CheckpointLost(f"{src_key} is missing") from gone


async def delete_keys(keys: Iterable[str | None]) -> None:
    for key in keys:
        if key:
            await storage.adelete_key(key, _bucket())


async def delete_prefix(prefix: str) -> int:
    return await storage.adelete_prefix(prefix, _bucket())


async def list_prefix(prefix: str) -> list[str]:
    return await storage.alist_keys(prefix, _bucket())
