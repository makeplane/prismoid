# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""DeviceFrame's life with the helper: services, allow, denials,
the interrupted report and its ack, `new_frame`, and the inventory. The rig is
`tests/support/rigs/device_driver_rig.py`."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import httpx
import pytest

from prismoid.ai.frames import inventory, new_frame
from prismoid.ai.frames.device_driver import DeviceFrame
from prismoid.ai.frames.driver import FrameUnreachable
from prismoid.ai.frames.layout import to_real
from prismoid.conf import settings
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.schemas.device import (
    InterruptedOut,
)
from prismoid.schemas.machines import LABEL_KIND, LABEL_MACHINE
from tests.support.rigs.device_driver_rig import BASE, FRAME, PATHS, Helper, frame_for, made
from tests.support.rigs.device_driver_rig import helper as helper
from tests.support.rigs.device_driver_rig import helper_home as helper_home


async def test_a_deleted_tasks_quarantine_is_forgotten_on_the_helper(helper: Helper) -> None:
    frame = await made(helper)
    helper.on("DELETE", "/frames/device-t1/quarantine", httpx.Response(204))
    await frame.forget_quarantine()
    assert ("DELETE", "/frames/device-t1/quarantine") in [(m, p) for m, p, _ in helper.sent]


async def test_stop_services_asks_the_helper_and_a_gone_frame_is_not_an_error(helper: Helper) -> None:
    frame = await made(helper)
    helper.on("POST", "/frames/device-t1/stop-services", httpx.Response(204))
    await frame.stop_services()
    assert helper.bodies("/frames/device-t1/stop-services") == [{}]
    helper.on(
        "POST", "/frames/device-t1/stop-services", httpx.Response(404, json={"detail": "no such frame"})
    )
    await frame.stop_services()  # nothing is running in a frame that is gone
    helper.on(
        "POST", "/frames/device-t1/stop-services", httpx.Response(422, json={"detail": f"{PATHS.work}: no"})
    )
    with pytest.raises(RuntimeError) as refused:
        await frame.stop_services()
    assert "/Users/me" not in str(refused.value)


async def test_allow_has_the_versioned_semantics(helper: Helper) -> None:
    held = {"v": 1}

    def put(b: dict[str, Any]) -> httpx.Response:
        if b["version"] is not None and b["version"] <= held["v"]:
            return httpx.Response(409, json={"ok": False, "error": "stale_version", "version": held["v"]})
        if b["version"] is not None:
            held["v"] = b["version"]
        return httpx.Response(200, json={"ok": True})

    helper.on("PUT", "/frames/device-t1/egress", put)
    frame = await made(helper)  # born at v1
    assert await frame.allow(["a.example", "b.example"], 7) is True
    assert frame.egress_version == 7 and frame.egress_hosts == ["a.example", "b.example"]
    assert await frame.allow(["a.example"], 6) is False  # stale locally: never sent
    assert len(helper.bodies("/frames/device-t1/egress")) == 1
    held["v"] = 9  # a peer pushed v9
    assert await frame.allow(["z.example"]) is False  # v8: refused, v9 adopted
    assert frame.egress_version == 9 and frame.egress_hosts == ["a.example", "b.example"]
    assert await frame.allow(["z.example"]) is True and frame.egress_version == 10


async def test_unpinned_allow_after_bind_is_unversioned(helper: Helper) -> None:
    helper.on("PUT", "/frames/device-t1/egress", httpx.Response(200, json={"ok": True}))
    frame = DeviceFrame("t1", name="device-t1", transport=httpx.MockTransport(helper))
    await frame.bind()
    assert await frame.allow(["a.example"]) is True
    assert helper.bodies("/frames/device-t1/egress")[0]["version"] is None and frame.egress_version == 0


