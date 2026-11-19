# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Who a process is: start times and process-group members, from libproc.

A process group's id is a number the kernel hands out again once the group is empty, so a number
alone never names "our" group: the frame keeps the members it saw, by pid AND start time, and a
group is still its own only while one of those very processes is in it (`frames.GroupRecord`).
libproc (`proc_pidinfo`, `proc_listpids`) is the macOS API `ps` itself reads, via ctypes (no
dependency). A process this user may not inspect answers nothing, and nothing is never "ours".
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

_PROC_PIDTBSDINFO: Final = 3
_PROC_ALL_PIDS: Final = 1
_PROC_PGRP_ONLY: Final = 2


class _BsdInfo(ctypes.Structure):  # struct proc_bsdinfo, <sys/proc_info.h>
    _fields_ = [
        ("pbi_flags", ctypes.c_uint32),
        ("pbi_status", ctypes.c_uint32),
        ("pbi_xstatus", ctypes.c_uint32),
        ("pbi_pid", ctypes.c_uint32),
        ("pbi_ppid", ctypes.c_uint32),
        ("pbi_uid", ctypes.c_uint32),
        ("pbi_gid", ctypes.c_uint32),
        ("pbi_ruid", ctypes.c_uint32),
        ("pbi_rgid", ctypes.c_uint32),
        ("pbi_svuid", ctypes.c_uint32),
        ("pbi_svgid", ctypes.c_uint32),
        ("rfu_1", ctypes.c_uint32),
        ("pbi_comm", ctypes.c_char * 16),
        ("pbi_name", ctypes.c_char * 32),
        ("pbi_nfiles", ctypes.c_uint32),
        ("pbi_pgid", ctypes.c_uint32),
        ("pbi_pjobc", ctypes.c_uint32),
        ("e_tdev", ctypes.c_uint32),
        ("e_tpgid", ctypes.c_uint32),
        ("pbi_nice", ctypes.c_int32),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


# None off macOS (CI runs on Linux): nothing is then provable, so nothing is ever signalled.
_LIB: ctypes.CDLL | None = (
    ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True) if sys.platform == "darwin" else None
)


def _info(pid: int) -> _BsdInfo | None:
    if _LIB is None or pid <= 0:
        return None
    info = _BsdInfo()
    if _LIB.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(
        info
    ):
        return None
    return info if info.pbi_pid == pid else None


def _start(info: _BsdInfo) -> int:
    return info.pbi_start_tvsec * 1_000_000 + info.pbi_start_tvusec


def started(pid: int) -> int | None:
    """When `pid` started, in microseconds since the epoch; None when it is gone or not ours to see."""
    info = _info(pid)
    return None if info is None else _start(info)


