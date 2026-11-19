# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A frame's servers: the loopback ports it listens on, which are the
only loopback ports its Chrome window and its proxy credential reach.

`listening_ports` asks `lsof -nP -a -p <pids> -iTCP -sTCP:LISTEN` (in its field output, `-F pn`)
about the processes of the frame's own process groups (`ProcessGroups.pids`: proved by start time,
never a stranger's, and found by their parents after a `setsid`). `FramePorts` keeps what each
frame listens on for `CACHE_S`, as (address, port): macOS lets a frame bind `[::1]:Y` beside a
stranger's `127.0.0.1:Y`, so a port number alone proves nothing, and only an address the frame
really listens on is ever dialed. With those are the addresses its journaled containers publish
(`DockerProxies.published`: a compose dev server listens through Docker, not through a frame
process). It looks again before it refuses one it does not know. Prismoid's own ports - the API, the
egress proxy - are never a frame's (`never`); Postgres has no port (a Unix socket), nor has the
helper.

The fence lets a command bind any address (Seatbelt's `localhost` in a bind filter also matches
0.0.0.0 and `::`), so a server listening beyond loopback, where the LAN reaches it, is found here
and its process group killed - at the end of every command and every `WATCH_S` while the frame has
process groups. The refusal is kept in the frame's denials, and the agent is told in its next tool
result to bind 127.0.0.1.

What a frame left running can plant as well as listen, and nothing else would walk its folders while
it has no command running, for a day under a parked step. So the same watch walks them
(`WALK_EVERY`), never under a running command: a link a build keeps for a moment is the walk after
it's to judge, and a move under it would fail the build.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import signal
import subprocess
import time
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from prismoid.device.frames import DeviceFrameState
from prismoid.egress_relay import loopback_host, own_literals

LOG = logging.getLogger(__name__)

LSOF = "/usr/sbin/lsof"  # a seam for tests
CACHE_S: Final = 2.0  # a frame's ports, as last looked at, are good this long
WATCH_S: Final = 2.0  # how often a frame with process groups is looked at between its commands
WALK_EVERY: Final = 15  # such looks to a walk of its folders: a stat per folder, so not at each
# A look an unknown port forces starts at most this often per frame: a page probing loopback ports
# one by one waits for the next look (one `lsof` per this), and never gets an answer older than
# its own question - a server that bound a moment ago is not refused.
_FRESH_S: Final = 0.1
_LSOF_TIMEOUT_S: Final = 10.0

WIDE_REASON: Final = "a server listened beyond loopback, where the network reaches it: stopped"
WIDE_NOTICE: Final = (
    "prismoid: stopped the server listening on {where}: a server here must listen on 127.0.0.1 only "
    "(the network could reach it). Start it again bound to 127.0.0.1 (e.g. `--host 127.0.0.1`)."
)

# What a frame listens on, each (address, port).
Own = frozenset[tuple[str, int]]
# What a frame's own containers publish, by its ref (`DockerProxies.published`).
Published = Callable[[str], Awaitable[set[tuple[str, int]]]]


@dataclass(frozen=True)
class Listener:
    pid: int
    address: str  # as lsof prints it, unbracketed: `127.0.0.1`, `::1`, `*` (any), `192.168.1.5`
    port: int

    @property
    def loopback(self) -> bool:
        return self.address != "*" and loopback_host(self.address)

    @property
    def where(self) -> str:
        return f"[{self.address}]:{self.port}" if ":" in self.address else f"{self.address}:{self.port}"


def _canonical(address: str) -> str:
    """As the relay spells an address: an IPv4-mapped one is its IPv4."""
    try:
        addr = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(addr)


def parse_lsof(text: str) -> list[Listener]:
    """lsof's field output: a `p<pid>` line, then per socket an `f<fd>` line and an
    `n<address>:<port>` line (`*:8080`, `127.0.0.1:3000`, `[::1]:5173`). Anything else is skipped."""
    out: list[Listener] = []
    pid: int | None = None
    for line in text.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid = int(value) if value.isdigit() else None
        elif tag == "n" and pid is not None and "->" not in value:
            address, sep, port = value.rpartition(":")
            if sep and address and port.isdigit():
                out.append(Listener(pid, _canonical(address.strip("[]")), int(port)))
    return out


