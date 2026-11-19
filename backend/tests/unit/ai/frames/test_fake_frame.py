# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""FakeFrame (tests/support/fakes/fake_frames.py): the shared in-memory driver later tasks lean on."""

import io
from typing import Any

import pytest

from prismoid.conf import settings
from prismoid.schemas.machines import LABEL_KIND
from tests.support.fakes.fake_frames import (
    NATIVE,
    SANDBAR,
    FakeFrame,
    FrameStopped,
    NativeFakeFrame,
    SandbarFakeFrame,
    frame_class,
)


@pytest.fixture(autouse=True)
def _world() -> None:  # pyright: ignore[reportUnusedFunction] - autouse: pytest calls it, not us
    FakeFrame.world.clear()
    FakeFrame.created.clear()


async def test_archive_and_unpack_round_trip_work_between_two_frames() -> None:
    a = FakeFrame("t1", name="machine-a-1")
    await a.create(labels={LABEL_KIND: "machine"})
    await a.write_bytes("/work/notes.db", b"sqlite bytes")
    buf = io.BytesIO()
    await a.archive("/work", buf)
    await a.destroy()
    b = FakeFrame("t1", name="machine-a-2")
    await b.create()
    buf.seek(0)
    await b.unpack("/work", buf)
    assert await b.read_bytes("/work/notes.db") == b"sqlite bytes"


async def test_bind_raises_when_the_frame_is_gone_and_inventory_lists_the_living() -> None:
    a = FakeFrame("t1", name="frame-x")
    await a.create(labels={LABEL_KIND: "seat"})
    assert [f.ref for f in FakeFrame.inventory()] == ["frame-x"]
    await a.destroy()
    with pytest.raises(RuntimeError):
        await FakeFrame("t1", name="frame-x").bind()


async def test_find_newer_than_the_marker_lists_only_later_writes() -> None:
    f = FakeFrame("t1")
    await f.create()
    await f.write_bytes("/work/old.txt", b"1")
    await f.write_bytes("/task/.step-start", b"", owner="system", mode=0o444)
    await f.write_bytes("/work/new.txt", b"2")
    out = await f.exec("find /work -newer /task/.step-start -type f")
    assert out.stdout.split() == ["/work/new.txt"]


async def test_allow_drops_a_stale_version() -> None:
    f = FakeFrame("t1")
    await f.create(egress_version=3)
    assert await f.allow(["a.example"], 5) is True
    assert await f.allow(["b.example"], 4) is False
    assert f.egress_hosts == ["a.example"] and f.egress_version == 5


async def test_an_unsynced_fakes_unpinned_allow_applies_without_a_version() -> None:
    f = FakeFrame("t1")  # never created or bound: no version synced, like `DockerFrame` after bind
    assert await f.allow(["a.example"]) is True
    assert f.egress_hosts == ["a.example"] and f.egress_version == 0


async def test_a_bound_fakes_push_reaches_the_live_frame() -> None:
    """The provider keeps the allowlist: a driver `bind()` attached pushes to the live frame."""
    live = FakeFrame("t1")
    await live.create(allow_domains=[])
    bound = FakeFrame("t1")
    await bound.bind()
    assert await bound.allow(["a.example"]) is True
    assert live.egress_hosts == ["a.example"] and live.egress_version == bound.egress_version


async def test_a_native_fake_keeps_everything_while_suspended_and_refuses_work() -> None:
    frame = NativeFakeFrame("t1", name="machine-n-1")
    await frame.create()
    await frame.write_bytes("/work/tab.txt", b"kept")
    await frame.suspend()
    with pytest.raises(FrameStopped):
        await frame.exec("true")
    await frame.resume()
    assert await frame.read_bytes("/work/tab.txt") == b"kept"
    assert (await frame.exec("true")).ok
    await frame.destroy()
    with pytest.raises(RuntimeError):
        await frame.resume()  # the provider lost it


async def test_a_native_fakes_suspend_and_resume_are_idempotent() -> None:
    """DBOS replays a step: suspending a suspended frame and resuming a running one are no-ops."""
    frame = NativeFakeFrame("t1", name="machine-n-2")
    await frame.create()
    await frame.resume()  # running already
    await frame.suspend()
    await frame.suspend()  # suspended already: no FrameStopped
    assert FakeFrame.world["machine-n-2"].suspended
    await frame.resume()
    await frame.resume()
    assert not FakeFrame.world["machine-n-2"].suspended
    assert FakeFrame.world["machine-n-2"].calls.count("suspend") == 1


async def test_an_archive_fake_never_sleeps_natively() -> None:
    frame = FakeFrame("t1")
    with pytest.raises(NotImplementedError):
        await frame.suspend()
    with pytest.raises(NotImplementedError):
        await frame.resume()


def test_the_fake_factory_makes_a_native_frame_for_the_native_driver() -> None:
    assert frame_class(NATIVE) is NativeFakeFrame and frame_class("docker") is FakeFrame


def test_the_fake_factory_makes_each_driver_its_own_frame_and_any_other_name_a_dockers() -> None:
    assert frame_class(SANDBAR) is SandbarFakeFrame and SandbarFakeFrame.driver == "sandbar"
    assert frame_class("zz-named") is FakeFrame  # `new_frame` still serves any name, as before


async def test_the_fake_listing_honours_the_drivers_asked_and_refuses_one_no_fake_serves(
    fake_frames: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like `frames.inventory`, the installed fake lists only the drivers it is
    asked (None: this worker's), each frame stamped with its own, and raises for a driver no fake
    frame class serves - so a fake-backed reconciler test can see a stale stamp and a driver
    that does not answer (I1)."""
    import prismoid.ai.frames as frames

    await FakeFrame("t", name="on-docker").create()
    await SandbarFakeFrame("t", name="on-sandbar").create()
    assert [(f.ref, f.driver) for f in await frames.inventory([SANDBAR])] == [("on-sandbar", "sandbar")]
    both = await frames.inventory(["docker", SANDBAR])
    assert sorted((f.ref, f.driver) for f in both) == [("on-docker", "docker"), ("on-sandbar", "sandbar")]
    monkeypatch.setattr(settings.worker, "frame_driver", "docker")
    assert [f.ref for f in await frames.inventory()] == ["on-docker"]
    with pytest.raises(ValueError, match="unknown frame_driver 'zz-named'"):
        await frames.inventory(["zz-named"])


async def test_every_driver_can_let_go_of_a_frame_without_touching_it(fake_frames: Any) -> None:
    """`close`: what a process holds open on a frame ends when
    the step lets go, and the frame runs on. Docker and sandbar hold nothing past a call."""
    import inspect

    from prismoid.ai.frames.docker_driver import DockerFrame
    from prismoid.ai.frames.sandbar_driver import SandbarFrame

    for cls in (DockerFrame, SandbarFrame, FakeFrame):
        assert inspect.iscoroutinefunction(cls.close), cls.__name__
    frame = FakeFrame("t")
    await frame.create()
    await frame.close()
    assert FakeFrame.world[frame.ref].closes == 1 and frame.ref in FakeFrame.world
