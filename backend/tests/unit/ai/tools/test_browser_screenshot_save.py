# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""`browser_screenshot(save_as=...)`: the agent files a picture of the running app in
its deliverable. The frame's own door says where it may land - the work folder or `out/`, never a
read-only grant - and only a frame on the device can save (elsewhere the same tool says it
cannot: `test_browser_batch.py`)."""

from __future__ import annotations

import base64
import os
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic_ai import BinaryContent

from prismoid.ai.capabilities.taint import tainted
from prismoid.ai.frames.device_driver import DeviceFrame
from prismoid.ai.rules.approvals import Approvals
from prismoid.ai.tools.browser import BROWSER_TOOLS, BrowserPorts, browser_toolset
from prismoid.conf import settings
from prismoid.device import files
from prismoid.device.frames import DeviceFrameState, FrameRegistry
from prismoid.schemas.device import FileIn, FolderGrant, FrameOut, FrameSpecIn
from prismoid.schemas.machines import FrameKind

PNG = b"\x89PNG\r\n\x1a\nnot really"


class _Ctx:
    tool_call_id = "c1"
    messages: tuple[Any, ...] = ()


class _Journal:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def __call__(
        self,
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        self.rows.append({"tool": tool, "ok": ok, "summary": summary, "denied_reason": denied_reason})


def _files(root: Path) -> set[Path]:
    return {p for p in root.rglob("*") if p.is_file()}


async def _browse(_cmd: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "png_b64": base64.b64encode(PNG).decode()}


async def _allow(_domains: list[str]) -> bool:
    return True


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> DeviceFrameState:
    monkeypatch.setattr(settings.local, "home", tmp_path)
    (tmp_path / "helper.key").write_text("k3y")
    for name in ("proj", "shelf", "other"):
        (tmp_path / name).mkdir()
    spec = FrameSpecIn(
        task_id="t1",
        kind=FrameKind.MACHINE,
        grants=[
            FolderGrant(path=str(tmp_path / "proj"), mode="rw", name="/work"),
            FolderGrant(path=str(tmp_path / "shelf"), mode="ro", name="/grants/shelf"),
            FolderGrant(path=str(tmp_path / "other"), mode="rw", name="/grants/other"),
        ],
    )
    return FrameRegistry(tmp_path / "home").create(spec)


def _frame(state: DeviceFrameState) -> DeviceFrame:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/frames":
            out = FrameOut(ref=state.ref, device_name="Mac", paths=state.paths)
            return httpx.Response(200, json=out.model_dump(mode="json"))
        assert request.url.path.endswith("/files/write")
        try:
            files.write(state, FileIn.model_validate_json(request.content))
        except PermissionError as refused:
            return httpx.Response(403, json={"detail": str(refused)})
        return httpx.Response(204)

    return DeviceFrame("t1", transport=httpx.MockTransport(handle))


async def _tool(frame: DeviceFrame, journal: _Journal | None = None) -> Any:
    await frame.create(allow_domains=[], labels={})
    ports = BrowserPorts(browse=_browse, allow=_allow, save=frame.save_output)
    toolset = browser_toolset(BROWSER_TOOLS, journal, ports, Approvals(tainted=tainted))
    assert toolset is not None
    return toolset.tools["browser_screenshot"].function


async def test_screenshot_save_writes_png_in_out(state: DeviceFrameState) -> None:
    shot = await _tool(_frame(state))
    answer = await shot(_Ctx(), save_as="/out/change/screenshots/home.png")
    real = f"{state.paths.out}/change/screenshots/home.png"
    assert answer == real, "the path back is one the agent's own commands reach"
    with open(real, "rb") as saved:  # noqa: ASYNC230
        assert saved.read() == PNG


async def test_a_save_may_land_in_the_work_folder_in_either_spelling(state: DeviceFrameState) -> None:
    shot = await _tool(_frame(state))
    assert await shot(_Ctx(), save_as="/work/shots/a.png") == f"{state.paths.work}/shots/a.png"
    assert await shot(_Ctx(), save_as=f"{state.paths.work}/shots/b.png") == f"{state.paths.work}/shots/b.png"
    assert sorted(os.listdir(Path(state.paths.work, "shots"))) == ["a.png", "b.png"]


async def test_no_save_as_is_the_picture_the_model_always_got(state: DeviceFrameState) -> None:
    shot = await _tool(_frame(state))
    answer = await shot(_Ctx())
    assert isinstance(answer, BinaryContent) and answer.data == PNG


@pytest.mark.parametrize(
    "target",
    [
        "/task/a.png",  # a root of the frame, the system's
        "/artifacts/a.png",  # the loading dock: only publish
        "/out/../task/a.png",
        "/work/../../../../etc/a.png",
        "/grants/shelf/a.png",  # read-only
        "/grants/other/a.png",  # rw, and still neither the work folder nor out/
        "/etc/a.png",
        "a.png",
        "/out/a.txt",  # the picture is a PNG, whatever the agent would call it
    ],
)
async def test_screenshot_save_refused_outside(state: DeviceFrameState, target: str) -> None:
    base = Path(state.paths.work).parent
    before = _files(base)
    shot = await _tool(_frame(state))
    answer = await shot(_Ctx(), save_as=target)
    assert isinstance(answer, str) and answer.startswith("refused:")
    assert _files(base) == before
    assert str(base) not in answer


@pytest.mark.parametrize(("link", "aim"), [("ro", "shelf"), ("rw", "other"), ("out", "elsewhere")])
async def test_a_link_cannot_carry_a_save_out(state: DeviceFrameState, link: str, aim: str) -> None:
    """A folder link in the work folder, into the read-only grant, another rw grant, or nowhere of
    the frame's: the door judges where the path really goes."""
    base = Path(state.paths.work).parent
    os.makedirs(base / "elsewhere", exist_ok=True)
    os.symlink(base / aim, f"{state.paths.work}/{link}")
    shot = await _tool(_frame(state))
    answer = await shot(_Ctx(), save_as=f"/work/{link}/a.png")
    assert answer.startswith("refused:")
    assert not os.path.lexists(base / aim / "a.png")  # noqa: ASYNC240


