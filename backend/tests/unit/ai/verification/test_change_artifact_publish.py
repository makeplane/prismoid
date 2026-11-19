# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Publishing a `change`: the tree is tarred from the out folder and
nothing runs, a link is refused naming it, and the user's folder is never published as code. The rig is
`tests/support/rigs/change_rig.py`."""

from __future__ import annotations

import base64
import io
import json
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic_ai import ModelRetry

from prismoid.ai.frames import device_driver
from prismoid.ai.frames.device_driver import DeviceFrame
from prismoid.ai.tools.frame_tools import frame_toolset
from prismoid.ai.verification import change
from prismoid.conf import settings
from prismoid.device import files
from prismoid.device.frames import DeviceFrameState, FrameRegistry
from prismoid.errors import PolicyDenied
from prismoid.schemas.device import FileIn, FolderGrant, FrameOut, FrameSpecIn, PathIn
from prismoid.schemas.machines import FrameKind
from tests.support.rigs.change_rig import REPORT, TESTS, bundle, png


def _door(state: DeviceFrameState, spent: list[str]) -> httpx.MockTransport:
    """The worker's HTTP edge, wired to the real file door. Every route is recorded, so a publish
    that ran a process would show."""

    def handle(request: httpx.Request) -> httpx.Response:
        spent.append(request.url.path)
        body = json.loads(request.content) if request.content else {}
        out = FrameOut(ref=state.ref, device_name="MacBook Pro", paths=state.paths, grants=state.spec.grants)
        try:
            if request.url.path == "/frames":
                return httpx.Response(200, json=out.model_dump(mode="json"))
            if request.url.path.endswith("/files/read"):
                req = PathIn(**body)
                data = files.read(state, req.path, nofollow=req.nofollow)
                return httpx.Response(200, json={"data_b64": base64.b64encode(data).decode()})
            if request.url.path.endswith("/files/list"):
                return httpx.Response(200, json={"names": files.list_dir(state, PathIn(**body).path)})
            if request.url.path.endswith("/files/write"):
                files.write(state, FileIn(**body))
                return httpx.Response(204)
        except PermissionError as refused:
            return httpx.Response(403, json={"detail": str(refused)})
        except FileNotFoundError as absent:
            return httpx.Response(404, json={"detail": str(absent)})
        return httpx.Response(500, json={"detail": f"unexpected {request.url.path}"})

    return httpx.MockTransport(handle)


@pytest.fixture
def door(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[DeviceFrameState, list[str]]:
    monkeypatch.setattr(settings.local, "home", tmp_path)
    (tmp_path / "helper.key").write_text("k3y")
    work = tmp_path / "proj"
    work.mkdir()
    spec = FrameSpecIn(
        task_id="t1",
        kind=FrameKind.MACHINE,
        grants=[FolderGrant(path=str(work), mode="rw", name="/work")],
    )
    return FrameRegistry(tmp_path / "home").create(spec), []


async def test_publish_from_out_tars_the_tree_and_runs_nothing(
    door: tuple[DeviceFrameState, list[str]],
) -> None:
    """The bytes cross by the system file door: `out/` is the agent's, so no process runs with it
    as a working directory, and there is no runner or interpreter to plant anything for (B3)."""
    state, spent = door
    frame = DeviceFrame("t1", transport=_door(state, spent))
    await frame.create(allow_domains=[], labels={})
    await frame.write_bytes("/out/change/report.md", REPORT.encode())
    await frame.write_bytes("/out/change/test-output.txt", TESTS.encode())
    await frame.write_bytes("/out/change/screenshots/home.png", png())

    await frame.publish("/out/change", "change", "Home page greets")

    manifest = await frame.manifest()
    assert [(e["path"], e["type"]) for e in manifest["entries"]] == [("/artifacts/change", "change")]
    assert not any(str(state.paths.out) in json.dumps(e) for e in manifest["entries"])
    published = await frame.read_bytes("/artifacts/change")
    read = change.read_change(published)
    assert read is not None and read.report is not None and "greets" in read.report.text
    assert [s.name for s in read.screenshots] == ["screenshots/home.png"]
    assert not any(path.endswith("/exec") for path in spent)


def _stored(state: DeviceFrameState) -> bytes:
    """Everything the store could have been handed: the frame's loading dock, byte for byte."""
    return b"".join(p.read_bytes() for p in Path(state.paths.artifacts).rglob("*") if p.is_file())


