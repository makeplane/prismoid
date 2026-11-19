# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The helper's file door: `read`, `write` and `list_dir` for the worker.

Every path is resolved to its real path (symlinks included) and must lie in one of the frame's
roots. No call runs through a shell, and none runs in the sandbox, so the door mirrors the fence
itself:
- reads reach the frame's folders and its grants, never a `READ_DENY` secret inside one;
- an agent writes only its work folder, its other rw grants and `out/`; a system write may also
  reach `task/`, `artifacts/`, `skills/` and `verify/`;
- no write lands in a `.git` inside a user's folder, nor in the git folder of a repository that
  was already there (wherever it really is: a linked `.git`, a `--separate-git-dir`, a bare
  repository), but for the state files everyday git writes (`fence_data.GIT_WRITABLE_*`); nor on
  a path that runs code later (`PROTECT_WRITE`, `HOME_PROTECT_WRITE`).
- a file is at most `MAX_FILE_BYTES`, read or written;
- no write lands in a read-only grant inside a read-write one (the fence's own rule), and a
  `confined` write lands only in the work folder or `out/`, never in a read-only grant at all.

The walk to a file opens each folder from its root down without following a link, and a write
lands by rename, so a link swapped in after the check, or a hard link, is never written through.
"""

from __future__ import annotations

import base64
import contextlib
import errno
import os
import re
import secrets
import stat
from collections.abc import Generator, Sequence
from pathlib import Path
from typing import Final, Literal

from prismoid.device.fence_data import (
    GIT_WRITABLE_DIRS,
    GIT_WRITABLE_FILES,
    GIT_WRITABLE_PATTERNS,
    HOME_PROTECT_WRITE,
    PROTECT_WRITE,
    READ_DENY_PATTERNS,
)
from prismoid.device.frames import DeviceFrameState
from prismoid.device.seatbelt import home_paths, home_patterns, read_denied, under
from prismoid.schemas.device import LINK_REFUSED, FileIn

Owner = Literal["agent", "system"]

MAX_FILE_BYTES: Final = 64 * 1024 * 1024


class FileTooLarge(Exception):
    """A read or a write past `MAX_FILE_BYTES`."""


_GIT_DIRS = frozenset(d.casefold() for d in GIT_WRITABLE_DIRS)
_GIT_FILES = frozenset(n.casefold() for f in GIT_WRITABLE_FILES for n in (f, f"{f}.lock"))


def _locate(path: str, roots: Sequence[str]) -> tuple[str, list[str]]:
    """The real root `path` resolves into, and the folders and name below it."""
    if not os.path.isabs(path):
        raise PermissionError(f"not an absolute path: {path}")
    real = os.path.realpath(path)
    for root in (os.path.realpath(r) for r in roots):
        if under(real, root):
            return root, real.split("/")[len(root.split("/")) :]
    raise PermissionError(f"{path} is outside the frame's folders")


def _spelled(path: str, roots: Sequence[str]) -> tuple[str, list[str]]:
    """The real root `path` lies in as written, and the names below it: unlike `_locate`, no link
    is resolved, so the walk meets each one."""
    if not os.path.isabs(path):
        raise PermissionError(f"not an absolute path: {path}")
    clean = os.path.normpath(path)
    for root in roots:
        for spelling in (root, os.path.realpath(root)):
            if under(clean, spelling):
                return os.path.realpath(root), clean.split("/")[len(spelling.rstrip("/").split("/")) :]
    raise PermissionError(f"{path} is outside the frame's folders")


def resolve_inside(path: str, roots: Sequence[str]) -> Path:
    """`path`'s real path (symlinks resolved), or `PermissionError` when that is outside `roots`.
    Compared ignoring case: APFS and Seatbelt do."""
    root, parts = _locate(path, roots)
    return Path(root, *parts)


def read_roots(frame: DeviceFrameState) -> list[str]:
    p = frame.paths
    return [
        p.task,
        p.work,
        p.artifacts,
        p.skills,
        p.verify,
        p.out,
        *frame.granted("rw"),
        *frame.granted("ro"),
    ]


def write_roots(frame: DeviceFrameState, owner: Owner) -> list[str]:
    p = frame.paths
    agent = [p.work, *frame.granted("rw"), p.out]
    return [*agent, p.task, p.artifacts, p.skills, p.verify] if owner == "system" else agent


def _confined(frame: DeviceFrameState) -> list[str]:
    return [frame.paths.work, frame.paths.out]


def _secret(real: str) -> bool:
    if any(under(real, d) for d in read_denied()):
        return True
    return any(re.search(p, real, re.IGNORECASE) for p in home_patterns(READ_DENY_PATTERNS))


def _git_writable(parts: list[str]) -> bool:
    """A path below a pre-existing repository's git folder that the fence lets git write."""
    if parts and parts[0].casefold() in _GIT_DIRS:
        return True
    if len(parts) != 1:
        return False
    name = parts[0]
    return name.casefold() in _GIT_FILES or any(
        re.fullmatch(p, name, re.IGNORECASE) for p in GIT_WRITABLE_PATTERNS
    )


def _below(real: str, folder: str) -> list[str]:
    return real.split("/")[len(folder.split("/")) :]


def _frozen(frame: DeviceFrameState) -> list[str]:
    """The read-only grants inside a read-write one: `seatbelt._Walls.frozen`."""
    rw = frame.granted("rw")
    return [ro for ro in frame.granted("ro") if any(under(ro, root) for root in rw)]


def _write_refusal(frame: DeviceFrameState, real: str) -> str | None:
    if any(under(real, ro) for ro in _frozen(frame)):
        return f"{real} is in a read-only folder"
    if _secret(real) or any(under(real, p) for p in home_paths(HOME_PROTECT_WRITE)):
        return f"{real} is protected"
    if any(re.search(r, real, re.IGNORECASE) for r in PROTECT_WRITE):
        return f"{real} runs code later, outside the sandbox"
    # A pre-existing repository's git folder, by its real path, whatever leads there (the fence
    # judges real paths too); the innermost one rules, as the fence's last-emitted rule does.
    if owners := [git for git in frame.protected_git if under(real, git)]:
        if _git_writable(_below(real, max(owners, key=len))):
            return None
        return f"{real} is in a repository's git folder"
    for root in frame.granted("rw"):
        if under(real, root) and any(part.casefold() == ".git" for part in _below(real, root)):
            return f"{real} is in a repository's git folder"
    return None


def _open_below(fd: int, part: str, *, make: bool) -> int:
    """The folder `part` of the open folder `fd`, never through a link."""
    if make:
        with contextlib.suppress(FileExistsError):
            os.mkdir(part, 0o755, dir_fd=fd)
    try:
        return os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
    except OSError as e:
        # A link, to macOS, may be ENOTDIR here: O_DIRECTORY is checked before O_NOFOLLOW.
        link = e.errno == errno.ENOTDIR and stat.S_ISLNK(
            os.stat(part, dir_fd=fd, follow_symlinks=False).st_mode
        )
        if link or e.errno == errno.ELOOP:
            raise PermissionError(f"{LINK_REFUSED}: {part}") from e
        raise


@contextlib.contextmanager
def _folder(root: str, parts: list[str], *, make: bool = False) -> Generator[int]:
    """An fd on `root/parts...`, each folder opened from its parent without following a link."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            child = _open_below(fd, part, make=make)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def read(frame: DeviceFrameState, path: str, *, nofollow: bool = False) -> bytes:
    """`path`'s bytes. `nofollow`: a link anywhere below its root is refused, not resolved."""
    root, parts = _spelled(path, read_roots(frame)) if nofollow else _locate(path, read_roots(frame))
    if _secret(os.path.join(root, *parts)):
        raise PermissionError(f"{path} is protected")
    if not parts:
        raise IsADirectoryError(path)
    with _folder(root, parts[:-1]) as parent:
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise PermissionError(f"{LINK_REFUSED}: {path}") from e
            raise
        with os.fdopen(fd, "rb") as f:
            mode = os.fstat(f.fileno()).st_mode
            if stat.S_ISDIR(mode):
                raise IsADirectoryError(path)
            if not stat.S_ISREG(mode):  # a FIFO would hold the door open forever
                raise PermissionError(f"{path} is not a regular file")
            data = f.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise FileTooLarge(f"{path} is over {MAX_FILE_BYTES} bytes")
            return data


def list_dir(frame: DeviceFrameState, path: str) -> list[str]:
    root, parts = _locate(path, read_roots(frame))
    if _secret(os.path.join(root, *parts)):
        raise PermissionError(f"{path} is protected")
    # Directories wear a `/`, as `FrameDriver.list_dir` promises: a link is not one.
    with _folder(root, parts) as fd, os.scandir(fd) as entries:
        return sorted(f"{e.name}/" if e.is_dir(follow_symlinks=False) else e.name for e in entries)


def _mode_kept(parent: int, name: str, mode: int) -> int:
    """The mode of a regular file already at `name`, else `mode`."""
    with contextlib.suppress(OSError):
        existing = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISREG(existing.st_mode):
            return existing.st_mode & 0o777
    return mode


def write(frame: DeviceFrameState, req: FileIn) -> None:
    """Land `req` by rename. With no `mode` given, a file already there keeps its own (an agent
    rewriting its script does not lose the executable bit)."""
    if len(req.data_b64) > (MAX_FILE_BYTES // 3 + 1) * 4:
        raise FileTooLarge(f"{req.path}: over {MAX_FILE_BYTES} bytes")
    data = base64.b64decode(req.data_b64, validate=True)
    if len(data) > MAX_FILE_BYTES:
        raise FileTooLarge(f"{req.path}: over {MAX_FILE_BYTES} bytes")
    root, parts = _locate(req.path, _confined(frame) if req.confined else write_roots(frame, req.owner))
    if not parts:
        raise IsADirectoryError(req.path)
    real = os.path.join(root, *parts)
    if req.confined and any(under(real, ro) for ro in frame.granted("ro")):
        raise PermissionError(f"{real} is in a read-only folder")
    if refusal := _write_refusal(frame, real):
        raise PermissionError(refusal)
    for depth in range(1, len(parts)):  # each folder this write would make is vetted as made
        folder = os.path.join(root, *parts[:depth])
        if not os.path.lexists(folder) and (refusal := _write_refusal(frame, folder)):
            raise PermissionError(refusal)
    mode = req.mode & 0o777
    with _folder(root, parts[:-1], make=True) as parent:
        if "mode" not in req.model_fields_set:
            mode = _mode_kept(parent, parts[-1], mode)
        tmp = f".{parts[-1]}.prismoid-{secrets.token_hex(4)}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=parent)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                os.fchmod(f.fileno(), mode)
            os.replace(tmp, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=parent)
            raise
