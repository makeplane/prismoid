# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Prismoid's own Python on the device, built for real in a private home.

A real `ensure_runtime` once per module (uv's cache makes it quick after the first), then a real
helper on its Unix socket with the worker's own `DeviceFrame`: the system's Python runs by absolute
path, isolated, inside the fence, and nothing an agent wrote is ever imported. The agent reads the
runtime too: `python3` in a frame's own fresh folder, `$PRISMOID_PYTHON` in a granted one, and it is
never written."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import uvicorn

from prismoid.ai.frames.device_driver import DeviceFrame
from prismoid.ai.frames.driver import ExecResult, run_python
from prismoid.ai.frames.layout import STEP_MARKER
from prismoid.ai.verification import pipeline
from prismoid.ai.verification.qa_outcome import QA_CHECK_TIMEOUT_S
from prismoid.conf import settings
from prismoid.device.frames import JOURNAL_FILE
from prismoid.local import runtime
from prismoid.local.runtime import ensure_runtime
from prismoid.operations import qa
from prismoid.operations.execution import harvest as ex_harvest
from prismoid.operations.machines import ready as machine_ready
from prismoid.runner.device_door import create_app
from prismoid.schemas.artifacts import Manifest
from prismoid.schemas.device import FolderGrant, runtime_python
from prismoid.schemas.machines import retry_note
from prismoid.schemas.qa import QACheck
from prismoid.schemas.verification import CheckStatus, VerifierSuite
from tests.support.markers import darwin_only
from tests.support.rigs.chrome_rig import short_home

pytestmark = darwin_only  # no database: the helper, the fence and the runtime only

PROBE = "/task/probe.py"
IMPORTS = """\
import json, sys
import docx, plane_house
print(json.dumps({"docx": docx.__file__, "house": plane_house.__file__, "isolated": sys.flags.isolated}))
"""
# What the shell tool's doc promises, and the house style the assemble walkers build on.
PROMISED = "import docx, openpyxl, pptx, reportlab, pypdf, pandas, matplotlib, yaml, plane_house"
BUILD_DOCX = """\
from plane_house.docx_theme import heading, house_document
d = house_document("Quarterly memo")
heading(d, "Quarterly memo", first=True)
d.add_paragraph("Revenue held steady across the quarter, and nothing else changed.")
d.save("memo.docx")
"""
BUILD_PDF = """\
from reportlab.pdfgen import canvas
c = canvas.Canvas("memo.pdf")
c.drawString(72, 720, "Revenue held steady across the quarter, and nothing else changed.")
c.showPage()
c.save()
"""


@pytest.fixture(scope="module")
def home() -> Iterator[Path]:
    top = short_home()
    home = top / "App Support"  # the default home has a space in it, and every path must survive that
    ensure_runtime(home)
    yield home
    shutil.rmtree(top, ignore_errors=True)


@pytest.fixture
async def helper(home: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Path]:
    monkeypatch.setattr(settings.local, "home", home)
    app = create_app(home, device_name=lambda: "Test Mac", chrome_path="/nonexistent")
    server = uvicorn.Server(
        uvicorn.Config(
            app, uds=str(home / "run" / "helper.sock"), log_level="critical", timeout_graceful_shutdown=1
        )
    )
    serving = asyncio.create_task(server.serve())
    while not server.started:
        if serving.done():
            await serving
        await asyncio.sleep(0.02)
    try:
        yield home
    finally:
        server.should_exit = True
        await serving


@pytest.fixture
async def frame(helper: Path) -> AsyncIterator[DeviceFrame]:
    made = DeviceFrame(f"t{uuid.uuid4().hex[:8]}")
    await made.create(labels={"plane.kind": "machine"})
    try:
        yield made
    finally:
        await made.destroy()


async def _system_script(frame: DeviceFrame, body: str) -> ExecResult:
    await frame.write_bytes(PROBE, body.encode(), mode=0o444, owner="system")
    return await frame.system_python(PROBE, timeout=120.0)


def test_runtime_created_once_and_stamped(home: Path) -> None:
    stamp = home / "runtime" / "stamp"
    before = stamp.stat().st_mtime_ns, runtime_python(home).lstat().st_mtime_ns
    assert ensure_runtime(home) == runtime_python(home)
    assert (stamp.stat().st_mtime_ns, runtime_python(home).lstat().st_mtime_ns) == before


async def test_system_exec_uses_runtime_python(frame: DeviceFrame, helper: Path) -> None:
    done = await _system_script(frame, IMPORTS)
    assert done.ok, done.stderr
    seen = json.loads(done.stdout)
    assert seen["isolated"] == 1
    assert seen["docx"].startswith("/runtime/venv/") and seen["house"].startswith("/runtime/venv/")
    assert str(helper) not in done.stdout
    on_path = await frame._run('python3 -c "import docx, plane_house"', 60.0, system=True)  # pyright: ignore[reportPrivateUsage]
    assert on_path.ok, on_path.stderr


async def test_a_traceback_through_site_packages_stores_no_real_path(
    frame: DeviceFrame, helper: Path
) -> None:
    done = await _system_script(frame, 'import openpyxl\nopenpyxl.load_workbook("/no/such.xlsx")\n')
    assert done.exit_code != 0 and "/runtime/venv/lib/" in done.stderr and "site-packages" in done.stderr
    assert str(helper) not in done.stderr


async def test_on_a_fresh_folder_an_agents_python3_is_the_runtime(frame: DeviceFrame, helper: Path) -> None:
    """An agent exec, not a system one: the shell tool's `python3 <work>/build.py`, word for word."""
    await frame.write_file("/work/build.py", BUILD_DOCX)
    built = await frame.agent_exec(f"python3 {frame.paths.work}/build.py")
    assert built.ok, built.stderr
    assert (Path(frame.paths.work) / "memo.docx").stat().st_size > 0
    prefix = await frame.exec('python3 -c "import sys; print(sys.prefix)"')
    assert prefix.stdout.strip() == str(helper / "runtime" / "venv")


async def test_what_the_model_writes_keeps_its_own_words(frame: DeviceFrame) -> None:
    """Through the real helper and fence: text that looks like a root lands in the file as typed."""
    written = await frame.agent_exec("cat > routes.py <<'PY'\n@app.get(\"/task/{id}\")\nPY")
    assert written.ok, written.stderr
    assert (Path(frame.paths.work) / "routes.py").read_text() == '@app.get("/task/{id}")\n'


async def test_every_library_the_shell_doc_promises_imports(frame: DeviceFrame) -> None:
    done = await frame.exec(f'python3 -c "{PROMISED}"', timeout=120.0)
    assert done.ok, done.stderr
    assert done.stderr == "", "nothing warns on the way"


@pytest.fixture
def folder() -> Iterator[Path]:
    """A folder in the user's real home: a grant must be in the home."""
    path = Path(tempfile.mkdtemp(prefix="prismoid-e5-", dir=Path.home()))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


async def test_in_a_granted_folder_python3_is_the_users(helper: Path, folder: Path) -> None:
    """Their real toolchain; Prismoid's is one word away, and leaks into nothing else."""
    made = DeviceFrame(f"t{uuid.uuid4().hex[:8]}")
    made.grant([FolderGrant(path=str(folder), mode="rw", name="/work")], docker=False)
    await made.create(labels={"plane.kind": "machine"})
    try:
        venv = str(helper / "runtime" / "venv")
        prefix = await made.exec('python3 -c "import sys; print(sys.prefix)"')
        assert prefix.ok, prefix.stderr
        assert not prefix.stdout.strip().startswith(venv)
        path = await made.exec('echo "$PATH"; echo "[$PYTHONPATH]"')
        assert venv not in path.stdout and "[]" in path.stdout
        await made.write_file("/work/s.py", "import docx, plane_house\nprint(docx.__file__)\n")
        ours = await made.exec('"$PRISMOID_PYTHON" s.py')
        assert ours.ok, ours.stderr
        assert ours.stdout.startswith(venv)
    finally:
        await made.destroy()


# Every shape keeps its meaning through an alias, because no shell grammar is read. `ROOT` is `out/`,
# in a home with a space; each row is (the command, what it prints).
SHAPES = [
    ("cat ROOT/a", "hello\n"),
    ('cat "ROOT/a"', "hello\n"),
    ("cat 'ROOT/a'", "hello\n"),
    ('x="$(cat "ROOT/a")"; echo "$x"', "hello\n"),
    ('cd "$(dirname "ROOT/a")" && cat a', "hello\n"),
    ('echo "$(cat ROOT/a)"', "hello\n"),
    ("echo `cat ROOT/a`", "hello\n"),
    ('y="${x:-"ROOT"}"; cat "$y/a"', "hello\n"),
    ("# don't touch\ncat ROOT/a", "hello\n"),
    ("cat <<\\EOF\nROOT/a\nEOF", "ROOT/a\n"),
    ("cat <<'A-B'\nROOT/a\nA-B", "ROOT/a\n"),
    ('cat <<"A B"\nROOT/a\nA B', "ROOT/a\n"),
    ("cat <<< ROOT/a; cat ROOT/a", "ROOT/a\nhello\n"),
    ("echo $((1<<2)); cat ROOT/a", "4\nhello\n"),
    ("echo it\\'s; cat ROOT/a", "it's\nhello\n"),
    # A bare root is the folder, not the link that spells it.
    ("find ROOT -type f", "ROOT/a\nROOT/sub/b\n"),
    ("find ROOT/ -type f", "ROOT/a\nROOT/sub/b\n"),
    ('find "ROOT" -type f', "ROOT/a\nROOT/sub/b\n"),
    ("grep -r hello ROOT", "ROOT/a:hello\n"),
    ("ls ROOT", "a\nsub\n"),
    ("ls -a ROOT", ".\n..\na\nsub\n"),
    ("ls -la ROOT | wc -l | tr -d ' '", "5\n"),
    ("test -d ROOT && echo yes", "yes\n"),
    ("cd ROOT && pwd", "ROOT\n"),
]


@pytest.mark.parametrize(
    ("command", "printed"), [("cat ESCAPED/a", "hello\n"), ("find ESCAPED -type f", "ROOT/a\n")]
)
async def test_a_backslash_escaped_root_goes_through_bash(
    frame: DeviceFrame, command: str, printed: str
) -> None:
    """The real bash, on the root as a model fluent in shell spells it (`Application\\ Support`)."""
    root = str(frame.paths.out)
    (Path(root) / "a").write_text("hello\n")
    done = await frame.exec(command.replace("ESCAPED", root.replace(" ", "\\ ")))
    assert done.ok, done.stderr
    assert done.stdout == printed.replace("ROOT", root)


async def test_a_backslash_escaped_root_never_reaches_the_journal_head(
    frame: DeviceFrame, helper: Path
) -> None:
    """What a gate reason and `resume_state` store: the head, read back through the real journal."""
    escaped = str(frame.paths.out).replace(" ", "\\ ")
    running = asyncio.create_task(frame.exec(f"sleep 30; cat {escaped}/a"))
    try:
        for _ in range(100):
            if (cut := await frame.interrupted()) is not None:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the command never reached the journal")
        assert cut.command_head == "sleep 30; cat /out/a", "the stored head is virtual"
        held = "".join(p.read_text() for p in helper.rglob(JOURNAL_FILE))  # noqa: ASYNC240 - test check
        real = str(frame.paths.out)
        assert "sleep 30" in held and real not in held and escaped not in held, "nor its own file"
    finally:
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running


@pytest.mark.parametrize(("command", "printed"), SHAPES)
async def test_a_command_keeps_its_meaning_through_an_alias(
    frame: DeviceFrame, command: str, printed: str
) -> None:
    root = str(frame.paths.out)
    assert " " in root
    (Path(root) / "a").write_text("hello\n")
    (Path(root) / "sub").mkdir()
    (Path(root) / "sub" / "b").write_text("bye\n")
    done = await frame.exec(command.replace("ROOT", root))
    assert done.ok, done.stderr
    lines = sorted(done.stdout.splitlines()) if "find" in command else done.stdout.splitlines()
    assert lines == printed.replace("ROOT", root).splitlines(), "the agent sees its own spelling back"


@pytest.mark.parametrize("spelled", ["/work", "real"])
async def test_a_bare_root_counts_its_size_archives_and_copies(frame: DeviceFrame, spelled: str) -> None:
    """`du`, `tar` and `cp -R` act on the folder, in the virtual spelling and the real one."""
    work = Path(str(frame.paths.work))
    (work / "sub").mkdir()
    (work / "sub" / "b").write_text("x" * 8192)
    (work / "a").write_text("hello\n")
    root = f"'{work}'" if spelled == "real" else spelled
    size = await frame.exec(f"du -sk {root}")
    assert size.ok and int(size.stdout.split()[0]) >= 8, size.stdout
    assert (await frame.exec(f"tar cf /out/w.tar {root}")).ok
    with tarfile.open(Path(str(frame.paths.out)) / "w.tar") as held:
        names = [m.name for m in held.getmembers() if m.isfile()]
    assert sorted(n.rsplit("/", 1)[-1] for n in names) == ["a", "b"]
    assert (await frame.exec(f"cp -R {root} /out/copy")).ok
    copy = Path(str(frame.paths.out)) / "copy"
    assert copy.is_dir() and not copy.is_symlink()
    assert (copy / "sub" / "b").read_text() == "x" * 8192


async def test_the_two_meanings_a_bare_root_changes(frame: DeviceFrame) -> None:
    """What `alias/.` costs, pinned so a change is seen: `cp -R` into a folder that exists copies
    the contents, as `cp -R /work/.` does, and `basename` says `.`."""
    work, out = Path(str(frame.paths.work)), Path(str(frame.paths.out))
    (work / "a").write_text("hello\n")
    (out / "dest").mkdir()
    assert (await frame.exec("cp -R /work /out/dest")).ok
    assert (out / "dest" / "a").is_file() and not (out / "dest" / "work").exists()
    assert (await frame.exec("basename /work")).stdout == ".\n"


async def test_a_retry_is_told_what_the_failed_attempt_changed(frame: DeviceFrame) -> None:
    """On a default-home frame: the listing the retry's note is written from."""
    await frame.write_bytes(STEP_MARKER, b"", mode=0o444, owner="system")
    await asyncio.sleep(1.1)  # the marker's mtime has seconds to tell apart on some volumes
    assert " " in str(frame.paths.work)
    assert (await frame.exec("mkdir -p /work/sub && echo x > /work/sub/changed.txt")).ok
    changed = await machine_ready._changed_files(frame)  # pyright: ignore[reportPrivateUsage]
    assert changed is not None and changed == [
        "/work/sub/changed.txt"
    ]  # never a real path: the prompt is stored
    assert "changed no files" not in retry_note(2, changed)


async def test_a_recording_stage_clears_what_an_earlier_crash_left(frame: DeviceFrame) -> None:
    """`_recording_stage`'s clean, through a bare `/out`: the old copy goes, nothing else does."""
    out = Path(str(frame.paths.out))
    (out / "recording-pub-old").mkdir()
    (out / "recording-pub-old" / "x").write_text("old")
    (out / "keep").write_text("keep")
    Path(frame.recording_dir.replace("/task", str(frame.paths.task))).mkdir(parents=True)  # noqa: ASYNC240
    copy, stage, remove = ex_harvest.recording_stage(frame, "recording-pub-new")
    staged = await frame.exec(stage)
    assert staged.ok, staged.stderr
    assert sorted(p.name for p in out.iterdir()) == ["keep", "recording-pub-new"]  # noqa: ASYNC240
    assert (await frame.exec(remove)).ok
    assert sorted(p.name for p in out.iterdir()) == ["keep"]  # noqa: ASYNC240
    assert copy == "/out/recording-pub-new"


@pytest.mark.parametrize("name", ["rock&roll", "Bob's stuff", "a b", "$x (1)"])
async def test_a_granted_folder_with_an_awkward_name_works(helper: Path, name: str) -> None:
    folder = Path(tempfile.mkdtemp(prefix="prismoid-e5-", dir=Path.home())) / name
    folder.mkdir()
    made = DeviceFrame(f"t{uuid.uuid4().hex[:8]}")
    made.grant([FolderGrant(path=str(folder), mode="rw", name="/work")], docker=False)
    await made.create(labels={"plane.kind": "machine"})
    try:
        wrote = await made.exec("echo hi > /work/a && cat /work/a")
        assert (wrote.ok, wrote.stdout) == (True, "hi\n"), wrote.stderr
        assert (folder / "a").read_text() == "hi\n", "it is the person's folder, not a copy"
        real = await made.exec(f"cd {made.paths.work} && pwd -P")
        assert real.stdout.strip() == str(folder)
        logical = await made.exec("cd /work && pwd")
        assert logical.stdout.strip() == str(folder), "the agent's own spelling, never the alias"
        assert made.virtual_text(logical.stdout.strip()) == "/work"
    finally:
        await made.destroy()
        shutil.rmtree(folder.parent, ignore_errors=True)


async def test_an_alias_is_outside_the_home_and_no_exec_moves_it(frame: DeviceFrame, helper: Path) -> None:
    aliases = frame._aliases  # pyright: ignore[reportPrivateUsage]
    assert aliases and str(helper) not in "".join(aliases.values())
    await frame.exec("true")
    real, alias = next(iter(aliases.items()))
    assert Path(alias).resolve() == Path(real).resolve()  # noqa: ASYNC240 - test check
    folder = Path(alias).parent
    for command in (f"rm {alias}", f"ln -s / {folder}/evil", f"touch {folder}/x"):
        assert (await frame.exec(command)).exit_code != 0, command
    assert Path(alias).is_symlink() and sorted(p.name for p in folder.iterdir()) == sorted(  # noqa: ASYNC240 - test check
        Path(a).name for a in aliases.values()
    )


async def test_a_wrong_alias_or_a_stale_folder_is_replaced(helper: Path) -> None:
    """Only damage from outside gets here: it must not 500 every command of the frame."""
    made = DeviceFrame(f"t{uuid.uuid4().hex[:8]}")
    await made.create(labels={"plane.kind": "machine"})
    try:
        aliases = made._aliases  # pyright: ignore[reportPrivateUsage]
        (real, alias), *_ = aliases.items()
        folder = Path(alias).parent
        await made.exec("true")
        Path(alias).unlink()  # noqa: ASYNC240
        Path(alias).symlink_to("/tmp")  # noqa: ASYNC240
        (folder / "stale-folder" / "deep").mkdir(parents=True)
        (folder / "stale-file").write_text("x")
        assert (await made.exec("true")).ok
        assert os.readlink(alias) == real
        assert sorted(p.name for p in folder.iterdir()) == sorted(Path(a).name for a in aliases.values())
    finally:
        await made.destroy()


async def test_an_alias_gone_missing_is_made_again_and_goes_with_the_frame(helper: Path) -> None:
    made = DeviceFrame(f"t{uuid.uuid4().hex[:8]}")
    await made.create(labels={"plane.kind": "machine"})
    aliases = made._aliases  # pyright: ignore[reportPrivateUsage]
    folder = Path(next(iter(aliases.values()))).parent
    assert (await made.exec("cat /task/nothing || true")).ok
    next(iter(folder.iterdir())).unlink()  # the system purged its caches
    (folder / "stale").symlink_to("/tmp")
    assert (await made.exec("ls /out /task /artifacts")).ok
    assert sorted(p.name for p in folder.iterdir()) == sorted(Path(a).name for a in aliases.values())
    await made.destroy()
    assert not folder.exists()


async def test_git_and_the_fence_hold_through_an_alias(helper: Path, folder: Path) -> None:
    """A granted folder with a space: git runs, and the protected `.git` is as protected."""
    spaced = folder / "my project"
    spaced.mkdir()
    subprocess.run(["git", "init", "-q", str(spaced)], check=True)  # noqa: ASYNC221 - setup
    made = DeviceFrame(f"t{uuid.uuid4().hex[:8]}")
    made.grant([FolderGrant(path=str(spaced), mode="rw", name="/work")], docker=False)
    await made.create(labels={"plane.kind": "machine"})
    try:
        (spaced / "f").write_text("x\n")
        assert (await made.exec("git -C /work add f && git -C /work status --short")).stdout == "A  f\n"
        for planted in ("/work/.git/hooks/pre-commit", "/work/.git/config", "/work/.envrc"):
            denied = await made.exec(f"echo x >> {planted}")
            assert denied.exit_code != 0 and "Operation not permitted" in denied.stderr, planted
        linked = await made.exec(f"ln -s {Path.home()}/.ssh /work/out-link")
        assert "moved" in linked.stderr and not (spaced / "out-link").exists()
    finally:
        await made.destroy()


async def test_no_pythonpath_on_a_fresh_folder_either(frame: DeviceFrame) -> None:
    done = await frame.exec('echo "[$PYTHONPATH]"')
    assert done.stdout.strip() == "[]"


@pytest.mark.parametrize("planted", ["docx.py", "plane_house.py", "sitecustomize.py"])
async def test_a_module_planted_in_the_cwd_is_never_imported(frame: DeviceFrame, planted: str) -> None:
    """A system command's cwd is `out/`, the agent's to write."""
    out = Path(str(frame.paths.out))
    (out / planted).write_text(f"open({str(out / 'imported')!r}, 'w').close()\n")
    done = await _system_script(frame, IMPORTS)
    assert done.ok, done.stderr
    assert not (out / "imported").exists()
    assert str(out) not in done.stdout
    line = "import docx, plane_house"
    isolated = f'"$PRISMOID_PYTHON" -I -c "{line}"'
    done = await frame._run(isolated, 60.0, system=True)  # pyright: ignore[reportPrivateUsage]
    assert done.ok and not (out / "imported").exists(), done.stderr
    if planted != "sitecustomize.py":  # the plant is real: without `-I` the cwd is on the path
        await frame._run(isolated.replace(" -I", ""), 60.0, system=True)  # pyright: ignore[reportPrivateUsage]
        assert (out / "imported").exists()


async def test_no_exec_writes_the_runtime(frame: DeviceFrame, helper: Path) -> None:
    target = runtime_python(helper).parent / "planted"
    agent = await frame.exec(f"touch '{target}'")
    system = await _system_script(frame, f"open({str(target)!r}, 'w').close()\n")
    assert agent.exit_code != 0 and system.exit_code != 0
    assert not target.exists()


async def test_publish_docx_on_device(
    frame: DeviceFrame, helper: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A `.docx` end to end: built by the agent's own exec, published through the door, its preview
    extracted by the runtime in the fence, and the document suite reading it in a fresh frame."""
    monkeypatch.setattr(settings.local, "has_office", False)
    await frame.write_file("/work/build.py", BUILD_DOCX)
    built = await frame.exec("python3 /work/build.py")
    assert built.ok, built.stderr
    await frame.publish("/work/memo.docx", "document", "The memo")
    manifest = Manifest.model_validate(await frame.manifest())

    previews, renditions = await ex_harvest.extract_previews(frame, manifest)
    assert list(previews) == ["/artifacts/memo.docx"]
    assert b"Quarterly memo" in previews["/artifacts/memo.docx"]
    assert renditions == {}

    payload = await frame.read_bytes("/artifacts/memo.docx")
    entry = manifest.latest()["/artifacts/memo.docx"]
    fresh = DeviceFrame(f"verify-{uuid.uuid4().hex[:8]}")
    await fresh.create(labels={"plane.kind": "verify"})
    try:
        await fresh.write_bytes("/artifacts/memo.docx", payload, mode=0o444, owner="system")
        checks = await pipeline.run_suite(
            fresh,
            VerifierSuite.DOCUMENT,
            artifact_path="/artifacts/memo.docx",
            artifact_sha256=entry.sha256,
            sources=[],
            allow_domains=[],
        )
    finally:
        await fresh.destroy()
    assert checks, "the suite ran"
    ran = {c.name: c for c in checks}
    assert "suite:ran" not in ran, ran
    assert not [c for c in checks if c.status is CheckStatus.FAIL and c.name.startswith("artifact")], checks
    assert str(helper) not in json.dumps([c.model_dump(mode="json") for c in checks]), "no real path stored"


async def _precheck(built: DeviceFrame, name: str) -> tuple[ExecResult, DeviceFrame]:
    """The QA gate's frame work on a scratch device frame, as `qa._review` does it: the precheck
    through `run_python`, its job in the frame's own spelling."""
    fresh = DeviceFrame(f"qa-{uuid.uuid4().hex[:8]}")
    await fresh.create(allow_domains=[], labels={"plane.kind": "qa"})
    await fresh.write_bytes(f"/work/{name}", await built.read_bytes(f"/work/{name}"))
    await qa.ship_precheck(fresh)
    job = {
        "deliverable": fresh.paths.render(f"/work/{name}"),
        "rendition": None,
        "outline": None,
        "kind": "document",
        "unit": "pages",
        "target_units": 1,
        "target_words": 15,
        "tolerance": 0.3,
        "figure_ids": [],
    }
    await fresh.write_bytes("/work/qa_job.json", json.dumps(job).encode())
    done = await run_python(
        fresh, "/work/qa_precheck.py", stdin="/work/qa_job.json", timeout=QA_CHECK_TIMEOUT_S
    )
    return done, fresh


async def test_the_qa_precheck_reads_a_docx_on_the_device(frame: DeviceFrame, helper: Path) -> None:
    """The runtime parses it in the fence; the visual pass, which needs LibreOffice, is simply
    absent, at once and without hanging."""
    await frame.write_file("/work/build.py", BUILD_DOCX)
    assert (await frame.exec("python3 /work/build.py")).ok
    done, fresh = await _precheck(frame, "memo.docx")
    try:
        assert done.ok, done.stderr
        raw = json.loads(done.stdout.strip().splitlines()[-1])
        checks = [QACheck.model_validate(c) for c in raw["checks"]]
        assert [c.ok for c in checks if c.name == "qa:extraction"] == [True], checks
        assert [c for c in checks if c.name.startswith("qa:layout")], "geometry needs no LibreOffice"
        assert "Revenue held steady" in await fresh.read_file("/work/extracted.md")
        assert str(helper) not in done.stdout, "no real path stored"
    finally:
        await fresh.destroy()


async def test_the_house_render_says_geometry_only_on_the_device(frame: DeviceFrame) -> None:
    """What `longform.py` tells the agent to run, with `soffice` on this Mac's PATH or not."""
    await frame.write_file("/work/build.py", BUILD_DOCX)
    assert (await frame.exec("python3 /work/build.py")).ok
    done = await frame.exec("python3 -m plane_house.render memo.docx --pages pages")
    assert "geometry checks only" in done.stdout and "Traceback" not in done.stderr


def test_the_pip_branch_builds_a_runtime_whose_scripts_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No uv on PATH, for real: Prismoid's Python, pip, the rename, and a console script afterwards, in
    a home with a space. One small requirement: the whole list is pinned by the uv build."""
    reqs = tmp_path / "reqs.txt"
    reqs.write_text("pyyaml==6.0.2\n")
    monkeypatch.setattr(runtime, "REQUIREMENTS", reqs)
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)
    python = ensure_runtime(tmp_path / "App Support")
    assert subprocess.run([python, "-I", "-c", "import yaml, plane_house"], check=False).returncode == 0
    pip = subprocess.run([python.parent / "pip", "--version"], capture_output=True, text=True, check=False)
    assert pip.returncode == 0, pip.stderr


@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="needs poppler")
async def test_the_qa_precheck_looks_at_a_pdfs_pages_on_the_device(frame: DeviceFrame) -> None:
    """poppler through a system command's PATH: the pages the composer reads come back."""
    await frame.write_file("/work/build.py", BUILD_PDF)
    built = await frame.exec("python3 /work/build.py")
    assert built.ok, built.stderr
    done, fresh = await _precheck(frame, "memo.pdf")
    try:
        assert done.ok, done.stderr
        pages = await pipeline.sampled_pages(fresh)
        assert [page.number for page in pages] == [1]
        assert pages[0].png.startswith(b"\x89PNG")
    finally:
        await fresh.destroy()
