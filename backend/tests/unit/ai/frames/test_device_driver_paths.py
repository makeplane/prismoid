# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""DeviceFrame's paths: only virtual roots out, grants and their
names, the runtime, and the alias of a root that needs quoting. The rig is
`tests/support/rigs/device_driver_rig.py`."""

from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from prismoid.ai.frames import device_driver
from prismoid.ai.frames.device_driver import DeviceFrame
from prismoid.ai.frames.driver import FrameUnreachable, TakesGrants
from prismoid.ai.frames.layout import OutsideFrame
from prismoid.ai.tools import labels
from prismoid.ai.tools.frame_tools import frame_toolset
from prismoid.errors import PolicyDenied
from prismoid.schemas.device import (
    RUNTIME_DIR,
    RUNTIME_MISSING,
    RUNTIME_STAMP,
    FolderGrant,
    FrameOut,
    grant_name_ok,
    runtime_python,
)
from prismoid.schemas.device_grants import mint_grant_name
from prismoid.schemas.machines import LABEL_KIND
from tests.support.rigs.device_driver_rig import BASE, FRAME, PATHS, Helper, b64, frame_for, made
from tests.support.rigs.device_driver_rig import helper as helper
from tests.support.rigs.device_driver_rig import helper_home as helper_home
from tests.support.runners import run_recorded


async def test_refusals_and_outside_errors_echo_only_virtual_paths(helper: Helper) -> None:
    helper.on("POST", "/frames/device-t1/files/read", httpx.Response(403, json={"detail": "protected"}))
    helper.on(
        "POST", "/frames/device-t1/files/write", httpx.Response(403, json={"detail": f"{BASE}/skills/x"})
    )
    helper.on("DELETE", "/frames/device-t1", httpx.Response(409, json={"detail": f"busy {PATHS.work}"}))
    helper.on(
        "PUT", "/frames/device-t1/egress", httpx.Response(422, json={"detail": f"bad {PATHS.artifacts}"})
    )
    frame = await made(helper)
    with pytest.raises(PermissionError) as read:
        await frame.read_file(f"{PATHS.work}/.env")  # the real spelling, as the model writes it
    assert str(read.value) == "/work/.env: protected"
    with pytest.raises(OutsideFrame) as outside:
        await frame.read_file(f"{PATHS.work}/../../.ssh/id")
    assert "/Users/me/code/demo" not in str(outside.value)
    with pytest.raises(RuntimeError) as gone:
        await frame.destroy()
    assert "/Users" not in str(gone.value) and "/work" in str(gone.value)
    with pytest.raises(RuntimeError) as allow:
        await frame.allow(["a.example"], 9)
    assert "/Users" not in str(allow.value) and "/artifacts" in str(allow.value)


async def test_publish_of_a_refused_read_says_refused_not_missing(helper: Helper) -> None:
    helper.on("POST", "/frames/device-t1/files/list", httpx.Response(200, json={"names": []}))
    helper.on(
        "POST",
        "/frames/device-t1/files/read",
        httpx.Response(403, json={"detail": f"{PATHS.work}/n.md is protected"}),
    )
    frame = await made(helper)
    with pytest.raises(PolicyDenied) as e:
        await frame.publish(f"{PATHS.work}/n.md", "document", "E")
    assert (
        "is protected" in str(e.value) and "no such file" not in str(e.value) and "/Users" not in str(e.value)
    )


async def test_virtual_text_leaves_a_longer_sibling_name_alone(helper: Helper) -> None:
    frame = await made(helper)
    sibling = f"{PATHS.work}-x/f"
    assert frame.virtual_text(f"{sibling} {PATHS.work}: {PATHS.work}.") == f"{sibling} /work: /work."


async def test_publish_type_error_echoes_a_virtual_path(helper: Helper) -> None:
    with pytest.raises(PolicyDenied) as e:
        await (await made(helper)).publish(f"{PATHS.work}/n.md", "spreadsheet", "E")
    assert "/Users" not in str(e.value) and "/work/n.md" in str(e.value)


async def test_close_is_bounded_when_the_browser_session_never_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The device drives its window through the shared remote-browser helpers
    (`remote_browser.close_session`), with its own close bound."""

    class _Hangs:
        async def close(self) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(device_driver, "_CLOSE_BOUND_S", 0.05)
    frame = DeviceFrame("t1", name="device-t1")
    frame._browser = _Hangs()  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]
    await asyncio.wait_for(frame.close(), 2.0)
    assert frame._browser is None  # pyright: ignore[reportPrivateUsage]


