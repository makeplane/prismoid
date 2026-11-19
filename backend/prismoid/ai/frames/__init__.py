# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Frames - the sandbox an agent works inside, behind one factory.

`new_frame(task_id, driver=..., profile=..., attempt=..., name=..., recording=...)` is the ONE place
a frame driver is chosen. The driver is the Machine's, not the process's:
an existing Machine's frame is reached through the driver its row names (`machines.driver`), and
`driver=None` - a new Machine, a wake's new frame, a throwaway frame - means
`settings.worker.frame_driver`:

* **docker** - the in-process `DockerFrame` (dev default; shared kernel, trusted workloads).
* **sandbar** - `SandbarFrame`, one container per frame through the sandbar daemon's Docker
  backend (its Firecracker microVM backend is experimental).
* **device** - `DeviceFrame`, the user's own Mac through `prismoid device helper` over a Unix socket
  (ADR-078): a fenced folder of theirs, nothing archived (`sleep_kind = "none"`).

Every driver satisfies the same `FrameDriver` protocol (`driver.py`), so every caller - execution,
verification, the dev self-tests - asks here and never names a backend. A distribution adds a
driver as one more row of `DRIVERS` (and, where it qualifies, a name in `EARLY_BOOT_DRIVERS` and a
module in `CLIENT_MODULES`); nothing else here names one.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import Final, Literal

from prismoid.ai import config
from prismoid.ai.frames.driver import ArchiveTooLarge, FrameDriver, FrameUnreachable
from prismoid.ai.frames.paper import PAPER_DRIVER, PaperFrame, PaperFull
from prismoid.schemas.machines import FrameDriverName, FrameInfo


@dataclass(frozen=True, slots=True)
class _Driver:
    """A driver, as this factory asks it: make a frame, say how they sleep, list them."""

    frame: Callable[..., FrameDriver]
    sleep_kind: Literal["archive", "native", "none"]
    inventory: Callable[[], Awaitable[list[FrameInfo]]]


def _docker() -> _Driver:
    from prismoid.ai.frames.docker_driver import DockerFrame, inventory

    return _Driver(DockerFrame, DockerFrame.sleep_kind, partial(asyncio.to_thread, inventory))


def _sandbar() -> _Driver:
    from prismoid.ai.frames.sandbar_driver import SandbarFrame, inventory

    return _Driver(SandbarFrame, SandbarFrame.sleep_kind, inventory)


def _device() -> _Driver:
    """A device frame's grants reach it through `TakesGrants.grant` (the Machine's lifecycle reads
    them from Postgres); its kind, from the labels `create` is given."""
    from prismoid.ai.frames.device_driver import DeviceFrame, inventory

    return _Driver(DeviceFrame, DeviceFrame.sleep_kind, inventory)


DRIVERS: Final[Mapping[str, Callable[[], _Driver]]] = {
    FrameDriverName.DOCKER: _docker,
    FrameDriverName.SANDBAR: _sandbar,
    FrameDriverName.DEVICE: _device,
}
"""Every driver, one row each, by the name `PRISMOID_WORKER_FRAME_DRIVER` and a Machine's row spell.
A row is a function so that only the driver asked for is imported."""

EARLY_BOOT_DRIVERS: Final[frozenset[str]] = frozenset()
"""Drivers whose every profile is one computer template, so a task's first Machine may start when its
turn is picked up (ADR-070, `machines.boots_early`): the boot cannot guess a frame the first step would
re-make. None here: Docker's and sandbar's profiles are different images (a sandbar frame born with
no list is airtight for good), and a device task never boots ahead of its first step's checks."""

CLIENT_MODULES: Final[tuple[str, ...]] = ()
"""Driver modules that keep a client per event loop and close it with `close_client()`. None here:
docker, sandbar and the device dial per call."""


def _named(driver: str | None) -> str:
    """The driver to use: the one named (an existing Machine's), else this worker's."""
    return driver or config.current().worker.frame_driver


def _driver(name: str | None) -> _Driver:
    chosen = _named(name)
    if chosen not in DRIVERS:
        raise ValueError(f"unknown frame_driver {chosen!r}; expected one of {', '.join(DRIVERS)}")
    return DRIVERS[chosen]()


