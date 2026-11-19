# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The paper frame: a step's files in memory, the one publish door, and honest refusals for what it
does not have. No I/O."""

from __future__ import annotations

from typing import Any

import pytest

from prismoid.ai.frames import FrameDriver, PaperFull, new_paper
from prismoid.errors import PolicyDenied
from prismoid.schemas.artifacts import Manifest
from prismoid.schemas.outputs import ArtifactType


def _paper(max_bytes: int = 1024) -> FrameDriver:
    return new_paper("t-s-1", max_bytes=max_bytes)


async def test_publish_is_the_one_door_and_the_manifest_harvest_reads() -> None:
    frame = _paper()
    await frame.write_bytes("/work/memo.md", b"# Memo\n\nbody")
    assert Manifest.model_validate(await frame.manifest()).entries == [], "a written file is not an artifact"
    await frame.publish("/work/memo.md", "document", "Memo", ["/task/inputs/brief.md"])
    entry = Manifest.model_validate(await frame.manifest()).entries[0]
    assert entry.path == "/artifacts/memo.md" and entry.type is ArtifactType.DOCUMENT
    assert entry.bytes == 12 and entry.sources == ["/task/inputs/brief.md"]
    assert await frame.read_bytes(entry.path) == b"# Memo\n\nbody", "harvest reads the published bytes"


async def test_the_door_checks_type_and_presence_like_every_driver() -> None:
    frame = _paper()
    await frame.write_bytes("/work/memo.md", b"x")
    with pytest.raises(PolicyDenied, match="unknown artifact type"):
        await frame.publish("/work/memo.md", "poster", "Memo")
    with pytest.raises(PolicyDenied, match="not a valid spreadsheet"):
        await frame.publish("/work/memo.md", "spreadsheet", "Memo")
    with pytest.raises(PolicyDenied, match="no such file"):
        await frame.publish("/work/gone.md", "document", "Gone")


async def test_what_was_given_stays_given() -> None:
    frame = _paper()
    await frame.write_bytes("/task/inputs/brief.md", b"source", mode=0o444, owner="system")
    with pytest.raises(PermissionError):
        await frame.write_file("/task/inputs/brief.md", "laundered")
    assert await frame.read_file("/task/inputs/brief.md") == "source"
    assert await frame.list_dir("/task") == ["inputs/"]
    assert await frame.list_dir("/task/inputs") == ["brief.md"]
    assert await frame.list_dir("/nowhere") == []
    await frame.reset_mounts()
    with pytest.raises(FileNotFoundError):
        await frame.read_file("/task/inputs/brief.md")


async def test_a_step_that_outgrows_paper_fails_as_infra() -> None:
    frame = _paper(max_bytes=10)
    await frame.write_bytes("/task/inputs/a", b"12345", owner="system")
    with pytest.raises(PaperFull) as full:
        await frame.write_bytes("/task/inputs/b", b"123456", owner="system")
    assert full.value.context.get("infra") is True


async def test_no_shell_no_browser_no_network_answered_not_raised() -> None:
    frame = _paper()
    ran = await frame.exec("ls /work")
    assert ran.exit_code == 126 and "without a computer" in ran.stderr
    assert (await frame.browser({"verb": "open"}))["ok"] is False
    assert await frame.denials() == []
    assert await frame.allow(["example.com"]) is True
    assert await frame.bind() is False


class _Ctx:
    tool_call_id = "toolu_01"


async def test_a_researcher_writes_and_publishes_its_findings_on_paper() -> None:
    """The researcher's own tools on a paper frame (a 200-word memo must not start a Machine only so
    the researcher can write `/work/findings.md`): `write_file` resolves its target without a shell -
    paper holds no links to follow - and `publish` is the same door. A path outside `/work` and
    `/artifacts` is still refused."""
    from prismoid.ai.tools.frame_tools import frame_toolset

    frame = _paper()
    tools: Any = frame_toolset(
        ("frame.write_file", "frame.publish"), frame, None, ("/task/inputs/brief.md",)
    ).tools
    said = await tools["write_file"].function(_Ctx(), "/work/findings.md", "# Findings\n\n- one")
    assert said.startswith("wrote /work/findings.md"), said
    await tools["publish"].function(_Ctx(), "/work/findings.md", "document", "Findings: memo")
    entry = Manifest.model_validate(await frame.manifest()).entries[0]
    assert entry.path == "/artifacts/findings.md" and entry.sources == ["/task/inputs/brief.md"]
    refused = await tools["write_file"].function(_Ctx(), "/task/plan.md", "laundered")
    assert refused.startswith("Refused:"), refused
