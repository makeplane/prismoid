# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""One home for "a secret in a file the user owns" (device helper key, blob door key).

Both keys are auth: a file that is empty, readable by anyone else, somebody else's, or a symlink
somewhere is not a key, and the process refuses to start rather than open its door on it. Creation
is atomic - a mode-0600 temp file, written and fsynced, then linked into place - so no starter ever
reads a half-written key and a racing starter loses cleanly. The value is cached per path after the
first good read, with the file's `lstat` fingerprint: every ask costs one `lstat` and no read, and a
key deleted, changed or opened up is a key no longer served. `forget` drops the cache, for tests.

It imports nothing from prismoid, so every layer - and the standalone device helper - may use it."""

from __future__ import annotations

import contextlib
import os
import stat
from collections.abc import Callable
from pathlib import Path


class KeyFileError(RuntimeError):
    """A key file that cannot be used. The message names the file and the fix."""


_cache: dict[Path, tuple[tuple[int, int, int, int], str]] = {}  # path -> (ino, mtime, size, mode), its key


def forget() -> None:
    _cache.clear()


def ensure(path: Path, make: Callable[[], str], *, restart: str) -> str:
    """The key in `path`, creating it (0600) when absent. `restart` names what to run again after
    the file is deleted, e.g. "start the helper again"."""
    if (cached := _cache.get(path)) is not None and _fingerprint(path) == cached[0]:
        return cached[1]
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_symlink() and not path.exists():
        _create(path, make())
    try:
        info = path.lstat()
    except OSError as err:
        raise KeyFileError(f"{path} cannot be read ({err.strerror}); delete it and {restart}") from err
    if not stat.S_ISREG(info.st_mode):
        raise KeyFileError(
            f"{path} must be a regular file, not a symlink or directory; delete it and {restart}"
        )
    if info.st_uid != os.getuid():
        raise KeyFileError(f"{path} must be owned by you; delete it and {restart}")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise KeyFileError(f"{path} must be mode 0600; run `chmod 600 {path}`")
    key = path.read_text().strip()
    if not key:
        raise KeyFileError(f"{path} is empty; delete it and {restart}")
    _cache[path] = (_print(info), key)
    return key


def _print(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_ino, info.st_mtime_ns, info.st_size, info.st_mode


def _fingerprint(path: Path) -> tuple[int, int, int, int] | None:
    """The file as it is now, without following a link; None when it cannot be seen."""
    try:
        return _print(path.lstat())
    except OSError:
        return None


def _create(path: Path, value: str) -> None:
    tmp = path.parent / f".{path.name}.{os.getpid()}.{os.urandom(4).hex()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(value)
            f.flush()
            os.fsync(f.fileno())
        with contextlib.suppress(FileExistsError):
            os.link(tmp, path)  # atomic; loses cleanly to a racing starter
    finally:
        tmp.unlink(missing_ok=True)