def listeners(pids: Iterable[int]) -> list[Listener]:
    """The TCP listeners of `pids` (and of no other process)."""
    wanted = sorted({p for p in pids if p > 0})
    if not wanted:
        return []
    argv = [LSOF, "-nP", "-a", "-p", ",".join(map(str, wanted)), "-iTCP", "-sTCP:LISTEN", "-F", "pn"]
    try:
        # Exit 1 is "nothing found for one of the pids" (it exited): what was found still stands.
        done = subprocess.run(argv, capture_output=True, text=True, timeout=_LSOF_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        LOG.warning("lsof could not list the frames' servers: %s", e)
        return []
    mine = set(wanted)
    return [found for found in parse_lsof(done.stdout) if found.pid in mine]


def listening_ports(pids: Iterable[int]) -> set[int]:
    return {found.port for found in listeners(pids)}


class FramePorts:
    """Each frame's own ports, looked at at most every `CACHE_S` (or sooner, for a port it did not
    know). Every look also stops the frame's servers listening beyond loopback (`_stop_wide`)."""

    def __init__(
        self,
        frames: Mapping[str, DeviceFrameState],
        published: Published | None = None,
        never: Callable[[], Collection[int]] = tuple,
    ) -> None:
        self._frames = frames
        self._published = published
        self._never = never
        self._seen: dict[str, tuple[float, Own]] = {}  # ref -> (looked at, what it listens on)
        self._looking: dict[str, tuple[float, asyncio.Task[Own]]] = {}  # ref -> (began, it)
        self._watch: asyncio.Task[None] | None = None

    async def of(self, frame: DeviceFrameState, *, fresh: bool = False) -> Own:
        """What the frame listens on: as last looked at while that is younger than `CACHE_S`; `fresh`, from
        a look that began after this question (waiting out `_FRESH_S` since the last one)."""
        seen = self._seen.get(frame.ref)
        asked = time.monotonic()
        if seen is not None:
            if not fresh and asked - seen[0] < CACHE_S:
                return seen[1]
            if fresh and (wait := seen[0] + _FRESH_S - asked) > 0:
                await asyncio.sleep(wait)
        return await self.look(frame, since=asked if fresh else 0.0)

    async def literals(self, frames: Iterable[DeviceFrameState], host: str, port: int) -> list[str]:
        """The addresses `host` names on `port` that these frames listen on: the only ones to dial.
        Asked of what was last seen first, then looked at afresh before none is the answer."""
        if port in self._never():
            return []
        frames = list(frames)
        for fresh in (False, True):
            found = [a for f in frames for a in own_literals(host, port, await self.of(f, fresh=fresh))]
            if found:
                return list(dict.fromkeys(found))
        return []

    async def same_frame(
        self, frames: Iterable[DeviceFrameState], origin: tuple[str, int], host: str, port: int
    ) -> bool:
        """Does one frame listen on both `origin` and the target? (Each looked at afresh once.)"""
        if port in self._never() or origin[1] in self._never():
            return False
        frames = list(frames)
        for fresh in (False, True):
            for frame in frames:
                own = await self.of(frame, fresh=fresh)
                if own_literals(*origin, own) and own_literals(host, port, own):
                    return True
        return False

    def owner(self, frames: Iterable[DeviceFrameState], host: str, port: int) -> DeviceFrameState | None:
        """The frame last seen listening on this, if any."""
        for frame in frames:
            if own_literals(host, port, self._seen.get(frame.ref, (0.0, frozenset()))[1]):
                return frame
        return None

    async def look(self, frame: DeviceFrameState, *, since: float = 0.0) -> Own:
        """Look now - one look per frame at a time. A caller joins the look in progress, unless it
        began before `since`: then it waits that one out and takes the next."""
        while (running := self._looking.get(frame.ref)) is not None and running[0] < since:
            await asyncio.wait({running[1]})
        if running is None:
            began = time.monotonic()
            task = asyncio.create_task(self._look(frame, began))
            task.add_done_callback(lambda _t, ref=frame.ref: self._looking.pop(ref, None))
            running = self._looking[frame.ref] = (began, task)
        return await asyncio.shield(running[1])

    async def _look(self, frame: DeviceFrameState, began: float) -> Own:
        pids = frame.groups.pids()
        found = await asyncio.to_thread(listeners, pids) if pids else []
        self._stop_wide(frame, pids, found)
        own = {(f.address, f.port) for f in found if f.loopback}
        if self._published is not None:
            own |= await self._published(frame.ref)
        never = set(self._never())
        mine = frozenset(entry for entry in own if entry[1] not in never)
        if frame.ref in self._frames:  # a frame destroyed meanwhile is not remembered
            self._seen[frame.ref] = (began, mine)
        return mine

    def _stop_wide(self, frame: DeviceFrameState, pids: Mapping[int, int], found: list[Listener]) -> None:
        wide = [f for f in found if not f.loopback]
        for pgid in dict.fromkeys(pids[f.pid] for f in wide):
            frame.groups.signal(signal.SIGKILL, only=pgid)  # proved again before it is sent
        for where in dict.fromkeys(f.where for f in wide):
            LOG.warning("frame %s listened on %s: stopped", frame.ref, where)
            frame.deny(where, WIDE_REASON)
            frame.notices.append(WIDE_NOTICE.format(where=where))

    def forget(self, ref: str) -> None:
        """The frame has ended (or its services stopped): nothing of it is remembered."""
        self._seen.pop(ref, None)

    def watch(self) -> None:
        """Look at every frame with process groups every `WATCH_S`, until none has any."""
        if (self._watch is None or self._watch.done()) and self._busy():
            self._watch = asyncio.create_task(self._every())

    def _busy(self) -> list[DeviceFrameState]:
        return [f for f in self._frames.values() if len(f.groups)]

    async def _every(self) -> None:
        looks = 0
        while self._busy():
            await asyncio.sleep(WATCH_S)
            looks += 1
            for frame in self._busy():
                await self._watched(frame, walk=looks % WALK_EVERY == 0)

    async def _watched(self, frame: DeviceFrameState, *, walk: bool) -> None:
        try:
            await self.look(frame)
            if walk and not frame.journal.running:
                await frame.sweep_within(0.0)  # what it finds is told with the next result, as ever
        except Exception:
            LOG.exception("could not look at frame %s's servers", frame.ref)

    async def close(self) -> None:
        looks = [task for _, task in self._looking.values()]
        tasks: list[asyncio.Task[Any]] = [*looks, *([self._watch] if self._watch is not None else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._watch = None