def require_driver(driver: str | None = None) -> None:
    """Load `driver` (None: this worker's) or raise, naming what is missing. A worker calls it at
    boot: an opt-in driver whose package is absent refuses the boot instead of failing every step."""
    _driver(driver)


async def close_clients() -> None:
    """Close this loop's driver clients (a client's channel belongs to the loop that made it). Only a
    driver in `CLIENT_MODULES` keeps one, and a driver this process never imported made none, so
    nothing is imported here. A short-lived loop that touched frames calls it before `asyncio.run` ends."""
    for name in CLIENT_MODULES:
        module = sys.modules.get(name)
        if module is not None:
            await module.close_client()


def new_frame(
    task_id: str,
    *,
    driver: str | None = None,
    profile: str = "base",
    attempt: int = 1,
    name: str | None = None,
    recording: str | None = None,
) -> FrameDriver:
    """The ONE place a driver is chosen. `driver` is an existing Machine's (its row's `driver`);
    None is this worker's, for a new Machine or a throwaway frame. `profile` picks the image; `name`
    overrides the default `frame-{task_id}` (a Machine's frame is `machine-{id}-{epoch}`);
    `recording` names the recording folder (`recording-{recording or attempt}` - per step and
    attempt on a Machine)."""
    return _driver(driver).frame(
        task_id=task_id, profile=profile, attempt=attempt, name=name, recording=recording
    )


def new_paper(name: str, *, max_bytes: int) -> FrameDriver:
    """A step's files with no computer under them (`paper.PaperFrame`): never a deployment's
    driver, and never a Machine's frame - the step that cannot use a computer asks for it by need."""
    return PaperFrame(name, max_bytes=max_bytes)


def sleep_kind(driver: str | None = None) -> Literal["archive", "native", "none"]:
    """How `driver`'s frames sleep (`FrameDriver.sleep_kind`), read off its class without making a
    frame: what the reconciler asks of a driver whose rows it judges (`schemas.machines.persistent`)."""
    return _driver(driver).sleep_kind


def ensure_ready(profile: str = "base", driver: str | None = None) -> None:
    """Make sure `driver` (None: this worker's) can hand out `profile` frames before `frame.create()`;
    only docker builds here (`ensure_image`), the other drivers own their images."""
    if _named(driver) == FrameDriverName.DOCKER:
        from prismoid.ai.frames.docker_driver import ensure_image

        ensure_image(profile)


async def inventory(drivers: Iterable[str] | None = None) -> list[FrameInfo]:
    """What the providers run that is ours - the reconciler's listing, each frame stamped
    with the driver that listed it (`FrameInfo.driver`). `drivers` names the providers to ask:
    every driver a Machine row names a frame on, and this worker's; None
    asks only this worker's. A driver that cannot be listed raises, and so does a name this factory
    does not know: one call never hands back half a world. A listing is per driver: the
    reconciler asks each driver on its own, so a driver that is down costs only its own
    frames and Machines that pass, never every other driver's.

    It replaced `reap_orphans`, the boot's kill-all: that assumed one worker and would destroy a
    live peer's frames; the reconciler (`operations/machines/reconcile.py`) judges each frame
    against the rows and destroys only what Postgres disowns."""
    asked = {_named(d) for d in drivers} if drivers is not None else {_named(None)}
    out: list[FrameInfo] = []
    for driver in sorted(asked):
        out += await _driver(driver).inventory()
    return out


async def destroy_ref(ref: str, *, driver: str | None = None) -> None:
    """Destroy one frame by its provider reference, on the driver that holds it (None: this
    worker's); a missing frame is already destroyed."""
    await new_frame("", driver=driver, name=ref).destroy()


__all__ = [
    "CLIENT_MODULES",
    "EARLY_BOOT_DRIVERS",
    "PAPER_DRIVER",
    "ArchiveTooLarge",
    "FrameDriver",
    "FrameUnreachable",
    "PaperFrame",
    "PaperFull",
    "close_clients",
    "destroy_ref",
    "ensure_ready",
    "inventory",
    "new_frame",
    "new_paper",
    "require_driver",
    "sleep_kind",
]