async def test_a_base_frame_has_no_browser_and_never_dials(monkeypatch: pytest.MonkeyPatch) -> None:
    async def dial(self: DeviceFrame) -> Any:
        raise AssertionError("a base frame dialled the relay")

    monkeypatch.setattr(DeviceFrame, "_dial", dial)
    frame = DeviceFrame("t1", profile="base", name="device-t1")
    assert await frame.browser({"verb": "snapshot"}) == {"ok": False, "error": "no browser in this frame"}


RO_REAL = "/Users/me/design/specs"


GRANTED = FrameOut(
    ref="device-t1",
    paths=PATHS.model_copy(update={"grants": {"/grants/specs": RO_REAL}}),
    device_name="MacBook Pro",
    grants=[
        FolderGrant(path=PATHS.work, mode="rw", name="/work"),
        FolderGrant(path=RO_REAL, mode="ro", name="/grants/specs"),
    ],
)


def test_a_grant_name_is_minted_once_and_never_reused() -> None:
    """A name comes from the folder's own name, made safe, and is
    deduplicated against every name the task ever had - revoked ones too - ignoring case."""
    assert mint_grant_name("/Users/me/a/Specs", []) == "/grants/Specs"
    assert mint_grant_name("/Users/me/b/specs", ["/work", "/grants/Specs"]) == "/grants/specs_2"
    assert mint_grant_name("/Users/me/c/my app!", []) == "/grants/my-app"
    assert mint_grant_name("/Users/me/.x", []) == "/grants/x"
    assert mint_grant_name("/Users/me/...", []) == "/grants/folder"
    taken = ["/grants/specs", "/grants/specs_2"]  # specs_2 was revoked: its name stays retired
    assert mint_grant_name("/Users/me/d/specs", taken) == "/grants/specs_3"
    assert all(grant_name_ok(n) for n in ("/work", "/grants/specs_3", "/grants/my-app"))
    assert not any(grant_name_ok(n) for n in ("/grants/..", "/grants/a/b", "/etc", "/grants/"))


async def test_create_tells_the_helper_the_tasks_grants(helper: Helper) -> None:
    frame = frame_for(helper)
    grants = list(GRANTED.grants)
    frame.grant(grants, docker=True)
    assert isinstance(frame, TakesGrants)
    await frame.create(labels={LABEL_KIND: "machine"})
    spec = helper.bodies("/frames")[0]
    assert spec["grants"] == [g.model_dump(mode="json") for g in grants] and spec["docker"] is True


async def test_bind_pushes_grants_only_when_they_changed(helper: Helper) -> None:
    helper.on("GET", "/frames/device-t1", httpx.Response(200, json=GRANTED.model_dump(mode="json")))
    pushed = GRANTED.model_copy(update={"grants": GRANTED.grants[:1], "paths": PATHS})
    helper.on("PUT", "/frames/device-t1/grants", httpx.Response(200, json=pushed.model_dump(mode="json")))
    same = DeviceFrame("t1", name="device-t1", transport=httpx.MockTransport(helper))
    same.grant(GRANTED.grants, docker=False)
    await same.bind()
    assert helper.bodies("/frames/device-t1/grants") == [], "the helper already holds them"
    assert same.paths.virtual(f"{RO_REAL}/a.md") == "/grants/specs/a.md"

    revoked = DeviceFrame("t1", name="device-t1", transport=httpx.MockTransport(helper))
    revoked.grant(GRANTED.grants[:1], docker=False)  # the read-only folder was revoked
    await revoked.bind()
    assert helper.bodies("/frames/device-t1/grants") == [
        {"grants": [GRANTED.grants[0].model_dump(mode="json")], "docker": False}
    ]
    assert revoked.paths.grants == () and revoked.paths.virtual(f"{RO_REAL}/a.md") is None

    untold = DeviceFrame("t1", name="device-t1", transport=httpx.MockTransport(helper))
    await untold.bind()  # a frame told nothing (the sweep, the reconciler) leaves the helper's as they are
    assert len(helper.bodies("/frames/device-t1/grants")) == 1


