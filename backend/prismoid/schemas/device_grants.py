# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a person grants a task on the device: the grant's vocabulary, the
virtual root a folder wears in a frame, and a folder's identity for "is it busy". Values
and pure rules, read by the server, the CLI and the helper alike."""

from __future__ import annotations

import posixpath
import re
import unicodedata
import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from pydantic import BaseModel


class GrantKind(StrEnum):
    FOLDER = "folder"
    DOCKER = "docker"


class GrantMode(StrEnum):
    RO = "ro"
    RW = "rw"


class FolderGrant(BaseModel):
    path: str
    mode: Literal["ro", "rw"]
    # Its virtual root, minted once by the server when the grant was made and stored on its row
    # (`mint_grant_name`): `/work` for the task's work folder, `/grants/<name>` for any other. Never
    # re-derived and never reused in the task, so it never points at another folder.
    name: str | None = None
    # A rw folder that is a git worktree (its `.git` a `gitdir:` pointer): the worktree's own git
    # folder and its repository's common one, real paths. Only git's state files are writable there
    # (the fence's protected-git allow-list); the helper derives them again and refuses a grant
    # whose list differs.
    git: list[str] = []


@dataclass(frozen=True, slots=True)
class FolderHolder:
    """The live task holding a folder read-write, as a claim is told it. A value, not a row: a disk
    has one set of folders, so the holder may sit in another workspace, and its row stays out of this
    unit of work. `revoked`: the grant was taken back, and a command it started may still write."""

    task_id: uuid.UUID
    workspace_id: uuid.UUID
    creator_actor_id: uuid.UUID
    label: str
    path: str
    revoked: bool


# The virtual root of the task's work folder, and of every other folder it is granted.
WORK_NAME: Final = "/work"
GRANTS_ROOT: Final = "/grants"
_GRANT_NAME = re.compile(r"^/grants/[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}$")


def mint_grant_name(real: str, taken: Collection[str]) -> str:
    """A new grant's virtual root: `/grants/` and the folder's own name made safe (`[A-Za-z0-9._-]`,
    anything else `-`, at most 48 characters), `_2`, `_3`... when the task already used that name
    (ignoring case) - `taken` holds every name it ever had, revoked ones too, so a name never comes
    back pointing at another folder. Minted once, when the grant is made, and stored."""
    used = {t.casefold() for t in taken}
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", posixpath.basename(real.rstrip("/"))).strip("-.")[:48] or "folder"
    name, n = f"{GRANTS_ROOT}/{base}", 1
    while name.casefold() in used:
        n += 1
        name = f"{GRANTS_ROOT}/{base}_{n}"
    return name


def grant_name_ok(name: str) -> bool:
    return name == WORK_NAME or bool(_GRANT_NAME.match(name))


def named_grants(grants: Sequence[FolderGrant]) -> list[tuple[str, FolderGrant]]:
    """Each grant with its virtual root. The server names every grant; one told without a name (a
    test, a direct helper caller) is named here: the first such read-write one `/work` when no grant
    holds it, any other one minted against the names in use."""
    taken = [g.name for g in grants if g.name]
    out: list[tuple[str, FolderGrant]] = []
    for g in grants:
        name = g.name
        if name is None:
            first_work = g.mode == "rw" and WORK_NAME not in taken
            name = WORK_NAME if first_work else mint_grant_name(g.path, taken)
            taken.append(name)
        out.append((name, g))
    return out


def folder_key(real: str) -> str:
    """The one identity of a folder for "is it busy": its real path, Unicode-composed
    (NFC: a typed name and `getcwd`'s decomposed one are the same folder) and case-folded (APFS
    ignores case, non-ASCII capitals included). Computed here, in Python, and stored: SQL compares
    keys and never folds case itself."""
    return unicodedata.normalize("NFC", real.rstrip("/") or "/").casefold()


def key_ancestors(key: str) -> list[str]:
    """Every folder key above `key`, `/` excluded: `/a/b/c` -> [`/a/b`, `/a`]."""
    out: list[str] = []
    parent = posixpath.dirname(key)
    while parent not in ("/", ""):
        out.append(parent)
        parent = posixpath.dirname(parent)
    return out


# Inside ~/Library only the synced folders people keep work in may be granted (iCloud Drive and
# the File Provider clouds: Dropbox, Google Drive, OneDrive).
_GRANTABLE_IN_LIBRARY = ("mobile documents", "cloudstorage")


def _parts(path: str) -> list[str]:
    return [p for p in posixpath.normpath(path).casefold().split("/") if p]


def write_root_refusal(path: str, home: str) -> str | None:
    """Why a user-granted folder may not be a write root, or None: the one rule the fence and the
    server's folder check share. `path` and `home` are real, absolute paths. Compared ignoring case:
    macOS volumes and Seatbelt are case-insensitive, so `~/library` IS `~/Library`."""
    folder, root = _parts(path), _parts(home)
    if root[: len(folder)] == folder:
        return f"{path} holds the home folder"
    if folder[: len(root)] != root:
        if folder[:1] == ["volumes"] and len(folder) >= 3:  # a folder on an external disk
            return None
        return f"{path} is outside the home and external disks (/Volumes/<disk>/...)"
    first, rest = folder[len(root)], folder[len(root) + 1 :]
    if first.startswith("."):
        return f"{path} is, or is in, a hidden folder of the home (shell, editor and tool config)"
    if first == "applications":
        return f"{path} is in ~/Applications (apps run from there)"
    if first == "library" and not (rest[:1] and rest[0] in _GRANTABLE_IN_LIBRARY and rest[1:]):
        return f"{path} is in ~/Library (launch agents, app plugins and settings live there)"
    return None
