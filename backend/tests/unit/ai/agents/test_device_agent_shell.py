# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What the model's `shell` sends to the device, and what a publish tells it back. The model is told
real paths, so its command is its own text: only the system's commands speak the virtual roots.
The helper is faked at the HTTP edge (`tests/support/rigs/device_driver_rig.py`)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic_ai import ModelRetry

from prismoid.ai.agents import all as _all
from prismoid.ai.agents.base import AgentName, instructions_for
from prismoid.ai.frames.layout import FramePaths
from prismoid.ai.tools.frame_tools import frame_toolset
from tests.support.fakes.fake_frames import FakeFrame
from tests.support.rigs.device_driver_rig import BASE, FRAME, PATHS, Helper, made
from tests.support.rigs.device_driver_rig import helper as helper
from tests.support.rigs.device_driver_rig import helper_home as helper_home

EXEC = "/frames/device-t1/exec"
ALIAS = "/Users/me/Library/Caches/Prismoid/roots/device-t1/abc"
LINKED = FRAME.model_copy(update={"aliases": {f"{BASE}/task": ALIAS}})
RAN = httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "truncated": False})


class _Ctx:
    tool_call_id = "toolu_01"


def _tool(frame: Any, name: str) -> Any:
    granted = ("frame.shell", "frame.publish", "frame.write_file", "frame.read_file")
    return frame_toolset(granted, frame).tools[name].function


@pytest.mark.parametrize(
    "command",
    [
        "sed -i '' 's#/verify#/v2/verify#g' app.py",
        "cat > routes.py <<'PY'\n@app.get(\"/task/{id}\")\n@app.get('/work')\nPY",
        'grep -rn "/artifacts/" src',
        "echo /out /skills /browser /task/inputs",
        "git log --format=%H -- /work",
    ],
)
async def test_the_models_text_reaches_the_command_as_typed(helper: Helper, command: str) -> None:
    helper.on("POST", EXEC, RAN)
    await _tool(await made(helper), "shell")(_Ctx(), command)
    assert helper.bodies(EXEC)[0]["command"] == command


async def test_a_real_root_that_needs_quoting_still_goes_by_its_alias(helper: Helper) -> None:
    """The one substitution the model's command keeps: without it a path with a space, typed bare as
    the model was told it, is two words."""
    helper.on("POST", "/frames", httpx.Response(200, json=LINKED.model_dump(mode="json")))
    helper.on("POST", EXEC, RAN)
    await _tool(await made(helper), "shell")(_Ctx(), f"cat {BASE}/task/plan.md # about /task")
    assert helper.bodies(EXEC)[0]["command"] == f"cat {ALIAS}/plan.md # about /task"


async def test_the_systems_commands_keep_the_virtual_roots(helper: Helper) -> None:
    helper.on("POST", EXEC, RAN)
    await (await made(helper)).exec("find /work -newer /task/.step-start")
    assert helper.bodies(EXEC)[0]["command"] == f"find {PATHS.work} -newer {BASE}/task/.step-start"


async def test_a_cloud_frame_has_no_second_door() -> None:
    frame = FakeFrame("t1")
    await _tool(frame, "shell")(_Ctx(), "echo /work")
    assert frame.execs == ["echo /work"]
    assert not hasattr(frame, "agent_exec")


async def test_a_publish_refusal_is_read_in_the_paths_the_model_can_use(helper: Helper) -> None:
    """The refusal says where to write. On the device that has to be a path the model's shell can
    reach, and a virtual root no longer is one."""
    helper.on("POST", "/frames/device-t1/files/list", httpx.Response(200, json={"names": ["a.md"]}))
    frame = await made(helper)
    with pytest.raises(ModelRetry) as told:
        await _tool(frame, "publish")(_Ctx(), "/work/site", "change", "C")
    assert f"under {BASE}/out" in str(told.value) and "under /out" not in str(told.value)


def test_each_other_granted_folder_is_named_by_its_real_path() -> None:
    """The model reaches a folder only through its real path, so the law names each one."""
    paths = FramePaths(
        work="/w", task="/t", out="/o", on_host=True, grants=(("/grants/notes", "/Users/me/notes"),)
    )
    text = instructions_for(_all.REGISTRY[AgentName("maker")], paths)
    assert "/Users/me/notes" in text and "/grants" not in text


def test_a_frame_with_no_other_folder_names_none() -> None:
    paths = FramePaths(work="/w", task="/t", out="/o", on_host=True)
    assert "/grants" not in instructions_for(_all.REGISTRY[AgentName("maker")], paths)