async def test_a_read_only_grant_never_reaches_a_record_in_its_real_spelling(helper: Helper) -> None:
    """The helper's refusal naming a read-only grant, a publish source in
    one, and a manifest path or source under no root at all - all virtual, or dropped."""
    files: dict[str, bytes] = {f"{RO_REAL}/brief.md": b"# B\n"}

    def read(b: dict[str, Any]) -> httpx.Response:
        if b["path"] == f"{RO_REAL}/secret.md":
            return httpx.Response(403, json={"detail": f"{RO_REAL}/secret.md is protected"})
        if b["path"] in files:
            return httpx.Response(200, json={"data_b64": b64(files[b["path"]])})
        return httpx.Response(404, json={"detail": "no"})

    def write(b: dict[str, Any]) -> httpx.Response:
        files[b["path"]] = base64.b64decode(b["data_b64"])
        return httpx.Response(204)

    helper.on("POST", "/frames", httpx.Response(200, json=GRANTED.model_dump(mode="json")))
    helper.on("POST", "/frames/device-t1/files/read", read)
    helper.on("POST", "/frames/device-t1/files/write", write)
    helper.on("POST", "/frames/device-t1/files/list", httpx.Response(200, json={"names": []}))
    frame = await made(helper)
    with pytest.raises(PermissionError) as refused:
        await frame.read_file("/grants/specs/secret.md")
    assert str(refused.value) == "/grants/specs/secret.md: /grants/specs/secret.md is protected"
    assert helper.bodies("/frames/device-t1/files/read")[0]["path"] == f"{RO_REAL}/secret.md"

    outside = "/Users/me/elsewhere/notes.md"
    await frame.publish(
        f"{RO_REAL}/brief.md", "document", "B", sources=[f"{RO_REAL}/brief.md", outside, "relative.md"]
    )
    stored = files[f"{PATHS.artifacts}/manifest.json"].decode()
    assert RO_REAL not in stored and outside not in stored and "/Users/me" not in stored
    [entry] = json.loads(stored)["entries"]
    assert entry["sources"] == ["/grants/specs/brief.md", "relative.md"]

    files[f"{PATHS.artifacts}/manifest.json"] = json.dumps(
        {"entries": [entry, {"path": outside, "sources": [outside]}, {**entry, "sources": [f"{RO_REAL}/x"]}]}
    ).encode()
    read_back = await frame.manifest()
    assert "/Users/me" not in json.dumps(read_back)
    assert [e["path"] for e in read_back["entries"]] == ["/artifacts/brief.md", "/artifacts/brief.md"]
    assert read_back["entries"][1]["sources"] == ["/grants/specs/x"]


def _built_runtime(home: Path) -> None:
    python = runtime_python(home)
    python.parent.mkdir(parents=True)
    python.write_text("")
    (home / RUNTIME_DIR / RUNTIME_STAMP).write_text("built")


async def test_the_systems_python_is_absolute_isolated_and_a_system_exec(
    helper: Helper, tmp_path: Path
) -> None:
    _built_runtime(tmp_path)
    helper.on(
        "POST",
        "/frames/device-t1/exec",
        httpx.Response(
            200,
            json={
                "exit_code": 0,
                "stdout": f"read {PATHS.artifacts}/a.docx",
                "stderr": "",
                "truncated": False,
            },
        ),
    )
    frame = await made(helper)
    done = await frame.system_python("/task/run.py", "/task/args.json", stdin="/verify/job.json", timeout=9.0)
    body = helper.bodies("/frames/device-t1/exec")[0]
    assert body["system"] is True and body["timeout"] == 9.0
    assert body["command"] == (
        f"\"$PRISMOID_PYTHON\" -I '{BASE}/task/run.py' '{BASE}/task/args.json' < '{BASE}/verify/job.json'"
    ), "the home's space is quoted, and the journal holds no path of the runtime"
    assert done.stdout == "read /artifacts/a.docx", "what a suite prints is stored"


async def test_with_no_runtime_the_systems_python_says_so_and_runs_nothing(helper: Helper) -> None:
    frame = await made(helper)
    done = await frame.system_python("/task/run.py", timeout=9.0)
    assert done.exit_code == 127 and RUNTIME_MISSING in done.stderr
    assert helper.bodies("/frames/device-t1/exec") == []


