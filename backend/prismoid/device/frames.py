# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The helper's frame registry: refs, folders, and `frame.json` on disk.

Layout under the helper's home: `tasks/<task_id>/{task,artifacts,skills,verify,out,work,tmp}` for
a Machine frame, `scratch/<ref>/...` for a throwaway one, and beside them the frame's exec journal,
its process groups and its git config. `frame.json` (spec, ref and the repositories found in its
grants, never a secret) is what lets a restarted helper list the frames it had; the restarted
helper kills what the last one left running.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import stat
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from prismoid.device.fence_data import REGISTRIES
from prismoid.device.frame_config import GIT_CONFIG_FILE, write_docker_config, write_gitconfig
from prismoid.device.journal import Journal
from prismoid.device.plants import QUARANTINE, Plants, holds_repository
from prismoid.device.procs import ProcessGroups
from prismoid.device.seatbelt import alias_root, cache_root, linked_git_refusal, under
from prismoid.schemas.device import (
    WORK_NAME,
    FolderGrant,
    FrameOut,
    FramePathsWire,
    FrameSpecIn,
    GrantsIn,
    grant_name_ok,
    named_grants,
)
from prismoid.schemas.machines import FrameKind

LOG = logging.getLogger(__name__)

FRAME_FILE = "frame.json"
# The instruction files the system mounts in `task/` (`ai/frames/layout.INSTRUCTIONS`, which
# `prismoid.device` may not import).
INSTRUCTION_FILES = ("answers.json", "context.md", "thread.md")
JOURNAL_FILE = "journal.jsonl"
NOTICES_FILE = "notices.json"
GROUPS_FILE = "groups.json"
PLANTS_FILE = "plants.json"
_SUBDIRS = ("task", "artifacts", "skills", "verify", "out", "work", "tmp")
# The walk for repositories already in a granted folder: this deep, and never into the folders that
# hold other people's checkouts. A repository it misses stays wholly read-only (fails closed).
_REPO_DEPTH = 6
_REPO_SKIP = frozenset({"node_modules", ".venv", "target"})
_DENIALS_KEPT = 100
# How long a plant walk may hold a command's result (or a sleep). One past it carries on in its
# thread and what it finds is told with the next result: a walk is one `lstat` per folder, but the
# first after a helper restart lists them all, and a finished command must not read as a lost helper.
SWEEP_WAIT_S: Final = 2.0
# A stopping helper's patience with its commands (`FrameRegistry.stop`): SIGTERM, this long to go,
# then SIGKILL, watched this long. Far inside the supervisor's `stop_timeout_s`, past which it kills
# the helper outright and the tree would outlive it.
STOP_GRACE_S: Final = 3.0
STOP_KILL_S: Final = 2.0
_STOP_TICK_S: Final = 0.05
# How many repositories get the fence's full rules, git's own state files writable (the shallowest;
# `FenceSpec.pinned_git` says what each costs), and how many more are pinned. Past both a repository
# is still the person's (known, never moved) but not fenced: its parents can be renamed, and its
# contents changed on the way.
FULL_GIT: Final = 16
PROTECTED_CAP: Final = 512


_QUARANTINE_REF = re.compile(r"device-[A-Za-z0-9_-]{1,100}")
GUARD = "guard"  # a denial's kind when it is not the proxy's: a plant moved, a server stopped
_SCRATCH_REF = re.compile(r"^device-(seat|verify|qa|figures|selftest)-[0-9a-f]{8}$")