def _everyone() -> list[_BsdInfo]:
    """Every process this user may inspect."""
    if _LIB is None:
        return []
    size = _LIB.proc_listpids(_PROC_ALL_PIDS, 0, None, 0)
    if size <= 0:
        return []
    buf = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int) + 32))()
    got = _LIB.proc_listpids(_PROC_ALL_PIDS, 0, buf, ctypes.sizeof(buf))
    found = (_info(pid) for pid in buf[: max(got, 0) // ctypes.sizeof(ctypes.c_int)] if pid > 0)
    return [info for info in found if info is not None]


def members(pgid: int) -> dict[int, int]:
    """The processes in group `pgid` that this user may inspect, each with its start time."""
    if _LIB is None or pgid <= 0:
        return {}
    size = _LIB.proc_listpids(_PROC_PGRP_ONLY, pgid, None, 0)
    if size <= 0:
        return {}
    buf = (ctypes.c_int * (size // ctypes.sizeof(ctypes.c_int) + 32))()
    got = _LIB.proc_listpids(_PROC_PGRP_ONLY, pgid, buf, ctypes.sizeof(buf))
    out: dict[int, int] = {}
    for pid in buf[: max(got, 0) // ctypes.sizeof(ctypes.c_int)]:
        info = _info(pid)
        if info is not None and info.pbi_pgid == pgid:  # still in it, not moved since the list
            out[pid] = _start(info)
    return out


@dataclass
class _Group:
    floor: int  # no process of ours started before this (just before the command was spawned)
    members: dict[int, int] = field(default_factory=dict)  # pid -> start time, as last seen


class ProcessGroups:
    """A frame's process groups: its running commands and what they left running (`cmd &`).

    A group is the frame's only while a process it saw (same pid, same start time) is still in it;
    each check that proves so takes the group's members as they are now, so the children a
    leftover spawns are known too. A group that cannot be proved is forgotten, never signalled:
    a stranger's group with a reused number is never killed, at the cost of a leftover whose every
    known process has exited going unkilled. Persisted (0600) beside the frame's folders, so the
    next helper life can kill what this one left.

    `hold` is the one exception, and lasts seconds: a stop's own signal can end every process seen
    in a group while a child one of them just started lives on. A group proved at the stop's start
    stays the frame's for that stop while anything in it started since its floor.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._groups: dict[int, _Group] = {}
        self._held: set[int] = set()  # proved at a stop's start (`hold`)
        with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
            raw = json.loads(path.read_text())
            for pgid, g in raw.items():
                members = {int(pid): int(at) for pid, at in g["members"].items()}
                self._groups[int(pgid)] = _Group(floor=int(g["floor"]), members=members)

    def __iter__(self):  # the group ids
        return iter(list(self._groups))

    def __len__(self) -> int:
        return len(self._groups)

    def spawned(self, pgid: int, floor: int) -> None:
        """A command's group, just spawned (its leader's id is the group's)."""
        at = started(pgid)
        self._groups[pgid] = _Group(floor=floor, members={pgid: at} if at is not None and at >= floor else {})
        self._save()

    def exited(self, pgid: int) -> None:
        """The command's leader has exited: what is left in its group now is what it left running.
        Nothing can have taken the group's number between the exit and this look (the kernel
        never reuses an id while a group holds it), so every member started since the spawn is
        taken as the frame's."""
        group = self._groups.get(pgid)
        if group is not None:
            group.members = {p: at for p, at in members(pgid).items() if at >= group.floor}
            if not group.members:
                del self._groups[pgid]
        self._save()

    def _proved(self, pgid: int) -> bool:
        group = self._groups[pgid]
        now = members(pgid)
        ours = {p: at for p, at in now.items() if at >= group.floor}
        seen = any(group.members.get(pid) == at for pid, at in now.items())
        if not (seen or (pgid in self._held and ours)):
            return False
        group.members = ours
        return True

    def prune(self) -> None:
        """Forget every group that is gone, or that can no longer be proved the frame's."""
        self.pids()

    def pids(self) -> dict[int, int]:
        """Every process of the groups still proved the frame's, as they are now: pid -> its group.
        A group that can no longer be proved is forgotten, as `prune` does. A process that left its
        group is still found while its parents lead back to one (`_descendants`), and its own
        group joins the frame's; one re-parented to launchd can be proved by nothing, and is
        nobody's. Written back only if that changed anything: a look may come ten times a second."""
        before = self._data()
        out: dict[int, int] = {}
        for pgid in list(self._groups):
            if self._proved(pgid):
                out.update(dict.fromkeys(self._groups[pgid].members, pgid))
            else:
                del self._groups[pgid]
        if out:
            self._descendants(out)
        if self._data() != before:
            self._save()
        return out

    def hold(self) -> None:
        """A stop is under way: take the tree as it is now, before the signal that follows ends what
        leads to the rest of it, and keep every group proved so far for the stop's few seconds."""
        self.pids()
        self._held |= set(self._groups)

    def kill(self) -> None:
        """SIGKILL for the whole tree, found through its parents while they live."""
        self.pids()
        self.signal(signal.SIGKILL)

    def _descendants(self, pids: dict[int, int]) -> None:
        """Add to `pids` each process whose ppid chain leads to one of them, in its own group,
        which the frame then holds (and kills) like any other. A process older than the group it
        descends from is no child of the frame's."""
        floors = {pid: self._groups[pgid].floor for pid, pgid in pids.items()}
        children: dict[int, list[_BsdInfo]] = {}
        for info in _everyone():
            children.setdefault(info.pbi_ppid, []).append(info)
        pending = list(pids)
        while pending:
            parent = pending.pop()
            for child in children.get(parent, ()):
                pid, at = int(child.pbi_pid), _start(child)
                if pid in pids or at < floors[parent]:
                    continue
                pgid = int(child.pbi_pgid)
                group = self._groups.setdefault(pgid, _Group(floor=floors[parent]))
                group.members[pid] = at
                pids[pid], floors[pid] = pgid, floors[parent]
                pending.append(pid)

    def signal(self, sig: int, only: int | None = None) -> None:
        """Send `sig` to every group still proved the frame's (or just group `only`), and no other."""
        for pgid in [only] if only is not None else list(self._groups):
            if pgid not in self._groups:
                continue
            try:
                if not self._proved(pgid):
                    raise ProcessLookupError
                os.killpg(pgid, sig)
            except (ProcessLookupError, PermissionError):
                del self._groups[pgid]
        self._save()

    def _data(self) -> dict[str, dict[str, Any]]:
        return {
            str(pgid): {"floor": g.floor, "members": {str(p): at for p, at in g.members.items()}}
            for pgid, g in self._groups.items()
        }

    def _save(self) -> None:
        if not self.path.parent.is_dir():
            return  # the frame is being removed
        data = self._data()
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