@pytest.mark.parametrize("name", ["a.docx", "a.pdf"])
async def test_a_publish_that_needs_the_runtime_says_how_to_get_it(helper: Helper, name: str) -> None:
    frame = await made(helper)
    with pytest.raises(PolicyDenied, match="prismoid up"):
        await frame.publish(f"/work/{name}", "document", "A")
    assert helper.bodies("/frames/device-t1/files/write") == []


async def test_a_stampless_runtime_is_no_runtime(helper: Helper, tmp_path: Path) -> None:
    _built_runtime(tmp_path)
    (tmp_path / RUNTIME_DIR / RUNTIME_STAMP).unlink()
    with pytest.raises(PolicyDenied, match="prismoid up"):
        await (await made(helper)).publish("/work/a.docx", "document", "A")


async def test_a_text_publish_needs_no_runtime(helper: Helper) -> None:
    def read(b: dict[str, Any]) -> httpx.Response:
        if b["path"].endswith("a.md"):
            return httpx.Response(200, json={"data_b64": b64(b"hi")})
        return httpx.Response(404, json={"detail": "no"})

    helper.on("POST", "/frames/device-t1/files/read", read)
    helper.on("POST", "/frames/device-t1/files/write", httpx.Response(204))
    await (await made(helper)).publish("/work/a.md", "document", "A")
    assert len(helper.bodies("/frames/device-t1/files/write")) == 2  # the bytes, then the manifest


ALIAS = "/Users/me/Library/Caches/Prismoid/roots/device-t1/abc"


ESCAPED = BASE.replace(" ", "\\ ")  # as a model fluent in shell types it


LINKED = FRAME.model_copy(update={"aliases": {f"{BASE}/task": ALIAS}})


async def aliased(helper: Helper, stdout: str = "") -> DeviceFrame:
    helper.on("POST", "/frames", httpx.Response(200, json=LINKED.model_dump(mode="json")))
    helper.on(
        "POST",
        "/frames/device-t1/exec",
        httpx.Response(200, json={"exit_code": 0, "stdout": stdout, "stderr": "", "truncated": False}),
    )
    return await made(helper)


@pytest.mark.parametrize(
    ("command", "sent"),
    [
        ("cat /task/plan.md", f"cat {ALIAS}/plan.md"),
        ("find /task -type f", f"find {ALIAS}/. -type f"),
        ("ls /task", f"ls {ALIAS}/."),
        ("tar cf x.tar /task/", f"tar cf x.tar {ALIAS}/./"),
        ('du -s "/task"', f'du -s "{ALIAS}/."'),
        ("-v /task:/x", f"-v {ALIAS}/.:/x"),
        ("ls /task/*", f"ls {ALIAS}/*"),
        ("ls /task*", f"ls {ALIAS}*"),
        (f"cat {BASE}/task/plan.md", f"cat {ALIAS}/plan.md"),
        (f'cat "{BASE}/task/plan.md"', f'cat "{ALIAS}/plan.md"'),
        (f"cat '{BASE}/task'", f"cat '{ALIAS}/.'"),
        (f"# don't\ncat {BASE}/task", f"# don't\ncat {ALIAS}/."),
        (f"cat {BASE}/task.bak {BASE}/tasks/x", f"cat {BASE}/task.bak {BASE}/tasks/x"),
        (f"cat /x{BASE}/task", f"cat /x{BASE}/task"),
        (f"cat {ESCAPED}/task/plan.md", f"cat {ALIAS}/plan.md"),
        (f"find {ESCAPED}/task -type f", f"find {ALIAS}/. -type f"),
        (f"cat {ESCAPED}/task.bak", f"cat {ESCAPED}/task.bak"),
    ],
)
async def test_a_root_that_needs_quoting_goes_by_its_alias(helper: Helper, command: str, sent: str) -> None:
    await (await aliased(helper)).exec(command)
    assert helper.bodies("/frames/device-t1/exec")[0]["command"] == sent


async def test_a_backslash_escaped_root_folds_back_like_any_other(helper: Helper) -> None:
    frame = await made(helper)
    assert frame.virtual_text(f"cut: cat {ESCAPED}/task/plan.md") == "cut: cat /task/plan.md"
    assert frame.virtual_text(f"cat {ESCAPED}/artifacts/a") == "cat /artifacts/a"