class Notices(deque[str]):
    """What the agent has not been told yet (a server stopped, a plant moved), on disk from the moment
    it is known: `notices.json`, whole, beside the frame. Reading the file takes nothing off it, and
    neither does a result that carries a note: only `told`, for a result somebody heard. So a note
    survives a helper killed at any point, a stop whose walk ran long, and an answer into the void.
    Appended from a walk's thread as well as the loop."""

    def __init__(self, path: Path) -> None:
        super().__init__(maxlen=_DENIALS_KEPT)
        self.path = path
        self._lock = threading.Lock()
        with contextlib.suppress(OSError, ValueError, TypeError):
            self.extend(str(note) for note in json.loads(path.read_text()))

    def append(self, note: str) -> None:
        with self._lock:
            super().append(note)
            self._save()

    def told(self, notes: list[str]) -> None:
        """A result carrying `notes` reached its worker."""
        with self._lock:
            for note in notes:
                with contextlib.suppress(ValueError):
                    self.remove(note)
            self._save()

    def _save(self) -> None:
        """Never fails what it follows; a frame being removed has nothing left to keep."""
        try:
            if not self:
                self.path.unlink(missing_ok=True)
            elif self.path.parent.is_dir():
                _write_whole(self.path, json.dumps(list(self)))
        except OSError:
            LOG.exception("could not keep frame notices in %s", self.path)


class HelperStopping(Exception):
    """The helper is stopping: this command will not run, or did not finish. Never a result: to the
    worker it is a helper gone away (a 503, `FrameUnreachable`), and the step parks on the journal."""


class GrantRefused(ValueError):
    """A grant the helper will not honour, whatever the server asked for."""