async def test_denials_and_browser_and_close(helper: Helper) -> None:
    helper.on(
        "GET", "/frames/device-t1/denials", httpx.Response(200, json={"denials": [{"host": "x.example"}]})
    )
    frame = await made(helper)
    assert await frame.denials() == [{"host": "x.example"}]
    assert await frame.browser({"verb": "open"}) == {"ok": False, "error": "no browser in this frame"}
    await frame.close()
    await frame.close()  # never raises, safe twice


async def test_reset_mounts_asks_the_helper(helper: Helper) -> None:
    helper.on("POST", "/frames/device-t1/reset_mounts", httpx.Response(204))
    await (await made(helper)).reset_mounts()
    assert helper.bodies("/frames/device-t1/reset_mounts") == [{}]


async def test_interrupted_returns_the_helpers_report_and_ack(helper: Helper) -> None:
    cut = InterruptedOut(
        exec_id="e1",
        command_head="make",
        started_at=dt.datetime(2026, 10, 29, 10, tzinfo=dt.UTC),
        still_running=True,
    )
    helper.on("GET", "/frames/device-t1/interrupted", httpx.Response(200, json=cut.model_dump(mode="json")))
    helper.on("POST", "/frames/device-t1/interrupted/ack", httpx.Response(204))
    frame = await made(helper)
    got = await frame.interrupted()
    assert got == cut and got is not None and got.still_running is True
    await frame.ack_interrupted()
    assert helper.bodies("/frames/device-t1/interrupted/ack") == [{"exec_id": "e1"}]
    helper.on("GET", "/frames/device-t1/interrupted", httpx.Response(204))
    assert await frame.interrupted() is None


async def test_interrupted_head_carries_only_virtual_roots(helper: Helper) -> None:
    """The head is the command as the device ran it - real roots - and a gate and a prompt record it."""
    cut = InterruptedOut(
        exec_id="e2",
        command_head="echo x >> /Users/me/code/demo/log.txt && cat /Users/me/code/demo",
        started_at=dt.datetime(2026, 10, 29, 10, tzinfo=dt.UTC),
    )
    helper.on("GET", "/frames/device-t1/interrupted", httpx.Response(200, json=cut.model_dump(mode="json")))
    got = await (await made(helper)).interrupted()
    assert got is not None and got.command_head == "echo x >> /work/log.txt && cat /work"


async def test_an_interrupted_head_naming_out_carries_only_virtual_roots(helper: Helper) -> None:
    """Fix round 2: the task's own `out/` is the `/out` root on a device, so a cut-off command that
    names it - the engine stages its recording copy there - comes back virtual, never real."""
    cut = InterruptedOut(
        exec_id="e3",
        command_head=f"ls '{BASE}/out/recording-pub-1-ab12' && cat {BASE}/out/log.txt",
        started_at=dt.datetime(2026, 10, 29, 10, tzinfo=dt.UTC),
    )
    helper.on("GET", "/frames/device-t1/interrupted", httpx.Response(200, json=cut.model_dump(mode="json")))
    got = await (await made(helper)).interrupted()
    assert got is not None and got.command_head == "ls '/out/recording-pub-1-ab12' && cat /out/log.txt"


async def test_a_cut_off_recording_copy_is_told_and_stored_with_virtual_roots_only(helper: Helper) -> None:
    """End to end: the engine's recording copy (`_recording_stage`), as the device ran it (real,
    quoted, a space in the home), cut off by the helper going away - `owed_cut` reads it back and
    what a gate or a prompt records names only virtual roots."""
    from prismoid.operations.execution.device import Cut, cut_note, owed_cut

    frame = await made(helper)
    _, copy, _ = ex_harvest.recording_stage(frame, "recording-pub-1-ab12cd34")
    ran = frame.paths.render(copy)  # what the helper's exec journal holds
    assert BASE in ran
    cut = InterruptedOut(
        exec_id="e4", command_head=ran, started_at=dt.datetime(2026, 10, 29, 10, tzinfo=dt.UTC)
    )
    helper.on("GET", "/frames/device-t1/interrupted", httpx.Response(200, json=cut.model_dump(mode="json")))
    told = await owed_cut(frame, Cut(look=True))
    assert told.head is not None
    for recorded in (told.head, cut_note(told) or ""):
        assert "/Users/me" not in recorded and "Application Support" not in recorded, recorded
    assert "/out/recording-pub-1-ab12cd34" in told.head and "/task/recordings/recording-1" in told.head
    assert to_real("/out/recording-pub-1-ab12cd34", frame.paths) == f"{BASE}/out/recording-pub-1-ab12cd34"
    assert frame.paths.virtual(f"{BASE}/out/x") == "/out/x", "/out round-trips"