@pytest.mark.parametrize("source", ["/work", "/work/app", "/out/lnk", "/grants/other"])
async def test_a_change_is_published_only_from_the_out_folder(
    door: tuple[DeviceFrameState, list[str]], tmp_path: Path, source: str
) -> None:
    """On the type the law and every refusal name: the user's folder never reaches the store,
    `.env` and all, by any of the three doors."""
    state, spent = door
    state.paths = state.paths.model_copy(update={"grants": {"/grants/other": str(tmp_path / "proj")}})
    frame = DeviceFrame("t1", transport=_door(state, spent))
    await frame.create(allow_domains=[], labels={})
    await frame.write_bytes("/work/.env", b"SECRET=hunter2")
    await frame.write_bytes("/work/app/main.py", b"print(1)")
    os.symlink(state.paths.work, f"{state.paths.out}/lnk")
    with pytest.raises(PolicyDenied) as refused:
        await frame.publish(source, "change", "T")
    assert "out folder" in str(refused.value) and str(tmp_path) not in str(refused.value)
    assert (await frame.manifest())["entries"] == []
    assert b"hunter2" not in _stored(state)


@pytest.mark.parametrize("link", ["creds", "dir"])
async def test_a_change_holding_a_link_is_refused_naming_it(
    door: tuple[DeviceFrameState, list[str]], link: str
) -> None:
    """A file link to the repo's `.env`, and a folder link to the repo: the door could read both,
    so the tar must refuse them itself, and say which."""
    state, spent = door
    frame = DeviceFrame("t1", transport=_door(state, spent))
    await frame.create(allow_domains=[], labels={})
    await frame.write_bytes("/work/.env", b"SECRET=hunter2")
    await frame.write_bytes("/out/change/report.md", REPORT.encode())
    aim = f"{state.paths.work}/.env" if link == "creds" else state.paths.work
    os.symlink(aim, f"{state.paths.out}/change/{link}")
    with pytest.raises(PolicyDenied) as refused:
        await frame.publish("/out/change", "change", "T")
    assert f"/out/change/{link}" in str(refused.value)
    assert (await frame.manifest())["entries"] == []
    assert b"hunter2" not in _stored(state)


@pytest.mark.parametrize("kind", ["code", "webapp"])
async def test_a_fresh_project_publishes_past_its_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """`node_modules/.bin` is what a fresh project holds: its links are skipped, neither followed
    nor stored, and counted in the log, and the project still publishes."""
    monkeypatch.setattr(settings.local, "home", tmp_path)
    (tmp_path / "helper.key").write_text("k3y")
    logged: list[int] = []
    monkeypatch.setattr(
        device_driver.logfire,
        "info",
        lambda _msg, **kw: logged.append(kw["links"]) if "links" in kw else None,
    )
    state = FrameRegistry(tmp_path / "home").create(FrameSpecIn(task_id="t3", kind=FrameKind.MACHINE))
    (tmp_path / "secret").write_text("SECRET=hunter2")
    frame = DeviceFrame("t3", transport=_door(state, []))
    await frame.create(allow_domains=[], labels={})
    await frame.write_bytes("/work/site/test.sh", b"exit 0")
    await frame.write_bytes("/work/site/node_modules/pkg/cli.js", b"run()")
    bin_dir = f"{state.paths.work}/site/node_modules/.bin"
    os.makedirs(bin_dir)
    os.symlink("../pkg/cli.js", f"{bin_dir}/tool")  # inside the project
    os.symlink(tmp_path / "secret", f"{bin_dir}/outward")  # out of every root
    os.symlink("/no/such/place", f"{bin_dir}/dangling")
    os.symlink(f"{state.paths.work}/site/node_modules/pkg", f"{bin_dir}/folder")

    await frame.publish("/work/site", kind, "T")

    assert [e["type"] for e in (await frame.manifest())["entries"]] == [kind]
    with tarfile.open(fileobj=io.BytesIO(await frame.read_bytes("/artifacts/site"))) as tar:
        names = tar.getnames()
        assert all(not m.issym() and not m.islnk() for m in tar.getmembers())
    assert "site/node_modules/pkg/cli.js" in names and "site/test.sh" in names
    assert not [n for n in names if n.startswith("site/node_modules/.bin/")]
    assert b"hunter2" not in _stored(state)
    assert logged == [4]


