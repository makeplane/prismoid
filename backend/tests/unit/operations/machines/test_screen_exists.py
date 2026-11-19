# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Whether a Machine has a screen at all: one rule, `machines.screen.has_screen`
- a browser Machine on a driver with a screen door. Docker's is the hijacked exec, sandbar's the
display route, open only when the operator says sandbar's side is configured
(`PRISMOID_COMPUTER_USE_SANDBAR_SCREEN`: sandbard holds the display keys and the API reaches it). On
a driver without one nobody can claim the Machine, and a step on it has no lease to yield to and no
person to hand the browser to (`Acquired.screen`)."""

from __future__ import annotations

from typing import Any

import pytest

from prismoid.conf import settings
from prismoid.operations.machines import Acquired
from prismoid.operations.machines.screen import has_screen


@pytest.mark.parametrize(
    ("profile", "driver", "sandbar_screen", "screen"),
    [
        ("browser", "docker", False, True),
        ("browser", "sandbar", True, True),
        ("browser", "sandbar", False, False),
        ("browser", "e2b", True, False),
        ("base", "docker", True, False),
        ("base", "sandbar", True, False),
    ],
)
def test_only_a_browser_machine_on_a_driver_with_a_screen_door_has_one(
    monkeypatch: pytest.MonkeyPatch, profile: str, driver: str, sandbar_screen: bool, screen: bool
) -> None:
    monkeypatch.setattr(settings.computer_use, "sandbar_screen", sandbar_screen)
    assert has_screen(profile, driver) is screen


class _Named:
    """A frame that only names its driver: all `Acquired.screen` reads."""

    def __init__(self, driver: str) -> None:
        self.driver = driver


@pytest.mark.parametrize(
    ("driver", "sandbar_screen", "screen"),
    [("docker", False, True), ("sandbar", True, True), ("sandbar", False, False), ("e2b", True, False)],
)
def test_a_steps_machine_has_the_screen_its_frames_door_gives(
    monkeypatch: pytest.MonkeyPatch, driver: str, sandbar_screen: bool, screen: bool
) -> None:
    """The frame names its driver - the Machine row's - so a step yields only to a lease someone
    could take, whatever this worker is configured to make (ADR-063)."""
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    monkeypatch.setattr(settings.computer_use, "sandbar_screen", sandbar_screen)
    machine = Acquired(frame=_Named(driver), kind="machine", label="", profile="browser")  # pyright: ignore[reportArgumentType]
    assert machine.screen is screen


def test_the_doors_and_their_kinds() -> None:
    """A pin on a constant table (ADR-065): Docker's and sandbar's one stream carries
    the holder's input too, so neither serves a separate control stream or a hand-back."""
    from prismoid.ai.frames import screen as frames_screen

    assert set(frames_screen.DOORS) == {"docker", "sandbar"}
    for driver in ("docker", "sandbar"):
        assert frames_screen.DOORS[driver].kinds == {"watch"}
        assert frames_screen.DOORS[driver].hand_back is None


@pytest.mark.parametrize("open_here", [True, False])
def test_a_machine_whose_door_has_a_control_stream_has_a_screen_where_that_door_is_open(
    monkeypatch: pytest.MonkeyPatch, open_here: bool
) -> None:
    """A remote driver's door that serves its holder on a control stream: the Machine has a screen,
    and is driven through that stream, exactly where the door is open."""
    from prismoid.ai.frames import screen as frames_screen

    async def never(
        ref: str, kind: frames_screen.ScreenKind, display_ticket: frames_screen.DisplayTicket
    ) -> Any:
        raise AssertionError("no stream is opened here")

    door = frames_screen.ScreenDoor(frozenset({"watch", "control"}), lambda: open_here, never)
    monkeypatch.setitem(frames_screen.DOORS, "remote", door)
    machine = Acquired(frame=_Named("remote"), kind="machine", label="", profile="browser")  # pyright: ignore[reportArgumentType]
    assert machine.screen is open_here
    assert frames_screen.has_control("remote") is open_here
    assert not frames_screen.has_control("docker")


@pytest.mark.parametrize("kind", ["seat", "throwaway", "legacy"])
def test_a_frame_that_is_not_the_tasks_machine_never_has_a_screen(
    monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    frame = Acquired(frame=object(), kind=kind, label="", profile="browser")  # pyright: ignore[reportArgumentType]
    assert frame.screen is False
