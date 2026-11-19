# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The content-addressed skill loader.

The loader is the thing that makes the edit loop fast and the snapshot small: a body is fetched from
the store once and then served from a local cache keyed by its own hash. These tests pin the two
properties that matter - a hit never touches the store, and a hit is never *wrong* - with the store
itself faked, because the loader's logic (cache, verify, heal) is what is under test, not MinIO.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from prismoid import storage
from prismoid.ai.skills.loader import SkillLoader

_BODY = b"You write prose a busy reader finishes.\n"
_SHA = storage.sha256(_BODY)


@pytest.fixture
def fake_store(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A stand-in for the object store that records every fetch, so a test can assert a cache hit
    did NOT reach it. Returns the list of hashes fetched."""
    fetched: list[str] = []

    def _get(digest: str, bucket: str | None = None) -> bytes:
        fetched.append(digest)
        if digest == _SHA:
            return _BODY
        raise KeyError(digest)

    monkeypatch.setattr(storage, "get", _get)
    return fetched


def test_first_load_is_a_miss_second_is_a_hit(tmp_path: Path, fake_store: list[str]) -> None:
    loader = SkillLoader(cache_dir=tmp_path)

    first = loader.load(_SHA)
    assert first.data == _BODY
    assert first.cache_hit is False
    assert fake_store == [_SHA]  # went to the store once

    second = loader.load(_SHA)
    assert second.data == _BODY
    assert second.cache_hit is True
    assert fake_store == [_SHA]  # and NOT again - the whole point


def test_is_cached_tracks_state(tmp_path: Path, fake_store: list[str]) -> None:
    loader = SkillLoader(cache_dir=tmp_path)
    assert loader.is_cached(_SHA) is False
    loader.load(_SHA)
    assert loader.is_cached(_SHA) is True


def test_a_corrupt_cache_file_is_a_miss_and_heals(tmp_path: Path, fake_store: list[str]) -> None:
    loader = SkillLoader(cache_dir=tmp_path)
    loader.load(_SHA)  # populate

    # Something rots the cached bytes. `is_cached` must not vouch for them, and `load` must refetch.
    (tmp_path / _SHA).write_bytes(b"corrupted")
    assert loader.is_cached(_SHA) is False

    healed = loader.load(_SHA)
    assert healed.data == _BODY
    assert healed.cache_hit is False  # it went back to the store
    assert loader.is_cached(_SHA) is True  # and re-cached the good bytes


def test_store_bytes_that_do_not_match_their_hash_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Content-addressing is a claim the loader must actually check: if the store hands back bytes
    whose sha is not the key, returning them would defeat the whole guarantee. It raises instead."""

    def _lying_get(digest: str, bucket: str | None = None) -> bytes:
        return b"these are not the droids you asked for"

    monkeypatch.setattr(storage, "get", _lying_get)
    loader = SkillLoader(cache_dir=tmp_path)
    with pytest.raises(ValueError, match="does not match its own hash"):
        loader.load(_SHA)


def test_two_steps_missing_the_cache_at_once_both_get_the_body(
    tmp_path: Path, fake_store: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two steps of one wave can load the same skill in the same instant (a writer pair 16 ms
    apart): both miss, and with one shared temp name the first rename takes it and the second
    raises FileNotFoundError - failing the whole task. Each writer must own
    its temp file, so the second writer's rename still finds its own."""
    loader = SkillLoader(cache_dir=tmp_path)
    original = Path.replace
    raced = False

    def _replace_after_a_rival(self: Path, target: Path) -> Path:
        nonlocal raced
        if not raced:  # the rival lands between this writer's write and its rename
            raced = True
            assert SkillLoader(cache_dir=tmp_path).load(_SHA).data == _BODY
        return original(self, target)

    monkeypatch.setattr(Path, "replace", _replace_after_a_rival)

    assert loader.load(_SHA).data == _BODY
    assert (tmp_path / _SHA).read_bytes() == _BODY
    assert [p.name for p in tmp_path.iterdir()] == [_SHA]  # no temp file left behind


def test_a_write_that_fails_leaves_no_temp_file_behind(
    tmp_path: Path, fake_store: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk or a killed write must not strand a half-written temp file in the cache: the
    cache directory lives as long as the host, so every failed miss would add one forever."""
    loader = SkillLoader(cache_dir=tmp_path)

    def _disk_full(self: Path, target: Path) -> Path:
        raise OSError("No space left on device")

    monkeypatch.setattr(Path, "replace", _disk_full)

    with pytest.raises(OSError, match="No space left"):
        loader.load(_SHA)
    assert list(tmp_path.iterdir()) == []
