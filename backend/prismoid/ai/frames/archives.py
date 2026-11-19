# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Streaming tar filters for the Machine's checkpoints (ADR-063).

Two directions, one boundary each:

* **What leaves a frame** (`checkpoint_filter`). `/work` passes through. `/browser` drops
  Chromium's live-process locks - `SingletonLock`/`SingletonCookie`/`SingletonSocket`, symlinks
  encoding the SOURCE host and pid, which a restored frame's Chromium reads as "another computer
  has this profile open" and refuses to start (chrome/browser/process_singleton_posix.cc:365) -
  and its caches, which otherwise inflate every archive.

* **What enters a frame** (`restore_filter`). A TRUST BOUNDARY: the agent controls `/work`, and a
  tampered object could hold anything. Only members under the directory being restored survive;
  only regular files, directories and symlinks do, plus a hard link that names a regular file
  kept earlier in the stream (devices, FIFOs, unknown types and every other hard link are
  dropped); setuid/setgid/sticky bits are cleared; PAX records are discarded (they would
  outrank the stamped owner); ownership is stamped (the agent for `/work`, the browser user for
  `/browser`, whose two top directories are forced to 0700, as
  `profile_archive.normalize_profile_headers` did); the directory itself must stay a directory
  and every name must be canonical (no empty, `.` or `..` part); and a member whose path runs
  through a symlink seen earlier in the same stream is dropped, so `work/evil -> /etc` followed
  by `work/evil/cron.d/x` can never write outside `/work`.

Both run in streaming tar mode (`r|*` in, `w|` out), and `rewrite` keeps no member list: an
archive never sits whole in memory, not even as its headers.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Callable, Iterable, Iterator
from typing import IO, Final

from prismoid.ai.frames.layout import AGENT_UID, BROWSER, BROWSER_UID

Edit = Callable[[tarfile.TarInfo], bool]

_SINGLETONS: Final = frozenset({"SingletonLock", "SingletonCookie", "SingletonSocket"})
_CACHES: Final = frozenset(
    {
        "Cache",
        "Code Cache",
        "GPUCache",
        "GrShaderCache",
        "ShaderCache",
        "GraphiteDawnCache",
        "DawnCache",
        "DawnGraphiteCache",
        "DawnWebGPUCache",
    }
)


def _root(path: str) -> str:
    return path.strip("/")


def _under(name: str, root: str) -> bool:
    return name == root or name.startswith(f"{root}/")


def checkpoint_filter(path: str) -> Edit:
    root = _root(path)
    if path != BROWSER:
        return lambda member: _under(member.name.removeprefix("./"), root)

    def keep(member: tarfile.TarInfo) -> bool:
        name = member.name.removeprefix("./")
        if not _under(name, root):
            return False
        parts = name.split("/")
        return parts[-1] not in _SINGLETONS and not any(part in _CACHES for part in parts[1:])

    return keep


def _canonical(name: str, root: str) -> bool:
    """Under `root`, and canonical: the daemon cleans `work/./evil/x` and `work//evil/x` to
    `work/evil/x`, which a check on the raw name would never see."""
    return _under(name, root) and not any(part in ("", ".", "..") for part in name.split("/"))


# What one restore may hold: the filter keeps every kept file's and symlink's name for the checks
# below, in the shared worker, and the agent chose how many names a checkpoint carries. Far above an
# honest `/work` (a large node_modules is a few hundred thousand files); past either, the stream is
# refused as unreadable, which the checkpoint store reads as a lost checkpoint: the Machine rebuilds.
RESTORE_MAX_MEMBERS = 1_000_000
RESTORE_NAME_BYTES = 64 * 1024 * 1024


class RestoreTooLarge(tarfile.TarError):
    """A checkpoint past `RESTORE_MAX_MEMBERS` members or `RESTORE_NAME_BYTES` of kept names."""


