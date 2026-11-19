# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a fenced command may not leave in a person's folder: a git
repository, or a link out of the granted folders.

The fence refuses a `.git` written in a user root, but a repository can still arrive: made under
`$TMPDIR` and moved in, as plain files (`HEAD`, `objects/`, `refs/` - no `.git` in any name), or
through a link. Each runs code later, outside the sandbox (its hooks, its config's
`core.fsmonitor`, when the person runs git there), and a link out of the granted folders is a path
the person's own tools write through. So after every command, and when the frame's services stop or
it ends, the helper walks the frame's read-write grants and moves what is NEW of these to
`home/quarantine/<ref>/<n>/item/`, beside a manifest naming it by its virtual path; the refusal is
kept in the frame's denials and the agent is told.

**What is new.** Everything is known by identity, never by path alone, so a person's entry survives
any rename of a folder above it: a `.git` by its inode, a repository's shape file by its inode and
its folder's, a link by inode and target, or by its path and target: `ln -sfn` remakes one with the
same meaning and a new inode. What a grant holds when it is made is the person's (`baseline`); a new
inode at a protected path is a plant like any other. The inventory persists
beside the frame (0600), so a helper that died before its walk finds the plant at its next one. A
Python venv's interpreter link (`bin/python*`, beside `pyvenv.cfg`, to an executable) is a venv's,
not a plant: `uv venv` makes one in every Python project.

**What is a repository.** More than git takes for one, never less: an entry named `HEAD`, and one
named `objects` and one named `refs` (either may be `commondir`), of any type and any content - at a
grant's root too. A `.git` of any type is one as well (a FIFO, a socket). Its folder is never moved:
it may have been the person's before the shape arrived, and may hold a protected repository. Its
new shape files go, one by one, which is what stops it being a repository.

**What only a container can add.** A frame with the Docker grant has writers no fence holds, so there
what `PROTECT_WRITE` names is judged too (`.envrc`, `.vscode`, `.idea`, a shell's rc file, a command
could never write these): by its path in the grant, and what the person has is laid read-only over
a container's rw bind (`later`, `docker_policy._overlays`).

**The walk** goes by file descriptor (a path past PATH_MAX is walked), follows no link and enters no
git folder; a folder of a repository's shape is walked like any other, so being known hides nothing. A folder it may not list is restored to `u+rwx` and listed; one that still cannot be is
a plant (fails closed), and a listing that failed is never remembered. A folder closed when the walk
first met it is the person's: it stays closed, and unread. It is bounded by change: a
folder whose device, inode and ctime (which no command can set) are as the last walk saw them is not
listed again, only stat'ed - after the first walk of a grant, a walk is one `stat` per folder.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import errno
import fcntl
import json
import logging
import os
import re
import secrets
import shutil
import stat
import sys
import threading
from collections.abc import Generator, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from prismoid.device.fence_data import PROTECT_WRITE
from prismoid.device.seatbelt import under

LOG = logging.getLogger(__name__)

QUARANTINE: Final = "quarantine"
MANIFEST: Final = "manifest.json"
ITEM: Final = "item"  # a plant's own name can never be the manifest's

# What an entry is, whatever its path: (its kind or shape name, its inode, a link's target - or,
# for a shape file, its folder's inode: three files gathered from three folders are new).
Identity = tuple[str, int, str]
# In every inventory written since `runs` entries were kept: one without it never looked for them.
_RUNS_KEPT: Identity = ("runs-kept", 0, "")

