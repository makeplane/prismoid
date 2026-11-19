# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Machine's checkpoint store: gzip+hash for /work, chunked Fernet for
/browser, a size cap, and typed failures a caller degrades on."""

import gzip
import hashlib
import io
import struct
import tarfile
from collections.abc import Callable
from typing import IO

import pytest

from prismoid import storage
from prismoid.operations.machines import store
from tests.support.fakes.fake_frames import FakeFrame

# A streamed tar is padded to whole 10 KiB records (`tarfile.RECORDSIZE`): one small file is already
# 10 240 bytes, so a cap that must let small archives through sits well above a record.
_CAP = 100_000


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    objects: dict[str, bytes] = {}

    def put_file(key: str, fileobj: IO[bytes], bucket: str | None = None) -> None:
        objects[key] = fileobj.read()

    def get_file(key: str, fileobj: IO[bytes], bucket: str | None = None) -> None:
        if key not in objects:
            raise storage.Missing(key)
        fileobj.write(objects[key])

    monkeypatch.setattr(storage, "put_file", put_file)
    monkeypatch.setattr(storage, "get_file", get_file)
    FakeFrame.world.clear()
    FakeFrame.created.clear()
    return objects


async def _frame(files: dict[str, bytes]) -> FakeFrame:
    frame = FakeFrame("t")
    await frame.create()
    for path, data in files.items():
        await frame.write_bytes(path, data)
    return frame


async def test_work_round_trips_through_a_gzipped_hashed_object(bucket: dict[str, bytes]) -> None:
    source = await _frame({"/work/notes.db": b"rows", "/work/.local/pkg.py": b"x = 1"})
    stored = await store.save_work(source, "machines/m/1.tar.gz", cap=_CAP)
    blob = bucket["machines/m/1.tar.gz"]
    assert blob[:2] == b"\x1f\x8b" and stored.sha256 == hashlib.sha256(blob).hexdigest()
    assert stored.size == len(blob) and stored.raw_bytes > 0
    target = await _frame({})
    await store.load_work(target, stored.key, stored.sha256)
    assert await target.read_bytes("/work/notes.db") == b"rows"


async def test_a_wrong_hash_or_a_missing_object_is_lost(bucket: dict[str, bytes]) -> None:
    source = await _frame({"/work/a": b"1"})
    stored = await store.save_work(source, "machines/m/1.tar.gz", cap=_CAP)
    with pytest.raises(store.CheckpointLost):
        await store.load_work(await _frame({}), stored.key, "0" * 64)
    with pytest.raises(store.CheckpointLost):
        await store.load_work(await _frame({}), "machines/m/9.tar.gz", stored.sha256)


async def test_an_object_that_hashes_clean_but_will_not_unpack_is_lost(
    bucket: dict[str, bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bytes are the ones the sleep stored (the hash matches), and they are
    not an archive - retrying cannot change that, so it is a lost checkpoint (a rebuild), never the
    'S3 is down' infra failure retried forever. A download that fails stays that failure."""
    for broken in (b"not a gzip at all", gzip.compress(b"not a tar either" * 64)):
        bucket["machines/m/1.tar.gz"] = broken
        with pytest.raises(store.CheckpointLost, match="could not be unpacked"):
            await store.load_work(await _frame({}), "machines/m/1.tar.gz", hashlib.sha256(broken).hexdigest())

    def down(key: str, fileobj: IO[bytes], bucket: str | None = None) -> None:
        raise ConnectionResetError("the object store went away")

    monkeypatch.setattr(storage, "get_file", down)
    with pytest.raises(ConnectionResetError):
        await store.load_work(await _frame({}), "machines/m/1.tar.gz", "0" * 64)


async def test_over_the_cap_raises_oversize(bucket: dict[str, bytes]) -> None:
    source = await _frame({"/work/big": b"x" * 50_000})
    with pytest.raises(store.Oversize) as over:
        await store.save_work(source, "machines/m/1.tar.gz", cap=10_000)
    assert over.value.seen > 10_000 and not bucket


async def test_the_browser_object_is_ciphertext_and_restores(bucket: dict[str, bytes]) -> None:
    source = await _frame({"/browser/profile/Default/Cookies": b"session=SECRET-COOKIE"})
    stored = await store.save_browser(source, "machines/m/1.browser.enc", cap=_CAP)
    blob = bucket[stored.key]
    assert b"SECRET-COOKIE" not in blob and not tarfile.is_tarfile(io.BytesIO(blob))
    target = await _frame({})
    await store.load_browser(target, stored.key)
    assert await target.read_bytes("/browser/profile/Default/Cookies") == b"session=SECRET-COOKIE"


_KEY = "machines/m/1.browser.enc"
_DATA = bytes(range(256)) * 40  # 10 240 bytes over 1 000-byte chunks: 11 frames