async def test_a_root_with_a_backslash_of_its_own_is_matched_as_spelled(helper: Helper) -> None:
    """`/Users/x/a\\ b` really is the folder's name: `_unescaped` alone folds it to no root."""
    root = "/Users/x/a\\ b"
    alias = "/private/tmp/prismoid-k/a1b2c3d4"
    wire = FRAME.model_copy(
        update={"paths": PATHS.model_copy(update={"out": root}), "aliases": {root: alias}}
    )
    helper.on("POST", "/frames", httpx.Response(200, json=wire.model_dump(mode="json")))
    helper.on(
        "POST",
        "/frames/device-t1/exec",
        httpx.Response(200, json={"exit_code": 0, "stdout": "", "stderr": "", "truncated": False}),
    )
    frame = await made(helper)

    await frame.exec(f"cat {root}/a")
    assert helper.bodies("/frames/device-t1/exec")[0]["command"] == f"cat {alias}/a"
    assert frame.virtual_text(f"cat {root}/a") == "cat /out/a"
    assert frame.virtual_text(f"cat {ESCAPED}/task/plan.md") == "cat /task/plan.md", "the plain case holds"


class _Call:
    tool_call_id = "toolu_1"


async def test_a_refused_read_tells_the_model_and_no_real_path(helper: Helper) -> None:
    """The tool refuses it before the helper is asked, and neither the answer nor the journal's
    denied_reason (an append-only row) holds a real path: a path off the frame is "that path"."""
    reasons: list[str] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        reasons.append(denied_reason)

    frame = await made(helper)
    read: Any = frame_toolset(("frame.read_file",), frame, journal).tools["read_file"].function
    answer = await read(_Call(), "/Users/me/.ssh/id_ed25519")
    assert answer.startswith("Refused:") and "outside the files you can read" in answer
    assert "/Users/me" not in answer
    assert len(reasons) == 1 and "that path is outside the files you can read" in reasons[0]
    assert "/Users/me" not in reasons[0]
    assert helper.bodies("/frames/device-t1/files/read") == []


@pytest.mark.parametrize(
    ("status", "said"),
    [(400, "a folder"), (413, "past the device's file limit"), (422, "a folder")],
)
async def test_a_read_the_door_refuses_is_an_answer_never_a_failed_task(
    helper: Helper, status: int, said: str
) -> None:
    """A folder, a file past the limit, a path the door cannot take: the model reads why and goes
    on. Anything raised here would end the whole task with no rung spent."""
    helper.on("POST", "/frames/device-t1/files/read", httpx.Response(status, json={"detail": "a folder"}))
    frame = await made(helper)
    read: Any = frame_toolset(("frame.read_file",), frame).tools["read_file"].function
    answer = await read(_Call(), f"{PATHS.work}/src")
    assert answer.startswith("Refused:") and said in answer


@pytest.mark.parametrize("status", [400, 413])
async def test_a_write_the_door_refuses_is_an_answer_never_a_failed_task(helper: Helper, status: int) -> None:
    helper.on("POST", "/frames/device-t1/files/write", httpx.Response(status, json={"detail": "a folder"}))
    frame = await made(helper)
    write: Any = frame_toolset(("frame.write_file",), frame).tools["write_file"].function
    answer = await write(_Call(), f"{PATHS.work}/src", "text")
    assert answer.startswith("Refused:")


@pytest.mark.parametrize("tool", ["read_file", "write_file"])
async def test_a_helper_that_did_not_answer_refused_nothing(helper: Helper, tool: str) -> None:
    """The step's to wait out (it parks), and never the model's to read as a refusal."""
    for path in ("read", "write"):
        helper.on("POST", f"/frames/device-t1/files/{path}", httpx.Response(503))
    frame = await made(helper)
    grant = (f"frame.{tool}",)
    function: Any = frame_toolset(grant, frame).tools[tool].function
    args = (f"{PATHS.work}/a",) if tool == "read_file" else (f"{PATHS.work}/a", "text")
    with pytest.raises(FrameUnreachable):
        await function(_Call(), *args)