def restore_filter(path: str) -> Edit:
    root = _root(path)
    browser = path == BROWSER
    uid, uname = (BROWSER_UID, "browser") if browser else (AGENT_UID, "agent")
    private: set[str] = {root, f"{root}/profile"} if browser else set()
    links: set[str] = set()  # symlinks kept so far: nothing may be written through one
    files: set[str] = set()  # regular files kept so far: all a hard link may name
    seen = held = 0  # members read, bytes of names held in the two sets

    def through_link(name: str) -> bool:
        parts = name.split("/")
        return any("/".join(parts[:i]) in links for i in range(1, len(parts)))

    def keep(member: tarfile.TarInfo) -> bool:
        nonlocal seen, held
        seen += 1
        if seen > RESTORE_MAX_MEMBERS or held > RESTORE_NAME_BYTES:
            raise RestoreTooLarge(
                f"the checkpoint passes {RESTORE_MAX_MEMBERS:,} members or "
                f"{RESTORE_NAME_BYTES:,} bytes of names"
            )
        name = member.name.removeprefix("./").rstrip("/")
        if not _canonical(name, root) or through_link(name):
            return False
        if member.islnk():
            # Docker's tar makes the second name of a multiply-linked file a hard link (git
            # objects from a local clone, pnpm/uv caches). Kept only when it names a regular file
            # already kept in this stream, reached through no symlink - never a directory, a
            # symlink, a path outside the root, or a name the stream has not shown yet.
            target = member.linkname.removeprefix("./")
            if target not in files or through_link(target):
                return False
            member.linkname = target
        elif not (member.isfile() or member.isdir() or member.issym()):
            return False  # devices, FIFOs, and any type tarfile does not know
        if name == root and not member.isdir():
            return False  # `work -> /etc` would send every later write to `/work` into `/etc`
        if member.isfile() or member.islnk():
            files.add(name)
            held += len(name)
        else:
            files.discard(name)  # replaced: a later hard link to this name would follow it
            if member.issym():
                links.add(name)
                held += len(name)
        member.name = name
        # PAX records outrank the ustar fields below: `tarfile` copies them into the header it
        # writes, and Docker's reader applies them (`uid=0`, `uname=root`, `SCHILY.xattr.*`). A
        # tampered stream's are dropped whole; the writer re-derives any it needs (a long name, a
        # sub-second mtime) from the member itself.
        member.pax_headers = {}
        member.uid = member.gid = uid
        member.uname = member.gname = uname
        member.mode &= 0o777
        if name in private:
            member.mode = 0o700
        return True

    return keep


def rewrite(src: IO[bytes], dst: IO[bytes], edit: Edit) -> int:
    """Copy the tar stream `src` into `dst`, keeping what `edit` keeps (it may mutate a member
    it keeps). Returns how many members were kept. Only a regular file has content to pull: a
    symlink (Chromium's locks, an agent's own `ln -s`) re-added with no body stays a link -
    `extractfile()` on one hunts for a target that was never in the archive."""
    kept = 0
    with tarfile.open(fileobj=src, mode="r|*") as reader, tarfile.open(fileobj=dst, mode="w|") as writer:
        for member in reader:
            # Stream mode still appends every header to `.members`, on both ends: a checkpoint of
            # a million files would hold a million TarInfos. Neither side reads the list back.
            # (`members` is tarfile's own attribute; typeshed does not declare it.)
            reader.members = []  # pyright: ignore[reportAttributeAccessIssue]
            if not edit(member):
                continue
            writer.addfile(member, reader.extractfile(member) if member.isfile() else None)
            writer.members = []  # pyright: ignore[reportAttributeAccessIssue]
            kept += 1
    return kept


def empty_tar(dst: IO[bytes]) -> None:
    """A valid archive with nothing in it - what `archive` writes for a directory that is absent."""
    with tarfile.open(fileobj=dst, mode="w|"):
        pass


class ChunkReader(io.RawIOBase):
    """An iterator of byte chunks (Docker's `get_archive` stream) as a readable raw stream."""

    def __init__(self, chunks: Iterable[bytes]) -> None:
        self._chunks: Iterator[bytes] = iter(chunks)
        # A view, not bytes: slicing the rest of a 2 MiB chunk off on every small read would
        # copy it each time (quadratic per chunk); a view's slice is free.
        self._buf = memoryview(b"")

    def readable(self) -> bool:
        return True

    def readinto(self, target: bytearray | memoryview) -> int:  # pyright: ignore[reportIncompatibleMethodOverride]  # io hands readinto a bytearray or memoryview, the only buffers this slices
        while not self._buf:
            try:
                self._buf = memoryview(next(self._chunks))
            except StopIteration:
                return 0
        n = min(len(target), len(self._buf))
        target[:n] = self._buf[:n]
        self._buf = self._buf[n:]
        return n


__all__ = ["ChunkReader", "Edit", "checkpoint_filter", "empty_tar", "restore_filter", "rewrite"]
