# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The filesystem backend (ADR-077) behind the unchanged `prismoid.storage` API."""

from __future__ import annotations

import datetime as dt
import io
import os
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from prismoid import keyfile, storage, storage_fs
from prismoid.conf import settings


@pytest.fixture(autouse=True)
def fs_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setattr(settings.worker, "storage_backend", "fs")
    monkeypatch.setattr(settings.local, "home", tmp_path)
    keyfile.forget()
    return tmp_path


def test_fs_roundtrip_content_addressed(fs_backend: Path) -> None:
    digest = storage.put(b"hello")
    assert digest == storage.sha256(b"hello")
    assert (fs_backend / "objects" / "artifacts" / storage.key_for(digest)).read_bytes() == b"hello"
    assert storage.get(digest) == b"hello" and storage.exists(digest)
    assert storage.put(b"hello") == digest  # idempotent
    storage.delete(digest)
    storage.delete(digest)  # a missing key is success
    assert not storage.exists(digest)
    with pytest.raises(storage.Missing):
        storage.get(digest)


async def test_the_async_twins_run_the_same_calls(fs_backend: Path) -> None:
    """Async code reaches the store through the `a` twins; each one is the blocking call it names."""
    digest = await storage.aput(b"twin")
    assert await storage.aget(digest) == b"twin" == storage.get(digest)
    await storage.aput_file("t/a", io.BytesIO(b"file"), "snapshots")
    await storage.acopy_key("t/a", "t/b", "snapshots")
    assert await storage.alist_keys("t/", "snapshots") == ["t/a", "t/b"]
    assert [k for k, _ in await storage.alist_objects("t/", "snapshots")] == ["t/a", "t/b"]
    assert await storage.alast_modified("t/a", "snapshots") is not None
    out = io.BytesIO()
    await storage.aget_file("t/b", out, "snapshots")
    assert out.getvalue() == b"file"
    await storage.adelete_key("t/a", "snapshots")
    assert await storage.adelete_prefix("t/", "snapshots") == 1
    await storage.adelete(digest)
    assert not storage.exists(digest)


def test_fs_writes_are_atomic_and_leave_no_temp(fs_backend: Path) -> None:
    storage.put_file("m/a", io.BytesIO(b"x" * 10), "snapshots")
    names = [p.name for p in (fs_backend / "objects" / "snapshots" / "m").iterdir()]
    assert names == ["a"]


def test_fs_keys_list_prefix_delete() -> None:
    for key in ("m/2/b", "m/1/z", "m/1/a", "other/x", "m1"):
        storage.put_file(key, io.BytesIO(key.encode()), "snapshots")
    assert storage.list_keys("m/", "snapshots") == ["m/1/a", "m/1/z", "m/2/b"]  # S3's order
    assert storage.list_keys("m", "snapshots") == ["m/1/a", "m/1/z", "m/2/b", "m1"]  # string prefix
    assert storage.list_keys("m/1/a", "snapshots") == ["m/1/a"]
    listed = storage.list_objects("m/1/", "snapshots")
    assert [k for k, _ in listed] == ["m/1/a", "m/1/z"]
    assert all(t.tzinfo is not None for _, t in listed)
    stamp = storage.last_modified("m/1/a", "snapshots")
    assert isinstance(stamp, dt.datetime) and stamp.tzinfo is not None
    assert storage.last_modified("nope", "snapshots") is None
    storage.copy_key("m/1/a", "copy/a", "snapshots")
    assert storage.list_keys("copy/", "snapshots") == ["copy/a"]
    assert storage.delete_prefix("m/", "snapshots") == 3
    assert storage.list_keys("m/", "snapshots") == []
    storage.delete_key("other/x", "snapshots")
    storage.delete_key("other/x", "snapshots")
    assert storage.list_keys("", "snapshots") == ["copy/a", "m1"]


def test_fs_get_file_missing_raises_missing() -> None:
    with pytest.raises(storage.Missing):
        storage.get_file("nope", io.BytesIO(), "snapshots")
    with pytest.raises(storage.Missing):
        storage.copy_key("nope", "dst", "snapshots")


def test_fs_get_file_streams_into_the_handle() -> None:
    storage.put_file("k", io.BytesIO(b"abc" * 1000), "snapshots")
    out = io.BytesIO()
    storage.get_file("k", out, "snapshots")
    assert out.getvalue() == b"abc" * 1000


@pytest.mark.parametrize("key", ["../x", "a/../../x", "/etc/passwd", "", "a//b", "a/./b"])
def test_fs_keys_cannot_leave_the_bucket(key: str) -> None:
    with pytest.raises(ValueError):
        storage.put_file(key, io.BytesIO(b"x"), "snapshots")


def test_fs_bucket_names_are_plain() -> None:
    with pytest.raises(ValueError):
        storage.put_file("k", io.BytesIO(b"x"), "../evil")