REASONS: Final[dict[str, str]] = {
    "git": (
        "a git repository may not be added inside a granted folder (its hooks and config would run "
        "outside the sandbox): work in the folder's own repository, or make new ones under $TMPDIR"
    ),
    "bare": (
        "a bare git repository may not be added inside a granted folder (its hooks would run outside "
        "the sandbox): make it under $TMPDIR"
    ),
    "link": "a link out of the granted folders may not be added inside one: copy what you need instead",
    "opaque": "a folder that cannot be listed may not be left inside a granted folder",
    "runs": (
        "a file that runs code when its folder is opened may not be added inside a granted folder "
        "(a container has no fence to refuse it): the person adds it"
    ),
}
_VENV_PYTHON = re.compile(r"(python|pypy)[0-9.]*", re.IGNORECASE)
_RUNS = re.compile("|".join(PROTECT_WRITE), re.IGNORECASE)
_SHAPE: Final = frozenset({"head", "objects", "refs", "commondir", "config", "hooks"})
_REFUSED: Final = (errno.EACCES, errno.EPERM)
_GONE: Final = (errno.ENOENT, errno.ENOTDIR, errno.ELOOP)
_ANCHOR_AT: Final = 512  # bytes of path from an open folder, well inside PATH_MAX (1024 here)
_PASSES: Final = 4  # a walk again after a move: what the moved shape kept it out of
_OPEN_DIR: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


@dataclass(frozen=True)
class Planted:
    virtual: str  # where it was, as the agent names it (`/work/sub/.git`)
    kind: str
    moved: bool  # False: it could not be moved, and is tried again at the next walk

    @property
    def reason(self) -> str:
        return (
            REASONS[self.kind] if self.moved else f"{REASONS[self.kind]} (it could not be moved: remove it)"
        )

    @property
    def notice(self) -> str:
        if self.moved:
            return f"prismoid: moved {self.virtual} out of the folder: {REASONS[self.kind]}."
        return f"prismoid: {self.virtual} must go: {REASONS[self.kind]}."


@dataclass(frozen=True)
class _Found:
    path: str
    kind: str  # "git", "link", "opaque", "runs", or a shape file's name
    identity: Identity
    inode: int = 0  # where its identity holds none (`_runs_later`)
    also: Identity | None = None  # a second way to know it (`_list`'s links)

    @property
    def names(self) -> set[Identity]:
        return {self.identity, self.also} if self.also is not None else {self.identity}


@dataclass(frozen=True)
class _Seen:
    stamp: tuple[int, int, int]  # (device, inode, ctime) of the folder when it was listed
    subdirs: tuple[str, ...]
    found: tuple[_Found, ...]
    shaped: bool  # a repository's names are in this folder


@dataclass
class _Walk:
    seen: dict[str, _Seen] = field(default_factory=dict)
    opaque: list[_Found] = field(default_factory=list)
    whole: bool = True  # every folder was listed


class _Anchor:
    """An open folder the walk's paths are relative to, closed when no folder pending needs it."""

    def __init__(self, fd: int, path: str) -> None:
        self.fd, self.path, self.users = fd, path, 0

    def rel(self, path: str) -> str:
        return path[len(self.path) + 1 :] or "."

    def let_go(self) -> None:
        self.users -= 1
        if self.users <= 0:
            os.close(self.fd)


def _shaped(entries: dict[str, os.DirEntry[str]]) -> bool:
    """Names alone, ignoring case as APFS does: `HEAD`, and each of `objects` and `refs` or a
    `commondir` in its place. Nothing is opened, read or asked of any of them, on purpose: git's own
    test (`access`, not a folder; a link's text; `commondir`'s chain) is one a command can arrange to
    pass, so this is looser than git, and `test_every_shape_git_takes_is_caught` holds it there."""
    common = "commondir" in entries
    return "head" in entries and ("objects" in entries or common) and ("refs" in entries or common)


def _venv_python(link: str, target: str) -> bool:
    folder = os.path.dirname(link)
    if os.path.basename(folder).casefold() != "bin" or not _VENV_PYTHON.fullmatch(os.path.basename(link)):
        return False
    if not os.path.isfile(os.path.join(os.path.dirname(folder), "pyvenv.cfg")):
        return False
    return os.path.isfile(target) and os.access(target, os.X_OK)