async def test_a_project_type_still_refuses_what_is_no_link(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a link is skipped: a file the door refuses for another reason fails the publish."""
    monkeypatch.setattr(settings.local, "home", tmp_path)
    (tmp_path / "helper.key").write_text("k3y")
    state = FrameRegistry(tmp_path / "home").create(FrameSpecIn(task_id="t3", kind=FrameKind.MACHINE))
    real_read = files.read

    def refuse(frame: DeviceFrameState, path: str, *, nofollow: bool = False) -> bytes:
        if path.endswith("/key"):
            raise PermissionError(f"{path} is protected")
        return real_read(frame, path, nofollow=nofollow)

    monkeypatch.setattr(files, "read", refuse)
    frame = DeviceFrame("t3", transport=_door(state, []))
    await frame.create(allow_domains=[], labels={})
    await frame.write_bytes("/work/site/key", b"k")
    with pytest.raises(PolicyDenied, match="protected"):
        await frame.publish("/work/site", "code", "T")


@pytest.mark.parametrize("path", ["site", "/etc/thing", "/Users/me/.ssh/key"])
@pytest.mark.parametrize("kind", ["code", "webapp", "change"])
async def test_a_path_in_no_folder_is_the_agents_to_fix(
    door: tuple[DeviceFrameState, list[str]], kind: str, path: str
) -> None:
    """It reaches the retry path, and a path of the user's disk reaches neither it nor the journal."""
    state, spent = door
    frame = DeviceFrame("t1", transport=_door(state, spent))
    await frame.create(allow_domains=[], labels={})
    rows: list[str] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        rows.append(f"{tool} {summary} {denied_reason}")

    tool = frame_toolset(("frame.publish",), frame, journal, ()).tools["publish"].function
    with pytest.raises(ModelRetry) as retried:
        await tool(SimpleNamespace(tool_call_id="c1"), path, kind, "T")  # pyright: ignore[reportArgumentType]
    assert ".ssh" not in str(retried.value) and ".ssh" not in str(rows)


async def test_a_type_error_on_a_path_in_no_folder_keeps_it_out_too(
    door: tuple[DeviceFrameState, list[str]],
) -> None:
    state, spent = door
    frame = DeviceFrame("t1", transport=_door(state, spent))
    await frame.create(allow_domains=[], labels={})
    with pytest.raises(PolicyDenied) as refused:
        await frame.publish("/Users/me/.ssh/key.txt", "spreadsheet", "T")
    assert ".ssh" not in str(refused.value)


async def test_a_change_is_a_directory_never_a_file_the_agent_built(
    door: tuple[DeviceFrameState, list[str]],
) -> None:
    """`_tar` is the only source of a change's tar: an agent's own could show the judge one report
    and the owner another."""
    state, spent = door
    frame = DeviceFrame("t1", transport=_door(state, spent))
    await frame.create(allow_domains=[], labels={})
    await frame.write_bytes("/out/change", bundle())
    with pytest.raises(PolicyDenied) as refused:
        await frame.publish("/out/change", "change", "T")
    assert "directory" in str(refused.value)
    assert (await frame.manifest())["entries"] == []


@pytest.mark.parametrize("kind", ["code", "webapp"])
@pytest.mark.parametrize("source", ["/work", "/work/site"])
async def test_the_users_folder_is_never_published_as_code(
    door: tuple[DeviceFrameState, list[str]], kind: str, source: str
) -> None:
    """The repo itself is never tarred. The role's own words say publish a directory as
    `code`; the door answers with the type that fits."""
    state, spent = door
    frame = DeviceFrame("t1", transport=_door(state, spent))
    await frame.create(allow_domains=[], labels={})
    await frame.write_bytes("/work/site/index.html", b"<h1>")
    with pytest.raises(PolicyDenied) as refused:
        await frame.publish(source, kind, "T")
    assert "change" in str(refused.value)
    assert (await frame.manifest())["entries"] == []


async def test_a_link_to_the_users_folder_is_the_users_folder(
    door: tuple[DeviceFrameState, list[str]],
) -> None:
    state, spent = door
    frame = DeviceFrame("t1", transport=_door(state, spent))
    await frame.create(allow_domains=[], labels={})
    await frame.write_bytes("/work/a.txt", b"a")
    os.symlink(state.paths.work, f"{state.paths.out}/site")
    with pytest.raises(PolicyDenied):
        await frame.publish("/out/site", "code", "T")


async def test_a_folder_that_holds_a_grant_is_refused_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source around a grant: the frame's own work folder with a grant inside it."""
    monkeypatch.setattr(settings.local, "home", tmp_path)
    (tmp_path / "helper.key").write_text("k3y")
    grant = tmp_path / "base-work" / "proj"
    grant.mkdir(parents=True)
    spec = FrameSpecIn(
        task_id="t2",
        kind=FrameKind.MACHINE,
        grants=[FolderGrant(path=str(grant), mode="ro", name="/grants/p")],
    )
    state = FrameRegistry(tmp_path / "home").create(spec)
    state.paths = state.paths.model_copy(update={"work": str(tmp_path / "base-work")})
    frame = DeviceFrame("t2", transport=_door(state, []))
    await frame.create(allow_domains=[], labels={})
    with pytest.raises(PolicyDenied):
        await frame.publish("/work", "code", "T")


async def test_a_frame_with_no_folder_grant_still_publishes_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings.local, "home", tmp_path)
    (tmp_path / "helper.key").write_text("k3y")
    state = FrameRegistry(tmp_path / "home").create(FrameSpecIn(task_id="t3", kind=FrameKind.MACHINE))
    frame = DeviceFrame("t3", transport=_door(state, []))
    await frame.create(allow_domains=[], labels={})
    await frame.write_bytes("/work/site/test.sh", b"exit 0")
    await frame.publish("/work/site", "code", "T")
    assert [e["type"] for e in (await frame.manifest())["entries"]] == ["code"]


async def test_code_made_in_out_still_publishes(door: tuple[DeviceFrameState, list[str]]) -> None:
    state, spent = door
    frame = DeviceFrame("t1", transport=_door(state, spent))
    await frame.create(allow_domains=[], labels={})
    await frame.write_bytes("/out/tool/test.sh", b"exit 0")
    await frame.publish("/out/tool", "code", "T")
    assert [e["type"] for e in (await frame.manifest())["entries"]] == ["code"]


async def test_publish_refuses_a_link_inside_the_change_as_a_refusal(
    door: tuple[DeviceFrameState, list[str]], tmp_path: Path
) -> None:
    """A planted link in the deliverable is the model's to fix, never a dead step."""
    state, spent = door
    frame = DeviceFrame("t1", transport=_door(state, spent))
    await frame.create(allow_domains=[], labels={})
    await frame.write_bytes("/out/change/report.md", REPORT.encode())
    os.symlink(tmp_path / "helper.key", f"{state.paths.out}/change/stolen")
    with pytest.raises(PolicyDenied) as refused:
        await frame.publish("/out/change", "change", "T")
    assert str(tmp_path) not in str(refused.value)