def test_fs_a_symlink_out_of_the_bucket_is_refused(
    fs_backend: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret").write_text("s")
    storage.put_file("seed", io.BytesIO(b"x"), "snapshots")
    os.symlink(outside, fs_backend / "objects" / "snapshots" / "link")
    with pytest.raises(ValueError):
        storage.get_file("link/secret", io.BytesIO(), "snapshots")


def test_a_deleted_or_changed_key_is_no_longer_served(fs_backend: Path) -> None:
    """The file is the key: a link stops verifying as soon as it goes, without a restart."""
    signed = ("a" * 64, "artifacts", "a.txt", False, "text/plain")
    signature = storage_fs.sign(*signed, exp=2_000_000_000)
    path = fs_backend / "blob.key"
    first = path.read_text()
    assert storage_fs.blob_key() == first.encode()

    path.unlink()

    assert storage_fs.sign(*signed, exp=2_000_000_000) != signature, "the old key is gone, cached or not"
    assert path.read_text() != first, "and a new one stands in its place"

    replaced = fs_backend / "blob.key.new"
    replaced.write_text("c" * 64)
    replaced.chmod(0o600)
    replaced.replace(path)
    assert storage_fs.blob_key() == b"c" * 64


def test_a_cached_key_opened_up_is_refused_at_the_next_read(fs_backend: Path) -> None:
    """A `chmod` changes nothing a read would see but the mode, and the mode is the law."""
    keyfile.forget()
    key = storage_fs.blob_key()
    path = fs_backend / "blob.key"
    path.chmod(0o644)
    with pytest.raises(storage_fs.BlobKeyError, match="chmod 600"):
        storage_fs.blob_key()
    path.chmod(0o600)
    assert storage_fs.blob_key() == key


def test_blob_key_is_0600_and_fails_closed(fs_backend: Path) -> None:
    key = storage_fs.blob_key()
    path = fs_backend / "blob.key"
    assert len(key) >= 32 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert storage_fs.blob_key() == key
    keyfile.forget()
    path.chmod(0o644)
    with pytest.raises(storage_fs.BlobKeyError, match="chmod 600"):
        storage_fs.blob_key()
    path.chmod(0o600)
    path.write_text("")
    with pytest.raises(storage_fs.BlobKeyError, match="is empty"):
        storage_fs.blob_key()
    path.unlink()
    real = fs_backend / "elsewhere"
    real.write_text("k")
    real.chmod(0o600)
    path.symlink_to(real)
    with pytest.raises(storage_fs.BlobKeyError, match="regular file"):
        storage_fs.blob_key()


def test_blob_key_is_read_once_per_path(fs_backend: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = storage_fs.blob_key()

    def no_second_read(_: Path) -> str:
        raise AssertionError("the file did not change: its key is the cache's")

    monkeypatch.setattr(Path, "read_text", no_second_read)
    assert storage_fs.blob_key() == first


def test_presign_fs_points_at_blob_door() -> None:
    digest = storage.put(b"pdf")
    url = storage.presign(digest, "my file.pdf", inline=True, media="application/pdf", expires=60)
    parts = urlsplit(url)
    assert url.startswith(settings.api.public_base_url + f"/v1/blobs/{digest}?")
    q = {k: v[0] for k, v in parse_qs(parts.query).items()}
    assert q["bucket"] == "artifacts" and q["name"] == "my file.pdf" and q["inline"] == "1"
    assert q["media"] == "application/pdf" and q["sig"]
    assert 0 < int(q["exp"]) - int(dt.datetime.now(dt.UTC).timestamp()) <= 61


def test_signature_message_is_unambiguous() -> None:
    now = 1_000_000
    a = storage_fs.sign("d", "b", "xy", False, "", exp=now + 60)
    b = storage_fs.sign("d", "b", "x", False, "y", exp=now + 60)
    assert a != b  # plain concatenation would collide


def test_expiry_is_capped_at_the_s3_ceiling() -> None:
    digest = storage.put(b"x")
    q = parse_qs(urlsplit(storage.presign(digest, "f", expires=10**9)).query)
    assert int(q["exp"][0]) - dt.datetime.now(dt.UTC).timestamp() <= storage_fs.MAX_TTL_S + 1


def test_checked_paths_are_the_resolved_ones(fs_backend: Path) -> None:
    storage.put_file("real/a", io.BytesIO(b"x"), "snapshots")
    os.symlink(fs_backend / "objects" / "snapshots" / "real", fs_backend / "objects" / "snapshots" / "alias")
    got = storage_fs.object_path("snapshots", "alias/a")
    assert got == Path(os.path.realpath(fs_backend / "objects" / "snapshots" / "real" / "a"))


def test_only_the_presigned_bucket_is_servable() -> None:
    assert storage_fs.buckets() == frozenset({settings.worker.artifact_bucket})
