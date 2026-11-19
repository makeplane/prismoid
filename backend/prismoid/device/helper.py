# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What the device helper runs, behind the door `runner/device_door.py`
serves on its socket. No transport here: `Helper` takes and gives plain values.

Commands run fenced (`exec.py`), files pass the file door (`files.py`), and the frames' egress
proxy (`egress.py`) serves on loopback for the helper's lifetime. A browser frame gets a window in
the Prismoid Chrome (`chrome.py`, launched on the first one), and its agent drives that window only
(`cdp_relay.py`). A frame with the Docker grant reaches Docker only through its own filtering socket
(`docker_proxy.py`), served while it has the grant. A frame's own servers (`ports.py`) are the only
loopback its window and its proxy credential reach, and a frame's rw grants are walked for planted
repositories and links (`plants.py`) after every command, when its services stop and when it ends.

Every route, healthz and the CDP relay's WebSocket included, requires `KEY_HEADER` matching
`home/helper.key`: the socket is 0600 already, and one rule has no exceptions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import socket
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

from prismoid import keyfile
from prismoid.conf import settings
from prismoid.device import cdp_relay, docker_proxy
from prismoid.device import exec as device_exec
from prismoid.device.chrome import Chrome, ChromeError, ChromeMissing, missing
from prismoid.device.egress import DeviceEgress
from prismoid.device.frames import DeviceFrameState, FrameRegistry, ref_for
from prismoid.device.ports import FramePorts
from prismoid.schemas.device import ExecIn, ExecOut, FrameSpecIn, GrantsIn
from prismoid.schemas.machines import FrameKind

LOG = logging.getLogger(__name__)

KEY_HEADER = "X-Prismoid-Helper-Key"
KEY_FILE = "helper.key"