@dataclass
class DeviceFrameState:
    ref: str
    spec: FrameSpecIn
    paths: FramePathsWire
    token: str  # the frame's proxy credential, in memory only
    home: Path  # the Prismoid home
    base: Path  # the frame's folder: its roots, tmp/, frame.json and the journal
    journal: Journal
    # Its running commands and what they left running (`cmd &`).
    groups: ProcessGroups
    # The repositories and outward links its rw grants held, and the walk that moves new ones.
    plants: Plants
    # Each repository's git folder found in a rw grant when the frame was made (FenceSpec).
    protected_git: tuple[str, ...] = ()
    # Those past what the fence's full rules can bear, and those the baseline walk alone found:
    # parents not renamed, read-only (`FenceSpec.pinned_git`).
    pinned_git: tuple[str, ...] = ()
    # Readers still draining a pipe a leftover holds open, so it never blocks on a full pipe.
    drains: set[asyncio.Task[None]] = field(default_factory=set)
    allow: set[str] = field(default_factory=set)  # the egress allowlist: REGISTRIES and the task's
    egress_version: int = 0
    denials: deque[dict[str, str]] = field(default_factory=lambda: deque(maxlen=_DENIALS_KEPT))
    # What the agent is told with its next command's result (a server stopped, a plant moved).
    notices: Notices = field(init=False)
    walk: asyncio.Future[None] | None = None  # the plant walk in progress (`sweep_within`)
    # The registry's, one for all its frames: a re-create replaces a frame's state under its command.
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    # Each grant's path and the real folder it named when it was made: read from here, never
    # resolved again, so a link put in its place later moves no write access (`_pin`).
    pins: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.notices = Notices(self.base / NOTICES_FILE)

    @property
    def aliases(self) -> dict[str, str]:
        """Each root whose real path needs quoting, and the link that spells it without any. The
        default home is `Application Support`, so `/work/build.py` spelled really is two words to
        the shell; the worker writes the alias into a command instead, by plain substitution, so
        nothing parses bash. A root that needs no quoting has none. Empty when the user's home
        itself would need quoting: there is no safe spelling, and such a command fails as it did."""
        folder = alias_root(self.ref)
        if shlex.quote(folder) != folder:
            return {}
        p = self.paths
        roots = (p.task, p.work, p.artifacts, p.skills, p.verify, p.inputs, p.out, *p.grants.values())
        return {
            root: f"{folder}/{hashlib.sha256(root.encode()).hexdigest()[:12]}"
            for root in roots
            if root and shlex.quote(root) != root
        }

    def link_aliases(self) -> None:
        """Hold `aliases` true on disk: a missing link made, a wrong one replaced, and what no alias
        names removed, whatever it is. Asked at every command: the system may purge
        `~/Library/Caches`, and only the helper writes here, so anything else is damage to undo."""
        aliases = self.aliases
        folder = Path(alias_root(self.ref))
        if not aliases and not folder.exists():
            return
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        for real, alias in aliases.items():
            if _link_of(alias) != real:
                _remove(Path(alias))
                os.symlink(real, alias)
        for entry in folder.iterdir():
            if str(entry) not in aliases.values():
                _remove(entry)

    def granted(self, mode: str) -> list[str]:
        """The real paths of the folders granted in `mode`. A throwaway frame is blind to the
        user's folders: no grant is honoured."""
        if self.spec.kind != FrameKind.MACHINE:
            return []
        return [self.pins.get(g.path) or _real(g.path) for g in self.spec.grants if g.mode == mode]

    @property
    def user_roots(self) -> list[tuple[str, str]]:
        """Each read-write grant: (its virtual root, its real path)."""
        if self.spec.kind != FrameKind.MACHINE:
            return []
        return [
            (name, self.pins.get(g.path) or _real(g.path))
            for name, g in named_grants(self.spec.grants)
            if g.mode == "rw"
        ]

    @property
    def inward(self) -> list[str]:
        """Every folder it was granted: what a link in its folders may name."""
        return [*self.granted("rw"), *self.granted("ro")]

    def baseline_plants(self) -> None:
        """Its rw grants as they are now are the person's: the walk moves only what comes after,
        and every repository it found joins the fence's list (`find_repos` looks only so deep and
        never in `node_modules`). A walk of every folder (the first one), so the helper runs it
        off its event loop."""
        found = self.plants.baseline([real for _, real in self.user_roots], self.inward)
        rw = self.granted("rw")
        real = [r for r in map(os.path.realpath, found) if any(under(r, root) for root in rw)]
        tiers = _tiers([*self.protected_git, *self.pinned_git, *real], self.ref)
        if tiers != (self.protected_git, self.pinned_git):
            self.protected_git, self.pinned_git = tiers
            self.save()

    def save(self) -> None:
        """`frame.json`, whole or not at all: a torn one is a frame the next helper cannot list."""
        data = {
            "ref": self.ref,
            "spec": self.spec.model_dump(mode="json"),
            "protected_git": list(self.protected_git),
            "pinned_git": list(self.pinned_git),
            "pins": self.pins,
        }
        _write_whole(self.base / FRAME_FILE, json.dumps(data))

    def sweep_plants(self) -> None:
        """Move what is new of repositories and outward links in its rw grants to quarantine
        (`plants`): each kept in the denials, as the virtual path, and told to the agent. A walk
        that fails is logged: it never fails the command it follows."""
        protected = (*self.protected_git, *self.pinned_git, *self.linked_git)
        try:
            planted = self.plants.sweep(self.user_roots, protected, self.inward, containers=self.spec.docker)
        except Exception:
            LOG.exception("could not walk frame %s's folders for plants", self.ref)
            return
        for plant in planted:
            self.deny(plant.virtual, plant.reason)
            self.notices.append(plant.notice)

    def deny(self, host: str, reason: str) -> None:
        """A refusal of the helper's own, which a widened allowlist does not forget (`apply_allow`)."""
        self.denials.append({"ref": self.ref, "host": host, "reason": reason, "kind": GUARD})

    async def sweep_within(self, wait_s: float = SWEEP_WAIT_S) -> None:
        """`sweep_plants` off the event loop, waiting at most `wait_s` for it, and no second walk
        while one still runs past that (the next command starts it). A walk the wait gave up on
        runs on to its end."""
        if self.walk is not None and not self.walk.done():
            await asyncio.wait({self.walk}, timeout=wait_s)
            if not self.walk.done():
                return
        self.walk = asyncio.ensure_future(asyncio.to_thread(self.sweep_plants))
        await asyncio.wait({self.walk}, timeout=wait_s)

    async def last_walk(self) -> None:
        """The frame is ending: the walk in progress may be past what changed last, so it finishes
        and one more runs, to its end, however long."""
        if self.walk is not None:
            await asyncio.wait({self.walk})
        self.walk = asyncio.ensure_future(asyncio.to_thread(self.sweep_plants))
        await asyncio.wait({self.walk})

    @property
    def linked_git(self) -> tuple[str, ...]:
        """The git folders outside every grant that a granted worktree's pointer names (`_vet` has
        proved each one is): writable only for git's state files. One inside another read-write
        grant is that grant's own repository, already in `protected_git`."""
        if self.spec.kind != FrameKind.MACHINE:
            return ()
        rw = self.granted("rw")
        named = (d for g in self.spec.grants if g.mode == "rw" for d in g.git)
        return tuple(dict.fromkeys(d for d in named if not any(under(d, root) for root in rw)))

    @property
    def docker_config(self) -> Path:
        """The frame's own `DOCKER_CONFIG` (the user's `~/.docker/config.json` is read-denied)."""
        return self.base / "docker"


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _pin(grants: Sequence[FolderGrant], kept: dict[str, str]) -> dict[str, str]:
    """Each grant's real folder: the one already held for a path that stays, resolved for a new one."""
    return {g.path: kept.get(g.path) or _real(g.path) for g in grants}


