# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a driver cannot do it does not claim: a verb only some drivers have is its own Protocol, and
the driver table is one row per driver."""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from prismoid.ai import frames
from prismoid.ai.frames import (
    DRIVERS,
    close_clients,
    inventory,
    layout,
    new_frame,
    sleep_kind,
)
from prismoid.ai.frames.device_driver import DeviceFrame
from prismoid.ai.frames.docker_driver import DockerFrame
from prismoid.ai.frames.driver import (
    Checkpoints,
    FrameDriver,
    NamesItsDevice,
    NativeSleep,
    StagesCopies,
)
from prismoid.ai.frames.sandbar_driver import SandbarFrame
from prismoid.conf import settings

NAMES = {"docker", "sandbar", "device"}
CLOUD: dict[str, Any] = {"docker": DockerFrame, "sandbar": SandbarFrame}
ALL: dict[str, Any] = {**CLOUD, "device": DeviceFrame}


def test_the_table_is_every_driver_and_each_row_is_its_own() -> None:
    assert set(DRIVERS) == NAMES
    for name, kind in ALL.items():
        frame = new_frame("t", driver=name)
        assert type(frame) is kind and frame.driver == name and sleep_kind(name) == kind.sleep_kind
        assert isinstance(frame, FrameDriver)


def test_the_worker_default_reads_the_same_table(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings.worker, "frame_driver", "device")
    assert type(new_frame("t")) is DeviceFrame and sleep_kind() == "none"


async def test_every_door_refuses_an_unknown_driver_in_the_same_words() -> None:
    said = set()
    for ask in (lambda: new_frame("t", driver="e2b"), lambda: sleep_kind("e2b")):
        with pytest.raises(ValueError, match="unknown frame_driver 'e2b'") as refused:
            ask()
        said.add(str(refused.value))
    with pytest.raises(ValueError, match="unknown frame_driver 'e2b'") as refused:
        await inventory(["e2b"])
    said.add(str(refused.value))
    assert len(said) == 1
    assert all(name in next(iter(said)) for name in NAMES)


def test_a_device_frame_is_neither_archived_nor_slept_natively() -> None:
    frame = DeviceFrame("t")
    assert not isinstance(frame, Checkpoints) and not isinstance(frame, NativeSleep)
    for verb in ("archive", "unpack", "suspend", "resume"):
        assert not hasattr(frame, verb)


@pytest.mark.parametrize("driver", ["docker", "sandbar"])
def test_an_archive_driver_has_no_native_sleep(driver: str) -> None:
    frame = new_frame("t", driver=driver)
    assert frame.sleep_kind == "archive"
    assert isinstance(frame, Checkpoints) and not isinstance(frame, NativeSleep)


def test_only_the_device_stages_copies_and_names_its_computer() -> None:
    device = DeviceFrame("t")
    assert isinstance(device, StagesCopies) and isinstance(device, NamesItsDevice)
    for name in CLOUD:
        frame = new_frame("t", driver=name)
        assert not isinstance(frame, StagesCopies) and not isinstance(frame, NamesItsDevice)


def test_the_epoch_ledger_has_one_home() -> None:
    assert f"{layout.TASK}/.browser-epochs" == layout.EPOCHS


async def test_closing_the_clients_imports_no_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    """A loop that never made a client-keeping driver's frame has no client to close, so nothing is
    imported."""
    monkeypatch.setattr(frames, "CLIENT_MODULES", ("prismoid.ai.frames.remote_driver",))
    monkeypatch.delitem(sys.modules, "prismoid.ai.frames.remote_driver", raising=False)
    await close_clients()
    assert "prismoid.ai.frames.remote_driver" not in sys.modules


async def test_closing_the_clients_closes_each_loaded_driver_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """A driver module this loop imported closes its client; one it never imported is skipped."""
    closed: list[str] = []

    async def close_client() -> None:
        closed.append("remote")

    driver = ModuleType("prismoid.ai.frames.remote_driver")
    driver.__dict__["close_client"] = close_client
    monkeypatch.setitem(sys.modules, driver.__name__, driver)
    monkeypatch.delitem(sys.modules, "prismoid.ai.frames.absent_driver", raising=False)
    monkeypatch.setattr(frames, "CLIENT_MODULES", (driver.__name__, "prismoid.ai.frames.absent_driver"))
    await close_clients()
    assert closed == ["remote"]
