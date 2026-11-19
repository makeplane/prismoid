# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A publish on the device when the door says no, or says nothing. A helper that went away is the
step's to wait out, never a smaller deliverable or a file that "does not exist", and a file past the
door's cap is a refusal the model can act on, never a dead run. The helper is faked at the HTTP
edge (`tests/support/rigs/device_driver_rig.py`)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic_ai import ModelRetry

from prismoid.ai.frames.driver import FrameUnreachable
from prismoid.ai.tools.browser import BrowserPorts, _save  # pyright: ignore[reportPrivateUsage]
from prismoid.ai.tools.frame_tools import frame_toolset
from prismoid.errors import PolicyDenied
from tests.support.rigs.device_driver_rig import PATHS, Helper, b64, made
from tests.support.rigs.device_driver_rig import helper as helper
from tests.support.rigs.device_driver_rig import helper_home as helper_home

LIST, READ, WRITE = (f"/frames/device-t1/files/{verb}" for verb in ("list", "read", "write"))
OVER = httpx.Response(413, json={"detail": f"{PATHS.work}/big.md: over 67108864 bytes"})


def _down(_body: dict[str, Any]) -> httpx.Response:
    raise httpx.ConnectError("the socket is gone")


def _tree(listing: dict[str, list[str]], files: dict[str, bytes]) -> tuple[Any, Any]:
    """The door's answers for a folder of `listing` (under the work folder) holding `files`."""

    def listed(body: dict[str, Any]) -> httpx.Response:
        names = listing.get(body["path"].removeprefix(f"{PATHS.work}/"))
        return httpx.Response(404, json={"detail": "no"}) if names is None else _names(names)

    def read(body: dict[str, Any]) -> httpx.Response:
        name = body["path"].removeprefix(f"{PATHS.work}/")
        if name in files:
            return httpx.Response(200, json={"data_b64": b64(files[name])})
        return httpx.Response(404, json={"detail": "no such file"})

    return listed, read


def _names(names: list[str]) -> httpx.Response:
    return httpx.Response(200, json={"names": names})


async def test_a_helper_gone_at_the_listing_is_not_a_missing_file(helper: Helper) -> None:
    helper.on("POST", LIST, _down)
    frame = await made(helper)
    with pytest.raises(FrameUnreachable):
        await frame.publish("/work/a.md", "document", "A")


async def test_a_helper_gone_at_the_read_is_not_a_missing_file(helper: Helper) -> None:
    helper.on("POST", LIST, httpx.Response(404, json={"detail": "no"}))
    helper.on("POST", READ, _down)
    frame = await made(helper)
    with pytest.raises(FrameUnreachable):
        await frame.publish("/work/a.md", "document", "A")


async def test_a_helper_gone_inside_a_tree_never_ships_it_short(helper: Helper) -> None:
    """Down the tree, a silent listing would read as an empty folder and publish without it."""
    listed, read = _tree({"site": ["keep.md", "sub/"]}, {"site/keep.md": b"kept"})

    def flaky(body: dict[str, Any]) -> httpx.Response:
        return _down(body) if body["path"].endswith("/sub") else listed(body)

    helper.on("POST", LIST, flaky)
    helper.on("POST", READ, read)
    frame = await made(helper)
    with pytest.raises(FrameUnreachable):
        await frame.publish("/work/site", "code", "S")
    assert helper.bodies(WRITE) == [], "nothing landed"


async def test_a_folder_the_door_will_not_list_is_still_a_file(helper: Helper) -> None:
    _listed, read = _tree({}, {"a.md": b"# a\n"})
    helper.on("POST", LIST, httpx.Response(422, json={"detail": "not a directory"}))
    helper.on("POST", READ, read)
    helper.on("POST", WRITE, httpx.Response(204))
    await (await made(helper)).publish("/work/a.md", "document", "A")
    assert helper.bodies(WRITE)[0]["path"] == f"{PATHS.artifacts}/a.md"


async def test_a_source_over_the_cap_is_named_too_large(helper: Helper) -> None:
    helper.on("POST", LIST, httpx.Response(404, json={"detail": "no"}))
    helper.on("POST", READ, OVER)
    frame = await made(helper)
    with pytest.raises(PolicyDenied) as refused:
        await frame.publish("/work/big.md", "document", "B")
    said = str(refused.value)
    assert "no such file" not in said and "/work/big.md" in said and "limit" in said
    assert "/Users" not in said


async def test_a_file_over_the_cap_inside_a_tree_is_named_too_large(helper: Helper) -> None:
    listed, _read = _tree({"site": ["big.bin"]}, {})
    helper.on("POST", LIST, listed)
    helper.on("POST", READ, OVER)
    frame = await made(helper)
    with pytest.raises(PolicyDenied, match="limit"):
        await frame.publish("/work/site", "code", "S")


async def test_a_deliverable_over_the_cap_is_a_refusal(helper: Helper) -> None:
    """Every file of a tree fits and the tar of them does not: the write is what refuses."""
    listed, read = _tree({"site": ["a.bin"]}, {"site/a.bin": b"x"})
    helper.on("POST", LIST, listed)
    helper.on("POST", READ, read)
    helper.on(
        "POST", WRITE, httpx.Response(413, json={"detail": f"{PATHS.artifacts}/site: over 67108864 bytes"})
    )
    frame = await made(helper)
    with pytest.raises(PolicyDenied) as refused:
        await frame.publish("/work/site", "code", "S")
    assert "/artifacts/site" in str(refused.value) and "limit" in str(refused.value)
    assert len(helper.bodies(WRITE)) == 1, "no manifest entry for what never landed"


async def test_the_door_refusing_the_target_is_a_refusal(helper: Helper) -> None:
    helper.on("POST", LIST, httpx.Response(404, json={"detail": "no"}))
    helper.on("POST", READ, httpx.Response(200, json={"data_b64": b64(b"x")}))
    helper.on("POST", WRITE, httpx.Response(403, json={"detail": "that name is protected"}))
    with pytest.raises(PolicyDenied, match="protected"):
        await (await made(helper)).publish("/work/a.md", "document", "A")


async def test_a_helper_gone_at_the_write_stays_gone(helper: Helper) -> None:
    helper.on("POST", LIST, httpx.Response(404, json={"detail": "no"}))
    helper.on("POST", READ, httpx.Response(200, json={"data_b64": b64(b"x")}))
    helper.on("POST", WRITE, _down)
    with pytest.raises(FrameUnreachable):
        await (await made(helper)).publish("/work/a.md", "document", "A")


async def test_the_model_reads_the_cap_and_the_run_goes_on(helper: Helper) -> None:
    helper.on("POST", LIST, httpx.Response(404, json={"detail": "no"}))
    helper.on("POST", READ, OVER)
    frame = await made(helper)
    publish: Any = frame_toolset(("frame.publish",), frame).tools["publish"].function

    class _Ctx:
        tool_call_id = "c1"

    with pytest.raises(ModelRetry, match="limit"):
        await publish(_Ctx(), "/work/big.md", "document", "B")


async def test_a_screenshot_with_the_helper_gone_is_not_a_refusal() -> None:
    """The model would read it as a bad path and go on; the step has to wait for its device."""

    async def gone(_path: str, _png: bytes) -> str:
        raise FrameUnreachable("the device helper did not answer")

    async def browse(_command: dict[str, Any]) -> dict[str, Any]:
        return {}

    async def allow(_domains: list[str]) -> bool:
        return True

    ports = BrowserPorts(browse=browse, allow=allow, save=gone)
    with pytest.raises(FrameUnreachable):
        await _save(None, ports, "/out/a.png", b"x")
