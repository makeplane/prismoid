# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The skill body loader - a content-addressed local cache in front of the object store.

A skill body is stored in the skill bucket under its own sha256 (`operations/skills._write_version`).
The loader reads it back **by that hash, through a local cache keyed by the same hash**. Content-
addressing is what makes the cache trustworthy: the bytes filed under sha X are the bytes whose sha is
X, or they are corrupt and caught on read - there is no staleness to invalidate. An edited skill is a
*new* body with a *new* hash and a *new* version row, so it misses and is fetched; an unchanged skill
hits and never touches the network. That hit is half of the sub-60-second edit loop and
the reason `/skills` is excluded from snapshots (`layout.py`) - the body is re-fetchable, so it is
never stored twice.

This module lives in `ai/skills`, a **sibling** of `ai/frames`, so it may not reach a frame - it
hands *bytes* back to `operations`, which owns the frame and does the copy. It may import `storage` (a
lower layer), which is how it reaches the object store without importing `db`.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import logfire

from prismoid import storage
from prismoid.ai import config


class LoadedBody(NamedTuple):
    """A body and whether it came from the local cache - so a caller (the `pull` CLI) can say which,
    without the loader having to hold mutable per-call state."""

    data: bytes
    cache_hit: bool


class SkillLoader:
    """A local content-addressed cache in front of the skill bucket.

    Keyed by content hash, so the cache can never go stale and there is no invalidation to get wrong -
    which is the entire reason skills are addressed by hash rather than by (name, version) on disk.
    """

    def __init__(self, cache_dir: Path | None = None, *, bucket: str | None = None) -> None:
        self._dir = cache_dir or (Path.home() / ".cache" / "prismoid" / "skills")
        self._bucket = bucket or config.current().worker.skill_bucket

    def is_cached(self, body_sha256: str) -> bool:
        """Present *and* valid. A cached file whose bytes no longer hash to their own name is not a
        hit - it is corruption, and reporting it as cached would make the `pull` message lie."""
        cached = self._dir / body_sha256
        return cached.is_file() and storage.sha256(cached.read_bytes()) == body_sha256

    def load(self, body_sha256: str) -> LoadedBody:
        """The body for a hash - from the local cache if present and valid, else the store (and cached
        on the way). Verified on both paths: bytes that do not hash to their own name are never
        returned, so a corrupt cache heals instead of poisoning every task that loads the skill.
        """
        cached = self._dir / body_sha256
        if cached.is_file():
            data = cached.read_bytes()
            if storage.sha256(data) == body_sha256:
                logfire.info("skill cache hit {sha}", sha=body_sha256[:12])
                return LoadedBody(data, cache_hit=True)
            logfire.warn("skill cache corrupt for {sha} - refetching", sha=body_sha256[:12])

        data = storage.get(body_sha256, bucket=self._bucket)
        if storage.sha256(data) != body_sha256:
            raise ValueError(f"skill body for {body_sha256[:12]}… does not match its own hash")

        self._dir.mkdir(parents=True, exist_ok=True)
        # Write to a temp name then replace, so a crash mid-write never leaves a short file that a
        # later `is_cached` would then have to catch - atomic on the same filesystem. The temp name
        # is this writer's own: two steps of one wave miss together, and a shared name let the first
        # rename take the second writer's file (FileNotFoundError failed the whole task).
        fd, name = tempfile.mkstemp(dir=self._dir, prefix=f".{body_sha256}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as tmp:
                tmp.write(data)
            Path(name).replace(cached)
        except BaseException:
            # The cache lives as long as the host, so a failed write must not strand its temp file.
            Path(name).unlink(missing_ok=True)
            raise
        logfire.info("skill cache miss {sha} - fetched {n}B", sha=body_sha256[:12], n=len(data))
        return LoadedBody(data, cache_hit=False)


@lru_cache(maxsize=1)
def default_loader() -> SkillLoader:
    """The process-wide loader (one cache dir per host). `lru_cache` gives one instance without a
    module-level mutable global."""
    return SkillLoader()


async def read_body(body_sha256: str) -> bytes:
    """A body by its content hash, off the event loop: the read port `skill_read` is handed."""
    return (await asyncio.to_thread(default_loader().load, body_sha256)).data