async def test_the_record_says_virtual_paths_and_folds_before_the_clamp(helper: Helper) -> None:
    """The journal and the live label from one policy (`labels`), on the args as the model wrote
    them: its real roots, either spelling, an alias, a `..`. The clamp comes after: a clamped real
    path would keep the very head it must lose."""
    rows: list[tuple[str, str]] = []

    async def journal(
        tool: str,
        ok: bool,
        summary: str = "",
        duration_ms: int = 0,
        denied_reason: str = "",
        tool_call_id: str = "",
        ref: str | None = None,
    ) -> None:
        rows.append((tool, summary))

    previews: list[str] = []

    async def preview(path: str, _content: str) -> None:
        previews.append(path)

    frame = await aliased(helper, stdout="ok")
    helper.on("POST", "/frames/device-t1/files/write", httpx.Response(204))
    helper.on("POST", "/frames/device-t1/files/read", httpx.Response(200, json={"data_b64": b64(b"hi")}))
    granted = ("frame.write_file", "frame.read_file", "frame.shell")
    calls = [
        ("write_file", {"path": f"{BASE}/out/memo.md", "content": "x"}),
        ("read_file", {"path": f"{ALIAS}/plan.md"}),
        ("shell", {"command": f"cat {ESCAPED}/out/a '{BASE}/out/b' {ALIAS}/c '{BASE}/task' {BASE}/out/d"}),
    ]
    await run_recorded(frame_toolset(granted, frame, journal, (), preview), calls, journal, frame=frame)
    command = "cat /out/a '/out/b' /task/c '/task' /out/d"
    assert rows == [
        ("frame.write_file", "/out/memo.md"),
        ("frame.read_file", "/task/plan.md"),
        ("frame.shell", command),
    ]
    assert previews == ["/out/memo.md"]

    assert labels.label_for("write_file", {"path": f"{BASE}/out/memo.md"}, frame) == "/out/memo.md"
    assert labels.label_for("read_file", {"path": f"{ALIAS}/plan.md"}, frame) == "/task/plan.md"
    assert labels.label_for("publish", {"type": "document", "path": f"{BASE}/out/a"}, frame) == (
        "document:/out/a"
    )
    assert labels.label_for("generate_image", {"path": f"{BASE}/task/a.png"}, frame).startswith(
        "/task/a.png "
    )
    assert labels.label_for("shell", {"command": f"cat {BASE}/out/" + "x" * 200}, frame) == (
        labels.clamp("cat /out/" + "x" * 200, labels.COMMAND_MAX)
    )
    for off_the_frame in ("/Users/me/.ssh/id_ed25519", f"{BASE}/out/../../../../../../.ssh/id"):
        assert labels.label_for("read_file", {"path": off_the_frame}, frame) == "that path"


async def test_a_root_that_needs_no_quoting_is_left_alone(helper: Helper) -> None:
    await (await aliased(helper)).exec("cat /work/a")
    assert helper.bodies("/frames/device-t1/exec")[0]["command"] == "cat /Users/me/code/demo/a"


async def test_the_agent_never_sees_an_alias_and_nothing_stores_one(helper: Helper) -> None:
    frame = await aliased(helper, stdout=f"{ALIAS}/plan.md\n{ALIAS}/./sub/a\n{ALIAS}/.\n{ALIAS}/.hidden\n")
    assert (await frame.exec("pwd")).stdout == (
        f"{BASE}/task/plan.md\n{BASE}/task/sub/a\n{BASE}/task\n{BASE}/task/.hidden\n"
    )
    assert frame.virtual_text(f"cut: cat {ALIAS}/plan.md") == "cut: cat /task/plan.md"
    assert frame.virtual_text(f"cut: find {ALIAS}/. -type f") == "cut: find /task -type f"
    assert frame.virtual_text(f"{ALIAS}/./sub/a") == "/task/sub/a"


async def test_the_cwd_stays_the_real_path(helper: Helper) -> None:
    await (await aliased(helper)).exec("ls", cwd="/task")
    assert helper.bodies("/frames/device-t1/exec")[0]["command"] == f"cd '{BASE}/task' && ls"


async def test_a_traceback_through_the_runtime_is_stored_by_its_word(helper: Helper, tmp_path: Path) -> None:
    _built_runtime(tmp_path)
    where = f"{tmp_path}/runtime/venv/lib/python3.12/site-packages/openpyxl/x.py"
    helper.on(
        "POST",
        "/frames/device-t1/exec",
        httpx.Response(
            200,
            json={"exit_code": 1, "stdout": "", "stderr": f'File "{where}"', "truncated": False},
        ),
    )
    done = await (await made(helper)).system_python("/task/run.py", timeout=9.0)
    assert done.stderr == 'File "/runtime/venv/lib/python3.12/site-packages/openpyxl/x.py"'