def pointer_dirs(pointer: str) -> list[str]:
    """The folders a `.git` pointer file names, as real paths: its `gitdir:` (a worktree's own git
    folder, or a `--separate-git-dir`) and, when that has one, its `commondir` (the repository's).
    [] for a file that is no pointer."""
    try:
        with open(pointer, errors="replace") as f:
            line = f.readline(4096).strip()
    except OSError:
        return []
    if not line.startswith("gitdir:"):
        return []
    gitdir = os.path.realpath(os.path.join(os.path.dirname(pointer), line.removeprefix("gitdir:").strip()))
    named = [gitdir]
    try:
        with open(os.path.join(gitdir, "commondir"), errors="replace") as f:
            named.append(os.path.realpath(os.path.join(gitdir, f.readline(4096).strip())))
    except OSError:
        pass
    return named


def worktree_git(folder: str) -> list[str]:
    """What a granted folder that is a git worktree needs beyond itself:
    the git folders its own `.git` pointer names, when they lie outside it - real, existing folders,
    in the pointer's order. Only git's state files are writable in them (`FenceSpec.linked_git`);
    nothing else of the repository is granted. [] for any other folder."""
    real = os.path.realpath(folder)
    pointer = os.path.join(real, ".git")
    if not os.path.isfile(pointer) or os.path.islink(pointer):
        return []
    return [d for d in pointer_dirs(pointer) if os.path.isdir(d) and not under(d, real)]


def find_repos(roots: Sequence[str]) -> tuple[str, ...]:
    """The git folder of every repository under `roots`, found by a bounded walk that follows no
    folder link: each `.git` (a folder, a link to one, or a pointer file), the folder a pointer
    file's `gitdir:` names (`--separate-git-dir`, a worktree) and its `commondir`, and every bare
    repository (`HEAD`, `objects/`, `refs/`; its hooks run on a push). Matched ignoring case, as
    Seatbelt and APFS match. Only real paths inside `roots` are listed: the fence keeps anything
    outside them read-only already."""
    found: list[str] = []

    def keep(path: str) -> str:
        real = os.path.realpath(path)
        if any(under(real, root) for root in roots):
            found.append(real)
        return real

    def follow(pointer: str) -> None:
        for named in pointer_dirs(pointer):
            keep(named)

    def bare(folder: str) -> bool:
        return all(
            (os.path.isfile if name == "HEAD" else os.path.isdir)(os.path.join(folder, name))
            for name in ("HEAD", "objects", "refs")
        )

    def walk(folder: str, depth: int) -> None:
        if bare(folder):
            keep(folder)
            return
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for entry in entries:
            if entry.name.casefold() == ".git":
                keep(entry.path)
                if entry.is_file():
                    follow(entry.path)
                continue
            if depth < _REPO_DEPTH and entry.name not in _REPO_SKIP and entry.is_dir(follow_symlinks=False):
                walk(entry.path, depth + 1)

    for root in roots:
        walk(root, 0)
    return tuple(sorted(dict.fromkeys(found)))