def read_device_name() -> str:
    """`scutil --get ComputerName`, else the hostname. Read once, at helper start."""
    try:
        name = subprocess.run(
            ["scutil", "--get", "ComputerName"], capture_output=True, text=True, timeout=5, check=True
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return name or socket.gethostname()


class HelperKeyError(RuntimeError):
    """The helper key is unusable; the helper refuses to start rather than open its door."""


def ensure_key(home: Path) -> str:
    """The helper's shared secret: created 0600 on first start, reused after. An empty, loosely-moded,
    foreign or symlinked key file is a startup error - an auth door fails closed (`prismoid.keyfile`)."""
    try:
        return keyfile.ensure(
            home / KEY_FILE, lambda: secrets.token_urlsafe(32), restart="start the helper again"
        )
    except keyfile.KeyFileError as err:
        raise HelperKeyError(str(err)) from err


def private_dir(path: Path) -> Path:
    """A directory only the owner may enter (the socket's home), even if it existed looser."""
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


class Helper:
    """One helper process: its frames, proxies and Chrome wired together, and the order in which
    each of them starts and stops."""

    def __init__(
        self,
        home: Path,
        device_name: Callable[[], str] = read_device_name,
        chrome_path: str | None = None,
    ) -> None:
        self.key = ensure_key(home)
        self.name = device_name()
        private_dir(home / "run")
        self.registry = FrameRegistry(home)
        self.dockers = docker_proxy.DockerProxies(home)
        # Never a frame's: Prismoid's API and this helper's own proxy (`egress`, bound on first use).
        self.ports = FramePorts(
            self.registry.frames,
            self.dockers.published,
            never=lambda: (settings.local.api_port, self.egress.port, self.egress.socket_port),
        )
        self.egress = DeviceEgress(self.registry.frames, ports=self.ports)
        self.chrome = Chrome(
            home,
            chrome_path or settings.local.chrome_path,
            self.egress.chrome_url,
            fence=cdp_relay.RequestFence(self.registry.frames, self.ports, on_asset=self.egress.pass_asset),
            socket_proxy=self.egress.chrome_socket_origin,
        )
        self._halting: asyncio.Task[None] | None = None

    def frame(self, ref: str) -> DeviceFrameState | None:
        return self.registry.frames.get(ref)

    def acting(self, ref: str) -> DeviceFrameState | None:
        """The frame of a request that is the agent at work, or that says it is not parked: no
        hand-off outlives one."""
        if (state := self.frame(ref)) is not None:
            self.chrome.take_back(state.ref)
        return state

    def out(self, state: DeviceFrameState) -> dict[str, object]:
        """A frame as the worker reads it."""
        return self.registry.out(state, self.name).model_dump(mode="json")

    def all_out(self) -> list[dict[str, object]]:
        return [self.out(state) for state in self.registry.frames.values()]

    async def create_frame(self, spec: FrameSpecIn) -> DeviceFrameState:
        """The frame `spec` asks for, its folders baselined and its window open. Raises `GrantRefused`
        for a grant the helper will not take, `ChromeError` when a browser frame cannot have its
        window: a `ChromeMissing` before anything is made."""
        browser = spec.profile == "browser"
        if browser and (why := missing(self.chrome.chrome_path)):
            raise ChromeMissing(why)
        before = set(self.registry.frames)
        # A Machine's ref is its task's: a destroy of it still in flight finishes first (its lock),
        # so the frame made here is never the one that destroy drops. A throwaway ref is new.
        machine = spec.kind == FrameKind.MACHINE
        async with self.dockers.lock(ref_for(spec)) if machine else contextlib.nullcontext():
            state = self.registry.create(spec)
            self.dockers.revive(state.ref)
            self.chrome.reopen(state.ref)
            await asyncio.to_thread(state.baseline_plants)  # what its folders hold now is the person's
        await self.dockers.ensure(state)  # with the Docker grant, its socket is there for the first command
        if browser:
            try:
                await self.chrome.window_for(state.ref)
            except ChromeError:
                if state.ref not in before:
                    await self.dockers.close(state.ref)
                    self.registry.destroy(state.ref)  # no half-made frame: the worker's retry makes it whole
                raise
        return state

    async def delete_frame(self, ref: str) -> bool:
        """The frame ends: its window, what runs in it, what it planted, what its proxy made. False
        when there was no such frame, every other step having been taken anyway."""
        await self.chrome.close_window(ref)
        if (state := self.registry.frames.get(ref)) is not None:
            await self._stop(state)
            await state.last_walk()  # nothing it planted stays in the person's folder after it
        # One lock spans the Docker side and the registry's: no POST for this ref lands between.
        async with self.dockers.lock(ref):
            pending = await self.dockers.end(ref)
            destroyed = self.registry.destroy(ref)
        # What its proxy made (journaled, still labelled) goes, now or at a later try; nothing else.
        await self.dockers.clean(ref, pending)
        return destroyed

    async def _stop(self, state: DeviceFrameState) -> None:
        """What the frame left running dies and its containers stop (they have no fence, and write
        its grants up to their last moment): only after this is a walk for plants the last one."""
        self.registry.stop_services(state)
        self.ports.forget(state.ref)
        await self.dockers.stop(state.ref)

    async def stop_services(self, state: DeviceFrameState) -> None:
        """The Machine is going to sleep: what the frame left running dies, its containers stop,
        its folders are walked for plants, the frame stays."""
        await self._stop(state)
        await state.sweep_within()

    async def run(self, state: DeviceFrameState, req: ExecIn, gone: Callable[[], Awaitable[bool]]) -> ExecOut:
        """One fenced command (`exec.run`). `gone` says whoever asked has left."""
        return await device_exec.run(
            state, req, proxy=self.egress, docker=self.dockers, ports=self.ports, gone=gone
        )

    async def regrant(self, state: DeviceFrameState, req: GrantsIn) -> None:
        """The task's folders and Docker grant as Postgres holds them now, re-pushed by
        the worker when they differ from the frame's. Raises `GrantRefused`."""
        await asyncio.to_thread(self.registry.regrant, state, req)
        await self.dockers.ensure(state)  # a revoked grant's socket goes now, not at the next command

    async def start(self) -> None:
        """Before the door opens."""
        await self.egress.serve()  # exec also starts it, for a host that runs no lifespan
        # The last life may have stopped nothing. Before the door opens: no command of this life
        # can have started what this stops.
        await self.dockers.stop_all(list(self.registry.frames))
        self.dockers.start(self.registry.frames)  # cleanups a frame's end left for Docker to come back

    async def _halt(self) -> None:
        """The order is the point. The commands first, so nothing starts a container after the stop
        for containers. The containers next, beside Chrome (a new life relaunches it), since neither
        waits on the other. The plant walk last: only then has everything that writes the folders
        stopped. It is the normal end's walk, bounded the same way, and what it finds is on disk for
        the next life, however late it finds it. All of it fits the supervisor's `stop_timeout_s`.
        Nothing one frame or one step raises stops the rest. From the first line Chrome stays down."""
        self.chrome.no_more_launches()
        try:
            await self.registry.stop()
        except Exception:
            LOG.exception("the helper's stop went on past a failure")
        states = list(self.registry.frames.values())
        steps = await asyncio.gather(
            self.dockers.stop_all(list(self.registry.frames)), self.chrome.close(), return_exceptions=True
        )
        walks = await asyncio.gather(*(state.sweep_within() for state in states), return_exceptions=True)
        for failed in (e for e in (*steps, *walks) if isinstance(e, BaseException)):
            LOG.error("the helper's stop went on past a failure", exc_info=failed)

    async def halt(self) -> None:
        """Stop what runs on this computer (`_halt`), once however often it is asked."""
        self._halting = self._halting or asyncio.create_task(self._halt())
        await self._halting

    async def shutdown(self) -> None:
        """After the door has closed: each step runs, whatever the one before it raised."""
        for step in (self.halt, self.ports.close, self.dockers.close_all, self.egress.close):
            try:
                await step()
            except Exception:
                LOG.exception("the helper's shutdown went on past a failure")
