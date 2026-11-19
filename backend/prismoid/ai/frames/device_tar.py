# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A directory artifact (code, webapp, change) as one tar of the tree, the way the in-frame runner
makes it, but that a link is never followed: the device's file door refuses one, naming it, so what a
deliverable holds is the plain files in it.
"""

from __future__ import annotations

import io
import tarfile
from collections.abc import Awaitable, Callable

from prismoid.schemas.device import LINK_REFUSED


def _member(name: str, content: bytes | None) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.mtime = 0
    if content is None:
        info.type, info.mode = tarfile.DIRTYPE, 0o755
    else:
        info.size, info.mode = len(content), 0o644
    return info


class _Walk:
    """One tar being written, down a tree the door lists and reads."""

    def __init__(
        self,
        tf: tarfile.TarFile,
        list_or_none: Callable[[str], Awaitable[list[str] | None]],
        read: Callable[[str], Awaitable[bytes]],
        skip_links: bool,
    ) -> None:
        self.tf, self.list_or_none, self.read, self.skip_links = tf, list_or_none, read, skip_links
        self.skipped = 0

    async def folder(self, folder: str, arc: str, entries: list[str]) -> None:
        for entry in entries:
            child, child_arc = f"{folder}/{entry.rstrip('/')}", f"{arc}/{entry.rstrip('/')}"
            if entry.endswith("/"):
                sub = await self.list_or_none(child)
                self.tf.addfile(_member(child_arc, None))
                await self.folder(child, child_arc, sub or [])
            else:
                await self.file(child, child_arc)

    async def file(self, path: str, arc: str) -> None:
        try:
            content = await self.read(path)
        except PermissionError as refused:
            if not self.skip_links or LINK_REFUSED not in str(refused):
                raise
            self.skipped += 1
            return
        self.tf.addfile(_member(arc, content), io.BytesIO(content))


async def tar_tree(
    path: str,
    names: list[str],
    *,
    list_or_none: Callable[[str], Awaitable[list[str] | None]],
    read: Callable[[str], Awaitable[bytes]],
    skip_links: bool = False,
) -> tuple[bytes, int]:
    """The tar of the folder `path`, which holds `names`, and how many links it left out. `read`
    refuses a link. `skip_links`: a project's own links (`node_modules/.bin`) are left out and
    counted instead, and it publishes without them."""
    buf = io.BytesIO()
    root = path.rstrip("/")
    base = root.rsplit("/", 1)[-1]
    with tarfile.open(fileobj=buf, mode="w") as tf:
        tf.addfile(_member(base, None))
        walk = _Walk(tf, list_or_none, read, skip_links)
        await walk.folder(root, base, names)
    return buf.getvalue(), walk.skipped