class Plants:
    """One frame's inventory of the repositories and outward links in its read-write grants, and
    the walk that moves what is new. One walk at a time (a lock): commands of a frame may overlap."""

    def __init__(self, path: Path, home: Path, ref: str) -> None:
        self.path = path
        self.home = home
        self.ref = ref
        self._known: dict[str, set[Identity]] = {}  # per root (real path): the person's
        self._dirs: dict[str, dict[str, _Seen]] = {}  # per root: each folder as the last walk saw it
        self._inward: frozenset[str] = frozenset()  # what a link may name (cached in `_dirs`)
        self._lock = threading.Lock()
        with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
            raw = json.loads(path.read_text())
            self._known = {
                str(root): {(str(k), int(i), str(t)) for k, i, t in items} for root, items in raw.items()
            }

    def baseline(self, roots: Sequence[str], inward: Iterable[str] = ()) -> list[str]:
        """A grant is made: what its folder holds now is the person's. Roots already known keep
        their inventory (a re-made frame with the same grants), roots no longer granted are
        dropped. Returns the repositories found in the roots new here, for the fence."""
        repositories: list[str] = []
        with self._lock:
            before = {root: set(items) for root, items in self._known.items()}
            self._judge_links_by(roots, inward)
            for root in roots:
                if root not in self._known:
                    walk = self._walk(root)
                    self._dirs[root] = walk.seen
                    self._known[root] = _identities(walk)
                    repositories += [
                        *(f.path for s in walk.seen.values() for f in s.found if f.kind == "git"),
                        *(folder for folder, s in walk.seen.items() if s.shaped and folder != root),
                    ]
            for root in set(self._known) - set(roots):
                del self._known[root]
                self._dirs.pop(root, None)
            self._save(before)
        return repositories

    def sweep(
        self,
        roots: Sequence[tuple[str, str]],
        protected: Sequence[str],
        inward: Iterable[str] = (),
        *,
        containers: bool = False,
    ) -> list[Planted]:
        """Walk the read-write grants (virtual root, real root); move what is new to quarantine,
        each on its own: one that will not move never stops the next. `protected` is the list the
        fence holds: an unlistable folder holding one is never moved, nor its own files changed.
        A root never walked before is baselined, not swept. `containers`: something unfenced can
        write these folders, so what `PROTECT_WRITE` names is judged too."""
        real_roots = [real for _, real in roots]
        planted: list[Planted] = []
        with self._lock:
            before = {root: set(items) for root, items in self._known.items()}
            self._judge_links_by(real_roots, inward)
            for virtual_root, root in roots:
                planted += self._sweep(virtual_root, root, protected, containers)
            self._save(before)
        return planted

    def later(self) -> tuple[str, ...]:
        """The person's entries `PROTECT_WRITE` names, for the Docker proxy to lay read-only over a
        container's rw bind: an overlay is what keeps a container out of what is already there. A
        link has no file to lay. Off the walk's lock: a walk only ever replaces what this reads."""
        held = list(self._known.items())
        paths = (root + rel for root, items in held for kind, _, rel in items if kind == "runs")
        return tuple(sorted(p for p in paths if not os.path.islink(p)))

    def _sweep(
        self, virtual_root: str, root: str, protected: Sequence[str], containers: bool
    ) -> list[Planted]:
        if (known := self._known.get(root)) is None:  # a root never walked: as it is now, the person's
            walk = self._walk(root)
            self._known[root], self._dirs[root] = _identities(walk), walk.seen
            return []
        if _RUNS_KEPT not in known:  # an inventory older than `runs`: what is there now is the person's
            known = known | {i for i in _identities(self._walk(root, known)) if i[0] == "runs"}
        planted: dict[tuple[str, str], Planted] = {}
        failed: set[Identity] = set()
        moved: set[Identity] = set()
        taken: list[str] = []  # what went, whole: what it held went with it
        walk = _Walk()
        for _ in range(_PASSES):
            walk = self._walk(root, known)
            self._dirs[root] = walk.seen
            acted = False
            for label, kind, folder, entries in self._new(walk, known, protected, root, containers):
                if {e.identity for e in entries} <= failed or any(under(label, t) for t in taken):
                    continue
                virtual = _virtual(virtual_root, root, label)
                try:
                    done = self._quarantine(root, folder, entries, virtual, kind)
                except Exception:  # one plant's trouble is never the next one's
                    LOG.exception("could not move %s to quarantine", virtual)
                    done = []
                if done:  # this walk changed the folder: a ctime from a coarse clock may not show it
                    walk.seen.pop(folder, None)
                moved.update(i for e in done for i in e.names)
                taken += [e.path for e in done]
                failed.update(i for e in entries if e not in done for i in e.names)
                planted[(virtual, kind)] = Planted(virtual, kind, len(done) == len(entries))
                acted = acted or bool(done)
            if not acted:
                break
        found = _identities(walk) - moved - failed
        # An unfinished walk proves nothing gone: the person's entries behind it stay known.
        self._known[root] = found if walk.whole else found | known
        return list(planted.values())

    def _new(
        self, walk: _Walk, known: set[Identity], protected: Sequence[str], root: str, containers: bool
    ) -> Generator[tuple[str, str, str, list[_Found]]]:
        """What is new: (the path it is named by, its kind, the folder it is in, its entries)."""
        judged = ("git", "link", "runs") if containers else ("git", "link")
        for folder in sorted(walk.seen):
            seen = walk.seen[folder]
            for entry in seen.found:
                if entry.kind in judged and not entry.names & known:
                    yield entry.path, entry.kind, folder, [entry]
            if seen.shaped and not any(_same(folder, git) for git in protected):
                parts = [e for e in seen.found if e.kind in _SHAPE and e.identity not in known]
                if parts:
                    yield folder, "bare", folder, parts
        for entry in walk.opaque:
            holds = any(under(git, entry.path) for git in protected)
            if entry.identity not in known and not holds and entry.path != root:
                yield entry.path, "opaque", os.path.dirname(entry.path), [entry]

    def _judge_links_by(self, roots: Iterable[str], inward: Iterable[str]) -> None:
        """Whether a link is outward was judged at its listing: new grants, new judgement."""
        now = frozenset((*roots, *inward))
        if now != self._inward:
            self._inward = now
            self._dirs.clear()

    # ── the walk ─────────────────────────────────────────────────────────────────────────────

    def _walk(self, root: str, known: set[Identity] | None = None) -> _Walk:
        """`known`: the person's, as far as they go. None: all of it is, this being the first look."""
        cached = self._dirs.get(root, {})
        walk = _Walk()
        try:
            anchor = _Anchor(os.open(root, os.O_RDONLY | os.O_DIRECTORY), root)
        except OSError as e:
            LOG.warning("frame %s: could not open a granted folder: %s", self.ref, e)
            walk.whole = False
            return walk
        stack: list[tuple[str, _Anchor]] = [(root, anchor)]
        anchor.users = 1
        while stack:
            folder, anchor = stack.pop()
            try:
                self._visit(folder, anchor, root=root, cached=cached, walk=walk, stack=stack, known=known)
            finally:
                anchor.let_go()
        return walk

    def _visit(
        self,
        folder: str,
        anchor: _Anchor,
        *,
        root: str,
        cached: dict[str, _Seen],
        walk: _Walk,
        stack: list[tuple[str, _Anchor]],
        known: set[Identity] | None,
    ) -> None:
        rel = anchor.rel(folder)
        try:
            st = os.stat(rel, dir_fd=anchor.fd, follow_symlinks=False)
            if not stat.S_ISDIR(st.st_mode):
                return  # swapped for a link or a file since its parent was listed: next walk
            closed = st.st_mode & 0o500 != 0o500  # no list, or no way through to what it holds
            if closed and (known is None or ("opaque", st.st_ino, "") in known):
                walk.opaque.append(_Found(folder, "opaque", ("opaque", st.st_ino, "")))
            elif closed:
                st = self._restore(anchor, rel, folder, st)
        except OSError as e:
            if e.errno not in _GONE:
                walk.whole = False
                LOG.warning("frame %s: a folder could not be read: %s", self.ref, e.strerror)
            return
        stamp = (st.st_dev, st.st_ino, st.st_ctime_ns)
        last = cached.get(folder)
        opened: int | None = None
        try:
            if last is None or last.stamp != stamp:
                opened = os.open(rel, _OPEN_DIR, dir_fd=anchor.fd)
                last = self._list(opened, folder, stamp, root)
            walk.seen[folder] = last
            if not last.subdirs:
                return
            if len(os.fsencode(rel)) > _ANCHOR_AT:  # far from the open folder: its children start here
                opened = opened if opened is not None else os.open(rel, _OPEN_DIR, dir_fd=anchor.fd)
                anchor, opened = _Anchor(opened, folder), None
            for sub in last.subdirs:
                anchor.users += 1
                stack.append((sub, anchor))
        except OSError as e:
            if e.errno in _REFUSED:  # restored, and still refused: nothing here can be trusted
                walk.whole = False
                walk.opaque.append(_Found(folder, "opaque", ("opaque", st.st_ino, "")))
            elif e.errno not in _GONE:  # the walk's own trouble (no descriptors, an I/O error)
                walk.whole = False
                LOG.warning("frame %s: a folder could not be listed: %s", self.ref, e.strerror)
        finally:
            if opened is not None:
                os.close(opened)

    def _restore(self, anchor: _Anchor, rel: str, folder: str, st: os.stat_result) -> os.stat_result:
        """`u+rwx`, flags cleared first: an agent's `chmod` hides nothing from its owner's walk. Its
        parents are opened a component at a time from the anchor, following no link: a parent
        swapped for a link since its visit is never followed to change a folder outside the grant."""
        *parents, name = rel.split("/")
        try:
            with _down(anchor.fd, parents) as parent:
                _clear_flags(parent, name, st)
                mode = stat.S_IMODE(st.st_mode) | stat.S_IRWXU
                os.chmod(name, mode, dir_fd=parent, follow_symlinks=False)
                return os.stat(name, dir_fd=parent, follow_symlinks=False)
        except OSError:
            return st  # the listing that follows says what becomes of it

    def _list(self, fd: int, folder: str, stamp: tuple[int, int, int], root: str) -> _Seen:
        with os.scandir(fd) as it:
            entries = list(it)
        shaped = _shaped({e.name.casefold(): e for e in entries})
        found: list[_Found] = []
        subdirs: list[str] = []
        for entry in entries:
            name, path = entry.name, f"{folder}/{entry.name}"
            try:
                link = os.readlink(name, dir_fd=fd) if entry.is_symlink() else ""
                if name.casefold() == ".git":  # a folder, a pointer file or a link: never entered
                    found.append(_Found(path, "git", ("git", entry.inode(), link)))
                    continue
                if later := _runs_later(path, folder, root, entry.inode()):
                    found.append(later)
                part = name.casefold() in _SHAPE
                if part:
                    identity = (name.casefold(), entry.inode(), str(stamp[1]))
                    found.append(_Found(path, name.casefold(), identity))
                if link and self._outward(path) and not (part and shaped):
                    at = ("link-at", 0, f"{path[len(root) :]}\0{link}")
                    found.append(_Found(path, "link", ("link", entry.inode(), link), also=at))
                elif not link and entry.is_dir(follow_symlinks=False):
                    subdirs.append(path)
            except OSError:
                continue  # gone since the listing
        return _Seen(stamp, tuple(subdirs), tuple(found), shaped)

    def _outward(self, link: str) -> bool:
        """Does `link` resolve outside every granted folder, or into Prismoid's own home?"""
        target = os.path.realpath(link)
        if under(target, str(self.home.resolve())):
            return True
        if any(under(target, root) for root in self._inward):
            return False
        return not _venv_python(link, target)

    # ── quarantine ───────────────────────────────────────────────────────────────────────────

    def _quarantine(
        self, root: str, folder: str, entries: list[_Found], virtual: str, kind: str
    ) -> list[_Found]:
        """Move `entries` of `folder` into one new slot, beside its manifest; each only if it is
        still exactly what the walk found, reached from the root without following a link (a folder
        swapped for a link meanwhile is never followed to move a stranger's file). The slot exists
        only once something is in it. Returns what moved."""
        rel = folder[len(root) :].strip("/")
        parts = rel.split("/") if rel else []
        base = self.home / QUARANTINE / self.ref
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        incoming = base / f".incoming-{secrets.token_hex(4)}"
        incoming.mkdir(mode=0o700)
        (incoming / ITEM).mkdir(mode=0o700)
        moved: list[_Found] = []
        try:
            with _folder(root, parts) as parent:
                _loosen_folder(parent)
                moved = self._move_each(parent, entries, incoming / ITEM)
            if moved:
                self._publish(base, incoming, virtual, kind, moved)
        except OSError as e:
            LOG.warning("could not move %s to quarantine: %s", virtual, e.strerror)
        finally:
            if incoming.exists():
                shutil.rmtree(incoming, ignore_errors=True)
        return moved

    def _move_each(self, parent: int, entries: list[_Found], into: Path) -> list[_Found]:
        """What moved: an entry that would not goes on being looked at, and stops no other."""
        moved: list[_Found] = []
        for entry in entries:
            with contextlib.suppress(OSError):
                if self._move(parent, entry, into):
                    moved.append(entry)
        return moved

    def _move(self, parent: int, entry: _Found, into: Path) -> bool:
        name = os.path.basename(entry.path)
        st = os.stat(name, dir_fd=parent, follow_symlinks=False)
        link = os.readlink(name, dir_fd=parent) if stat.S_ISLNK(st.st_mode) else ""
        kept = entry.identity[2] if entry.kind in ("git", "link") else link
        if st.st_ino != (entry.inode or entry.identity[1]) or link != kept:
            LOG.warning("frame %s: an entry changed under the walk: left to the next one", self.ref)
            raise OSError(errno.ESTALE, "changed under the walk")
        _loosen(parent, name, st)
        try:
            os.rename(name, into / name, src_dir_fd=parent)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise
            _move_across(parent, name, into / name)  # a grant on another disk
        return True

    def _publish(self, base: Path, incoming: Path, virtual: str, kind: str, moved: list[_Found]) -> None:
        """The manifest, then the slot's number: a numbered slot always holds both."""
        names = [os.path.basename(e.path) for e in moved]
        manifest = {
            "virtual_path": virtual,
            "kind": kind,
            "reason": REASONS[kind],
            "files": names,
            "at": dt.datetime.now(dt.UTC).isoformat(),
        }
        fd = os.open(incoming / MANIFEST, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(manifest, f)
        n = max((int(p.name) for p in base.iterdir() if p.name.isdigit()), default=0) + 1
        while True:
            try:
                os.replace(incoming, base / str(n))
                break
            except OSError as e:
                if e.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                    raise
                n += 1
        LOG.warning("frame %s: moved %s (%s) to quarantine %s", self.ref, virtual, kind, n)

    def _save(self, before: dict[str, set[Identity]]) -> None:
        if (self._known == before and self.path.exists()) or not self.path.parent.is_dir():
            return  # nothing new, or the frame is being removed
        data = {root: sorted(items) for root, items in self._known.items()}
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)