def _link_of(path: str) -> str | None:
    """Where the link `path` points, as written; None for anything that is no link."""
    try:
        return os.readlink(path)
    except OSError:
        return None


def _remove(path: Path) -> None:
    """`path`, whatever it is: a link goes as a link, its target untouched."""
    if path.is_dir() and not path.is_symlink():
        force_rmtree(path)
    else:
        path.unlink(missing_ok=True)


def force_rmtree(path: Path) -> None:
    """Delete `path`, first giving its owner write on each folder (Go makes its module cache
    read-only, and a command may `chmod a-w` its own). Follows no link, so nothing outside is
    touched."""
    if not path.is_dir() or path.is_symlink():
        return

    def grant(folder: str) -> None:
        with contextlib.suppress(OSError):
            os.chmod(folder, stat.S_IRWXU, follow_symlinks=False)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            return
        for entry in entries:
            if entry.is_dir(follow_symlinks=False):
                grant(entry.path)

    grant(str(path))
    shutil.rmtree(path, ignore_errors=True)


def _tiers(repositories: Sequence[str], ref: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(the full rules, pinned). Only a folder named `.git` can be pinned: the fence's name rule is
    what keeps it read-only, so anything else (a bare repository) stays on the full list."""
    named = sorted(
        (r for r in dict.fromkeys(repositories) if os.path.basename(r).casefold() == ".git"),
        key=lambda r: (r.count("/"), r),
    )
    others = [r for r in dict.fromkeys(repositories) if os.path.basename(r).casefold() != ".git"]
    room = max(FULL_GIT - len(others), 0)
    if len(named) > room + PROTECTED_CAP:
        LOG.warning("frame %s: %d repositories are past the fence", ref, len(named) - room - PROTECTED_CAP)
    return (*others, *named[:room]), tuple(named[room : room + PROTECTED_CAP])


def ref_for(spec: FrameSpecIn) -> str:
    if spec.kind == FrameKind.MACHINE:
        return f"device-{spec.task_id}"
    return f"device-{spec.kind}-{uuid.uuid4().hex[:8]}"


def _asked(state: DeviceFrameState, sig: int) -> None:
    """One frame's part of a stop: whatever it raises, the next frame is stopped all the same."""
    try:
        state.groups.hold()
        state.groups.signal(sig)
    except Exception:
        LOG.exception("could not signal frame %s's processes", state.ref)


def _left(state: DeviceFrameState) -> int:
    try:
        return len(state.groups.pids())
    except Exception:
        LOG.exception("could not look at frame %s's processes", state.ref)
        return 1  # unknown is not gone: the stop goes on to its SIGKILL


def _write_whole(path: Path, text: str) -> None:
    """`text` in `path`, through a temporary file and a rename: a crash leaves the old one whole."""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class FrameRegistry:
    def __init__(self, home: Path) -> None:
        self.home = home
        self.frames: dict[str, DeviceFrameState] = {}
        self.stopping = asyncio.Event()
        self._rebuild()

    def _base(self, ref: str, spec: FrameSpecIn) -> Path:
        if spec.kind == FrameKind.MACHINE:
            return self.home / "tasks" / spec.task_id
        return self.home / "scratch" / ref

    def _paths(self, base: Path, spec: FrameSpecIn, pins: dict[str, str]) -> FramePathsWire:
        # A throwaway frame is blind to the user's folders: no grant is honoured.
        # Each grant is under the virtual root the server minted for it (`FolderGrant.name`): the
        # one named `/work` is the work folder; with none, the frame works in a fresh one of its own.
        grants = spec.grants if spec.kind == FrameKind.MACHINE else []
        named = named_grants(grants)
        work = next((g for name, g in named if name == WORK_NAME), None)
        granted = pins[work.path] if work is not None else None
        others = {name: pins[g.path] for name, g in named if name != WORK_NAME}
        return FramePathsWire(
            task=str(base / "task"),
            work=granted or str(base / "work"),
            artifacts=str(base / "artifacts"),
            skills=str(base / "skills"),
            verify=str(base / "verify"),
            inputs=str(base / "task" / "inputs"),
            browser=str(self.home / "chrome"),
            out=str(base / "out"),
            grants=others,
        )

    def _vet(self, spec: FrameSpecIn) -> None:
        """The helper is the enforcer: a grant equal to, inside or around its own home is refused;
        so is a worktree's git list that is not what the folder's own pointer names, or that names a
        folder the fence would never let git write (`linked_git_refusal`); and so are names that are
        not virtual roots, that repeat, or a read-only `/work`. Its refusals name the grant by its
        virtual root, never by its real path: they can reach a step's record."""
        home = self.home.resolve()
        seen: set[str] = set()
        for name, g in named_grants(spec.grants):
            if not grant_name_ok(name) or name.casefold() in seen or (name == WORK_NAME and g.mode != "rw"):
                raise GrantRefused("a grant's name is not a virtual root the helper takes, or repeats")
            seen.add(name.casefold())
            real = Path(g.path).expanduser().resolve()
            if real == home or home in real.parents or real in home.parents:
                raise GrantRefused(f"{name}: a grant may not overlap the helper's own home")
            if g.git and (g.mode != "rw" or g.git != worktree_git(str(real))):
                raise GrantRefused(f"{name}: the git folders are not the ones its .git names")
            if why := next(filter(None, (linked_git_refusal(d, self.home) for d in g.git)), None):
                raise GrantRefused(f"{name}: its repository's git folder {why}")

    def create(self, spec: FrameSpecIn) -> DeviceFrameState:
        self._vet(spec)
        ref = ref_for(spec)
        base = self._base(ref, spec)
        kept = self.frames.get(ref)
        pins = _pin(spec.grants, kept.pins if kept else self._saved_pins(base))
        paths = self._paths(base, spec, pins)
        for sub in _SUBDIRS:
            if sub == "work" and paths.work != str(base / "work"):
                continue  # the user's own folder is theirs to create
            (base / sub).mkdir(parents=True, exist_ok=True)
        Path(paths.inputs).mkdir(exist_ok=True)
        state = DeviceFrameState(
            ref=ref,
            spec=spec,
            paths=paths,
            token=kept.token if kept else secrets.token_hex(16),
            home=self.home,
            base=base,
            journal=kept.journal if kept else Journal(base / JOURNAL_FILE),
            groups=kept.groups if kept else ProcessGroups(base / GROUPS_FILE),
            plants=kept.plants if kept else Plants(base / PLANTS_FILE, self.home, ref),
            drains=kept.drains if kept else set(),
            denials=kept.denials if kept else deque(maxlen=_DENIALS_KEPT),
            stopping=self.stopping,
            pins=pins,
        )
        if kept:
            state.notices = kept.notices
        state.protected_git, state.pinned_git = self._repos(base, state)
        write_gitconfig(base)
        state.allow, state.egress_version = {*REGISTRIES, *spec.allow_domains}, spec.egress_version
        self._docker(state)
        state.save()
        self.frames[ref] = state
        return state

    def regrant(self, state: DeviceFrameState, grants: GrantsIn) -> None:
        """What the server holds for the task now (the helper is told, never asks): the
        frame's folders change, everything else of it - its egress, journal, running groups - stays.
        Its repositories are walked again when the folders changed."""
        spec = state.spec.model_copy(update={"grants": grants.grants, "docker": grants.docker})
        self._vet(spec)
        if spec == state.spec:
            return
        state.sweep_plants()  # no later walk reads a folder this takes away
        state.pins = _pin(spec.grants, state.pins)
        state.spec, state.paths = spec, self._paths(state.base, spec, state.pins)
        if state.paths.work == str(state.base / "work"):
            (state.base / "work").mkdir(exist_ok=True)
        state.protected_git, state.pinned_git = self._repos(state.base, state)
        state.baseline_plants()  # a folder granted now is the person's as it is now
        self._docker(state)
        state.save()

    def _docker(self, state: DeviceFrameState) -> None:
        if state.spec.docker:
            write_docker_config(state.docker_config)

    def forget_quarantine(self, ref: str) -> bool:
        """A deleted task: what its frames had moved out goes too, but for a repository, which a
        person may have started, and whose commits are nowhere else. False for no frame's ref."""
        if not _QUARANTINE_REF.fullmatch(ref):
            return False
        base = self.home / QUARANTINE / ref
        for slot in list(base.iterdir()) if base.is_dir() else ():
            if not holds_repository(slot):
                force_rmtree(slot)
        with contextlib.suppress(OSError):
            base.rmdir()
        return True

    def _saved_pins(self, base: Path) -> dict[str, str]:
        try:
            pins = json.loads((base / FRAME_FILE).read_text())["pins"]
        except (OSError, ValueError, KeyError, TypeError):
            return {}
        return {str(path): str(real) for path, real in pins.items()}

    def _repos(self, base: Path, state: DeviceFrameState) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Walked once, when the frame is first made; a re-create with the same grants keeps the
        lists it persisted (a repository that appeared since is not the user's, so not listed). New
        grants keep what a folder still rw held, since a baseline found it and `find_repos` is
        bounded, and only a folder new here is walked: in one kept, what looks like a repository now
        may be a plant the next walk will move."""
        rw = state.granted("rw")
        try:
            saved = json.loads((base / FRAME_FILE).read_text())
            before = FrameSpecIn.model_validate(saved["spec"]).grants
            protected = tuple(str(g) for g in saved["protected_git"])
            pinned = tuple(str(g) for g in saved.get("pinned_git", []))
            pins = saved.get("pins", {})
        except (OSError, ValueError, KeyError, TypeError):
            return _tiers(find_repos(rw), state.ref)
        if before == state.spec.grants:
            return protected, pinned
        kept = [pins.get(g.path) or _real(g.path) for g in before if g.mode == "rw"]
        held = [g for g in (*protected, *pinned) if any(under(g, r) for r in rw)]
        new = find_repos([r for r in rw if r not in kept])
        return _tiers([*held, *(g for g in new if not any(under(g, k) for k in kept))], state.ref)

    def destroy(self, ref: str) -> bool:
        """Kill what the frame left running, then remove its Prismoid folder and its cache. A granted
        work folder is outside both and never touched."""
        state = self.frames.pop(ref, None)
        if state is None:
            return False
        state.groups.kill()
        for drain in state.drains:
            drain.cancel()
        force_rmtree(Path(cache_root(ref)))
        force_rmtree(Path(alias_root(ref)))
        force_rmtree(self._base(ref, state.spec))
        return True

    def stop_services(self, state: DeviceFrameState) -> None:
        """The Machine is going to sleep: kill what the frame left running - every group
        still proved its own (`cmd &`, a dev server) - and keep the frame, its folders and its
        journal. Its containers are the helper's to stop (`docker_cleanup.stop_labelled`)."""
        state.groups.kill()

    def out(self, state: DeviceFrameState, device_name: str) -> FrameOut:
        try:
            made = dt.datetime.fromtimestamp((state.base / FRAME_FILE).stat().st_mtime, dt.UTC)
        except OSError:
            made = None
        return FrameOut(
            ref=state.ref,
            paths=state.paths,
            device_name=device_name,
            labels=state.spec.labels,
            created_at=made,
            grants=state.spec.grants,
            docker=state.spec.docker,
            aliases=state.aliases,
        )

    def reset_mounts(self, state: DeviceFrameState) -> None:
        """Take away what the last step mounted: `task/inputs`, the instruction files beside it and
        `skills/`. The folders stay; what the system put in them goes."""
        task = Path(state.paths.task)
        for folder in (Path(state.paths.inputs), Path(state.paths.skills)):
            for entry in list(folder.iterdir()) if folder.is_dir() else []:
                if entry.is_dir() and not entry.is_symlink():
                    force_rmtree(entry)
                else:
                    entry.unlink(missing_ok=True)
        for name in INSTRUCTION_FILES:
            (task / name).unlink(missing_ok=True)

    def _rebuild(self) -> None:
        for marker in sorted(self.home.glob(f"tasks/*/{FRAME_FILE}")):
            self._adopt(marker, f"device-{marker.parent.name}", FrameKind.MACHINE)
        for marker in sorted(self.home.glob(f"scratch/*/{FRAME_FILE}")):
            if _SCRATCH_REF.match(marker.parent.name):
                self._adopt(marker, marker.parent.name, None)

    def _adopt(self, marker: Path, ref: str, kind: FrameKind | None) -> None:
        """The ref comes from the directory name, never from frame.json."""
        try:
            spec = FrameSpecIn.model_validate(json.loads(marker.read_text())["spec"])
        except (OSError, ValueError, KeyError, TypeError):
            # A torn frame.json is not a frame, and what it ran is not a reason to run on.
            ProcessGroups(marker.parent / GROUPS_FILE).kill()
            return
        if kind is not None and (spec.kind != kind or f"device-{spec.task_id}" != ref):
            return
        if kind is None and ref.split("-")[1] != spec.kind:
            return
        try:
            saved = json.loads(marker.read_text())
            protected = tuple(str(g) for g in saved.get("protected_git", []))
            pinned = tuple(str(g) for g in saved.get("pinned_git", []))
        except (OSError, ValueError, AttributeError, TypeError):
            protected = pinned = ()  # a repository not listed is wholly read-only: fails closed
        pins = _pin(spec.grants, self._saved_pins(marker.parent))
        state = DeviceFrameState(
            ref=ref,
            spec=spec,
            paths=self._paths(marker.parent, spec, pins),
            token=secrets.token_hex(16),
            home=self.home,
            base=marker.parent,
            journal=Journal(marker.parent / JOURNAL_FILE),
            groups=ProcessGroups(marker.parent / GROUPS_FILE),
            plants=Plants(marker.parent / PLANTS_FILE, self.home, ref),
            protected_git=protected,
            pinned_git=pinned,
            allow={*REGISTRIES, *spec.allow_domains},
            egress_version=spec.egress_version,
            stopping=self.stopping,
            pins=pins,
        )
        # A new helper life: what the last one left running dies (its credential died with it),
        # and its unfinished commands stay in the journal as cut off, never as running.
        state.groups.kill()
        if not (marker.parent / GIT_CONFIG_FILE).exists():
            write_gitconfig(marker.parent)
        self.frames[ref] = state

    async def stop(self) -> None:
        """The helper is stopping (Ctrl-C on `prismoid up`, `prismoid down`, a SIGTERM): nothing new starts,
        and every process of every frame, running or left running, is asked to go and then killed.
        The whole tree: `hold` walks it before each signal, because what left a command's group is
        found through its parents only while they live. Bounded (`STOP_GRACE_S`, `STOP_KILL_S`); what
        still stands is logged, and the next life's `_adopt` tries again.

        The journal is `exec.run`'s to keep: `stopping` is set before the first signal, so a command
        killed here always sees it, and is left open as cut off. A helper killed outright never gets
        here, and the next life's `_adopt` is then the only stop. Asking twice is harmless."""
        self.stopping.set()
        states = list(self.frames.values())
        left = 0
        for sig, patience in ((signal.SIGTERM, STOP_GRACE_S), (signal.SIGKILL, STOP_KILL_S)):
            for state in states:
                _asked(state, sig)
            deadline = time.monotonic() + patience
            while left := sum(_left(state) for state in states):
                if time.monotonic() >= deadline:
                    break
                await asyncio.sleep(_STOP_TICK_S)
            if not left:
                return
        LOG.error("%d processes outlived the helper's stop", left)