def _sealed(data: bytes, context: str = _KEY, chunk: int = 1_000) -> bytes:
    out = io.BytesIO()
    store.encrypt_stream(io.BytesIO(data), out, context=context, chunk=chunk)
    return out.getvalue()


def _open(sealed: bytes, context: str = _KEY) -> bytes:
    out = io.BytesIO()
    store.decrypt_stream(io.BytesIO(sealed), out, context=context)
    return out.getvalue()


def _frames(sealed: bytes) -> list[bytes]:
    """The object split back into its length-framed tokens, each with its 4-byte header."""
    frames: list[bytes] = []
    at = 0
    while at < len(sealed):
        (size,) = struct.unpack_from(">I", sealed, at)
        frames.append(sealed[at : at + 4 + size])
        at += 4 + size
    return frames


def test_chunked_encryption_round_trips_across_chunk_boundaries() -> None:
    assert len(_frames(_sealed(_DATA))) == 11 and _open(_sealed(_DATA)) == _DATA
    exact = b"x" * 2_000  # an exact multiple: the second chunk is the final one, no empty third
    assert len(_frames(_sealed(exact))) == 2 and _open(_sealed(exact)) == exact


def test_an_empty_archive_still_seals_one_final_chunk() -> None:
    sealed = _sealed(b"")
    assert len(_frames(sealed)) == 1 and _open(sealed) == b""


def test_tampered_or_truncated_ciphertext_is_lost() -> None:
    sealed = _sealed(b"cookie")
    tampered = bytearray(sealed)
    tampered[10] ^= 0xFF
    with pytest.raises(store.CheckpointLost):
        _open(bytes(tampered))
    with pytest.raises(store.CheckpointLost):
        _open(sealed[:-5])


def _drop_last(frames: list[bytes]) -> list[bytes]:
    return frames[:-1]


def _drop_middle(frames: list[bytes]) -> list[bytes]:
    return [frames[0], *frames[2:]]


def _swap_first_two(frames: list[bytes]) -> list[bytes]:
    return [frames[1], frames[0], *frames[2:]]


def _nothing(frames: list[bytes]) -> list[bytes]:
    return []


def _garbage_after_final(frames: list[bytes]) -> list[bytes]:
    return [*frames, b"junk"]


def _final_replayed(frames: list[bytes]) -> list[bytes]:
    return [*frames, frames[-1]]


@pytest.mark.parametrize(
    "mangle",
    [_drop_last, _drop_middle, _swap_first_two, _nothing, _garbage_after_final, _final_replayed],
)
def test_a_broken_sequence_is_lost(mangle: Callable[[list[bytes]], list[bytes]]) -> None:
    """Every chunk decrypts on its own; the header is what makes the SEQUENCE authentic: a cut at
    a chunk boundary, a gap, a reorder, a 0-byte object or anything after the final chunk."""
    with pytest.raises(store.CheckpointLost):
        _open(b"".join(mangle(_frames(_sealed(_DATA)))))


def test_a_chunk_sealed_for_another_key_is_lost() -> None:
    """One cipher key covers every Machine: the object key in each chunk is what stops one
    Machine's cookies opening under another's key, whole or spliced in chunk by chunk."""
    other = _sealed(_DATA, context="machines/a/1.browser.enc")
    with pytest.raises(store.CheckpointLost):
        _open(other, context="machines/b/1.browser.enc")
    spliced = [_frames(other)[0], *_frames(_sealed(_DATA))[1:]]
    with pytest.raises(store.CheckpointLost):
        _open(b"".join(spliced))


async def test_a_browser_object_copied_or_emptied_is_lost_before_unpack(bucket: dict[str, bytes]) -> None:
    source = await _frame({"/browser/profile/Default/Cookies": b"session=A"})
    stored = await store.save_browser(source, "machines/a/1.browser.enc", cap=_CAP)
    bucket["machines/b/1.browser.enc"] = bucket[stored.key]
    bucket["machines/c/1.browser.enc"] = b""
    target = await _frame({})
    for key in ("machines/b/1.browser.enc", "machines/c/1.browser.enc"):
        with pytest.raises(store.CheckpointLost):
            await store.load_browser(target, key)
    assert not target.files


class _Reads(io.BytesIO):
    """Records every read size, so a test can see what a reader asked for."""

    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.asked: list[int | None] = []

    def read(self, size: int | None = -1, /) -> bytes:
        self.asked.append(size)
        return super().read(size)


def test_a_corrupt_length_is_lost_before_it_is_read() -> None:
    """A length no chunk's token could have is refused at the header: reading it would pull the
    rest of the object into memory."""
    src = _Reads(struct.pack(">I", 0xFFFF_FFFF) + b"x" * 64)
    with pytest.raises(store.CheckpointLost):
        store.decrypt_stream(src, io.BytesIO(), context=_KEY)
    assert all(size is not None and 0 <= size <= 4 for size in src.asked)