async def test_ack_names_the_exec_the_worker_parked_on(helper: Helper) -> None:
    """B6 round 1 (M3): the worker acks the entry it recorded, never whatever the journal says now."""
    helper.on("POST", "/frames/device-t1/interrupted/ack", httpx.Response(204))
    await (await made(helper)).ack_interrupted("e7")
    assert helper.bodies("/frames/device-t1/interrupted/ack") == [{"exec_id": "e7"}]
    assert helper.bodies("/frames/device-t1/interrupted") == [], "no fresh read"


async def test_ack_without_a_report_is_a_no_op(helper: Helper) -> None:
    helper.on("GET", "/frames/device-t1/interrupted", httpx.Response(204))
    await (await made(helper)).ack_interrupted()
    assert helper.bodies("/frames/device-t1/interrupted/ack") == []


def test_new_frame_device_arm(monkeypatch: pytest.MonkeyPatch) -> None:
    frame = new_frame("t1", driver="device", name="device-t1")
    assert isinstance(frame, DeviceFrame) and frame.ref == "device-t1" and frame.driver == "device"
    monkeypatch.setattr(settings.worker, "frame_driver", "device")
    assert isinstance(new_frame("t1"), DeviceFrame)
    assert new_frame("t1", driver="device", recording="3").recording_dir == "/task/recordings/recording-3"


async def test_create_without_labels_is_a_throwaway_seat(helper: Helper) -> None:
    frame = frame_for(helper)
    await frame.create()
    assert helper.bodies("/frames")[0]["kind"] == "seat"


async def test_create_passes_the_label_kind_and_no_grants_yet(helper: Helper) -> None:
    frame = frame_for(helper)
    await frame.create(labels={LABEL_KIND: "verify", LABEL_MACHINE: "m"}, egress_version=4)
    spec = helper.bodies("/frames")[0]
    assert spec["kind"] == "verify" and spec["egress_version"] == 4 and spec["grants"] == []
    assert spec["labels"][LABEL_MACHINE] == "m"


async def test_inventory_lists_frames_with_labels(helper: Helper) -> None:
    listed = FRAME.model_copy(
        update={
            "labels": {LABEL_KIND: "machine", LABEL_MACHINE: "m1"},
            "created_at": dt.datetime(2026, 10, 29, 10, tzinfo=dt.UTC),
        }
    )
    helper.on("GET", "/frames", httpx.Response(200, json=[listed.model_dump(mode="json")]))
    from prismoid.ai.frames import device_driver

    found = await device_driver.inventory(transport=httpx.MockTransport(helper))
    assert [(f.ref, f.driver, dict(f.labels)) for f in found] == [
        ("device-t1", "device", {LABEL_KIND: "machine", LABEL_MACHINE: "m1"})
    ]
    assert found[0].created_at.year == 2026


async def test_inventory_device_raises_when_helper_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reconciler treats a driver that cannot be listed as silent: it must raise, not return []."""
    monkeypatch.setattr(settings.local, "home", Path("/nonexistent-prismoid-home"))
    with pytest.raises(FrameUnreachable):
        await inventory(["device"])


async def test_inventory_names_the_device_driver_in_errors() -> None:
    with pytest.raises(ValueError, match="device"):
        await inventory(["nope"])