def _identities(walk: _Walk) -> set[Identity]:
    held = {name for seen in walk.seen.values() for entry in seen.found for name in entry.names}
    return held | {entry.identity for entry in walk.opaque} | {_RUNS_KEPT}


def holds_repository(slot: Path) -> bool:
    """Does this quarantine slot hold a repository, its own or at any depth of a folder it holds? A
    folder that cannot be read may. No link is followed."""
    try:
        kind = json.loads((slot / MANIFEST).read_text()).get("kind")
    except (OSError, ValueError, AttributeError):
        kind = None
    if kind in ("git", "bare"):
        return True
    unread: list[OSError] = []
    for _, folders, names in os.walk(slot / ITEM, onerror=unread.append):
        if any(name.casefold() == ".git" for name in (*folders, *names)):
            return True
    return any(not isinstance(e, FileNotFoundError) for e in unread)


def _runs_later(path: str, folder: str, root: str, inode: int) -> _Found | None:
    """An entry `PROTECT_WRITE` names, topmost only: a folder of these is one entry, and what the
    person's IDE keeps in it is theirs. Known by its path in the grant, unlike the rest: an overlay
    keeps a container out of what is there, and a person's editor saves by rename, to a new inode."""
    within, around = path[len(root) :], folder[len(root) :]
    if _RUNS.search(around) or not _RUNS.search(within):
        return None
    return _Found(path, "runs", ("runs", 0, within), inode)