async def test_a_read_only_grant_nested_in_the_work_folder_stays_read_only(tmp_path: Path) -> None:
    work = tmp_path / "proj"
    (work / "vendor").mkdir(parents=True)
    spec = FrameSpecIn(
        task_id="t1",
        kind=FrameKind.MACHINE,
        grants=[
            FolderGrant(path=str(work), mode="rw", name="/work"),
            FolderGrant(path=str(work / "vendor"), mode="ro", name="/grants/vendor"),
        ],
    )
    state = FrameRegistry(tmp_path / "home").create(spec)
    req = FileIn(path=str(work / "vendor" / "a.png"), data_b64=base64.b64encode(PNG).decode(), confined=True)
    with pytest.raises(PermissionError):
        files.write(state, req)


async def test_the_refusal_is_journaled_and_the_screenshot_stays_one_call(state: DeviceFrameState) -> None:
    journal = _Journal()
    shot = await _tool(_frame(state), journal)
    await shot(_Ctx(), save_as="/grants/shelf/a.png")
    assert [(r["tool"], r["ok"]) for r in journal.rows] == [
        ("browser.screenshot", True),
        ("browser.save", False),
    ]
    assert not any(str(state.paths.work) in str(r) for r in journal.rows)


async def test_a_path_of_the_users_disk_never_reaches_the_journal(state: DeviceFrameState) -> None:
    journal = _Journal()
    shot = await _tool(_frame(state), journal)
    answer = await shot(_Ctx(), save_as="/Users/me/.ssh/a.png")
    assert answer.startswith("refused:") and ".ssh" not in answer
    assert ".ssh" not in str(journal.rows)


async def test_a_device_toolset_does(state: DeviceFrameState) -> None:
    frame = _frame(state)
    await frame.create(allow_domains=[], labels={})
    toolset = browser_toolset(
        BROWSER_TOOLS,
        None,
        BrowserPorts(browse=_browse, allow=_allow, save=frame.save_output),
        Approvals(tainted=tainted),
    )
    assert toolset is not None
    assert "save_as" in toolset.tools["browser_screenshot"].tool_def.parameters_json_schema["properties"]