def _same(path: str, other: str) -> bool:
    return under(path, other) and under(other, path)


def _virtual(virtual_root: str, root: str, path: str) -> str:
    rel = path[len(root) :].strip("/")
    return f"{virtual_root}/{rel}" if rel else virtual_root


def _loosen_folder(fd: int) -> None:
    """The folder a plant is taken from: an immutable flag or a mode must not keep it there."""
    st = os.fstat(fd)
    with contextlib.suppress(OSError):
        if getattr(st, "st_flags", 0) and sys.platform == "darwin":
            os.chflags(_where(fd), 0, follow_symlinks=False)
        if st.st_mode & 0o700 != 0o700:
            os.fchmod(fd, stat.S_IMODE(st.st_mode) | stat.S_IRWXU)


def _clear_flags(parent: int, name: str, st: os.stat_result) -> None:
    if getattr(st, "st_flags", 0) and sys.platform == "darwin":
        with contextlib.suppress(OSError):
            os.chflags(os.path.join(_where(parent), name), 0, follow_symlinks=False)


def _loosen(parent: int, name: str, st: os.stat_result) -> None:
    """The plant itself: its flags cleared, and a folder writable (a rename across folders rewrites
    its `..`)."""
    _clear_flags(parent, name, st)
    if stat.S_ISDIR(st.st_mode) and st.st_mode & 0o700 != 0o700:
        with contextlib.suppress(OSError):
            os.chmod(name, stat.S_IMODE(st.st_mode) | stat.S_IRWXU, dir_fd=parent, follow_symlinks=False)


@contextlib.contextmanager
def _down(start: int, parts: list[str]) -> Generator[int]:
    """An fd on `parts...` below the open folder `start`, each folder opened from its parent without
    following a link."""
    fd = os.open(".", os.O_RDONLY | os.O_DIRECTORY, dir_fd=start)
    try:
        for part in parts:
            child = os.open(part, _OPEN_DIR, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextlib.contextmanager
def _folder(root: str, parts: list[str]) -> Generator[int]:
    """An fd on `root/parts...`, the walk down following no link."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with _down(fd, parts) as folder:
            yield folder
    finally:
        os.close(fd)


def _where(fd: int) -> str:
    """Where the open folder really is now."""
    if sys.platform == "darwin":
        return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b"\0").decode()
    return os.readlink(f"/proc/self/fd/{fd}")


def _move_across(parent: int, name: str, dest: Path) -> None:
    """The move to another disk: a copy (links copied as links) and a delete, from where the open
    folder really is now. A path again, so a folder swapped in that instant could be acted on: the
    price of a grant on an external disk."""
    shutil.move(os.path.join(_where(parent), name), dest)
