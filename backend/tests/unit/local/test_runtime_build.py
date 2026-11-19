# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Prismoid's own Python: how it is built, without building it.

`_build` is the one seam faked here, or the `uv` it runs; `tests/integration/frames/test_local_runtime.py`
builds the real one."""

from __future__ import annotations

import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from prismoid.local import runtime
from prismoid.local.runtime import REQUIREMENTS, RuntimeBuildFailed, ensure_runtime
from prismoid.schemas.device import runtime_python

DOCKERFILE = Path(runtime.__file__).parents[1] / "ai" / "frames" / "image" / "Dockerfile"


def _image_pins() -> list[str]:
    block = re.search(r"RUN pip install --no-cache-dir \\\n(.*?)\n\n", DOCKERFILE.read_text(), re.DOTALL)
    assert block is not None
    return [line.strip(' \\"') for line in block.group(1).splitlines()]


def test_the_requirements_are_the_images_pins() -> None:
    pins = [ln for ln in REQUIREMENTS.read_text().splitlines() if ln and not ln.startswith("#")]
    assert sorted(pins) == sorted(_image_pins())


def _fake_build(venv: Path) -> None:
    python = venv / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)


def test_a_current_runtime_is_not_built_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    builds: list[Path] = []

    def build(venv: Path) -> None:
        builds.append(venv)
        _fake_build(venv)

    monkeypatch.setattr(runtime, "_build", build)
    python = ensure_runtime(tmp_path)
    assert python == runtime_python(tmp_path)
    assert ensure_runtime(tmp_path) == python
    assert len(builds) == 1


def test_a_changed_house_or_requirements_builds_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    builds: list[Path] = []

    def build(venv: Path) -> None:
        builds.append(venv)
        _fake_build(venv)

    monkeypatch.setattr(runtime, "_build", build)
    ensure_runtime(tmp_path)
    monkeypatch.setattr(runtime, "fingerprint", lambda: "another")
    ensure_runtime(tmp_path)
    assert len(builds) == 2


def test_a_build_killed_midway_is_rebuilt_never_trusted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Killed in the temp folder: nothing at the real path, and the next up starts over."""

    class Killed(BaseException):
        pass

    def killed(venv: Path) -> None:
        _fake_build(venv)
        raise Killed

    monkeypatch.setattr(runtime, "_build", killed)
    with pytest.raises(Killed):
        ensure_runtime(tmp_path)
    assert not runtime_python(tmp_path).exists()
    monkeypatch.setattr(runtime, "_build", _fake_build)
    assert ensure_runtime(tmp_path).exists()
    assert [p.name for p in (tmp_path / "runtime").iterdir() if p.name.startswith(".")] == []


def test_a_venv_with_no_stamp_is_a_partial_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Killed after the rename, before the stamp: it is rebuilt."""
    _fake_build(tmp_path / "runtime" / "venv")
    builds: list[Path] = []

    def build(venv: Path) -> None:
        builds.append(venv)
        _fake_build(venv)

    monkeypatch.setattr(runtime, "_build", build)
    ensure_runtime(tmp_path)
    assert len(builds) == 1


def test_the_stamp_is_written_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "_build", _fake_build)
    real = runtime._write_stamp  # pyright: ignore[reportPrivateUsage]

    def stamp(root: Path, value: str) -> None:
        assert runtime_python(tmp_path).exists(), "the runtime is in place before its stamp"
        real(root, value)

    monkeypatch.setattr(runtime, "_write_stamp", stamp)
    ensure_runtime(tmp_path)
    assert (tmp_path / "runtime" / "stamp").read_text() == runtime.fingerprint()


def test_the_old_stamp_goes_before_the_old_venv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Killed in the swap: no stamp over a venv in motion, for a worker to read as a runtime."""
    monkeypatch.setattr(runtime, "_build", _fake_build)
    ensure_runtime(tmp_path)
    monkeypatch.setattr(runtime, "fingerprint", lambda: "another")
    real = Path.rename

    def rename(self: Path, target: Path) -> Path:
        if self.name == "venv":
            assert not (tmp_path / "runtime" / "stamp").exists()
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)
    ensure_runtime(tmp_path)


def _fail_new_rename(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    real = Path.rename

    def rename(self: Path, target: Path) -> Path:
        if self.name == "venv" and ".build-" in str(self):
            raise error
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)


def _fail_aside_rename(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    real = Path.rename

    def rename(self: Path, target: Path) -> Path:
        if self.name == "venv" and ".build-" not in str(self) and Path(target).name == "old":
            raise error
        return real(self, target)

    monkeypatch.setattr(Path, "rename", rename)


def _fail_new_stamp(monkeypatch: pytest.MonkeyPatch, error: BaseException) -> None:
    def stamp(root: Path, value: str) -> None:
        raise error

    monkeypatch.setattr(runtime, "_write_stamp", stamp)


@pytest.mark.parametrize(
    ("fail", "error", "raised"),
    [
        (_fail_aside_rename, OSError("no"), RuntimeBuildFailed),
        (_fail_new_rename, OSError("no room"), RuntimeBuildFailed),
        (_fail_new_rename, KeyboardInterrupt(), KeyboardInterrupt),
        (_fail_new_stamp, OSError("disk full"), RuntimeBuildFailed),
    ],
    ids=["the aside rename", "the new rename", "Ctrl-C at the new rename", "the new stamp"],
)
def test_every_failed_swap_keeps_the_old_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail: Any,
    error: BaseException,
    raised: type[BaseException],
) -> None:
    """What stood there before stands, with its stamp, and nothing of the build is left."""
    monkeypatch.setattr(runtime, "_build", _fake_build)
    python = ensure_runtime(tmp_path)
    stamp = (tmp_path / "runtime" / "stamp").read_text()
    marker = python.parent / "old-runtime"
    marker.write_text("kept")
    monkeypatch.setattr(runtime, "fingerprint", lambda: "another")
    fail(monkeypatch, error)
    with pytest.raises(raised):
        ensure_runtime(tmp_path)
    assert marker.read_text() == "kept"
    assert (tmp_path / "runtime" / "stamp").read_text() == stamp
    assert [p.name for p in (tmp_path / "runtime").iterdir() if p.name.startswith(".")] == []


@pytest.mark.parametrize(
    "header",
    ["#!{python}\n", "#!/bin/sh\n'''exec' \"{python}3.12\" \"$0\" \"$@\"\n' '''\n"],
    ids=["shebang", "pip's header for a path with a space"],
)
def test_a_plain_venvs_scripts_survive_the_rename(tmp_path: Path, header: str) -> None:
    """A real script, moved like the runtime is, into a folder with a space."""
    built = tmp_path / "build" / "venv"
    (built / "bin").mkdir(parents=True)
    (built / "bin" / "python").symlink_to(sys.executable)
    (built / "bin" / "python3.12").symlink_to(sys.executable)
    tool = built / "bin" / "tool"
    tool.write_text(header.format(python=built / "bin" / "python") + "import sys\nprint(sys.argv[1])\n")
    tool.chmod(0o755)
    runtime._relocate_scripts(built)  # pyright: ignore[reportPrivateUsage]
    moved = tmp_path / "App Support" / "venv"
    moved.parent.mkdir()
    built.rename(moved)
    done = subprocess.run([moved / "bin" / "tool", "hello"], capture_output=True, text=True, check=False)
    assert (done.returncode, done.stdout) == (0, "hello\n"), done.stderr


def test_a_failed_rebuild_keeps_what_works(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "_build", _fake_build)
    python = ensure_runtime(tmp_path)

    def offline(venv: Path) -> None:
        raise RuntimeBuildFailed("no network")

    monkeypatch.setattr(runtime, "_build", offline)
    monkeypatch.setattr(runtime, "fingerprint", lambda: "another")
    with pytest.raises(RuntimeBuildFailed):
        ensure_runtime(tmp_path)
    assert python.exists() and (tmp_path / "runtime" / "stamp").exists()


def _script(folder: Path, name: str, body: str) -> None:
    path = folder / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def test_without_uv_it_falls_back_to_pip_from_prismoids_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str]) -> str:
        calls.append(argv)
        return ""

    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)
    monkeypatch.setattr(runtime, "_run", run)
    monkeypatch.setattr(runtime, "_install_house", lambda python: None)
    monkeypatch.setattr(runtime, "_relocate_scripts", lambda venv: None)
    runtime._build(tmp_path / "venv")  # pyright: ignore[reportPrivateUsage]
    assert calls[0] == [sys.executable, "-m", "venv", str(tmp_path / "venv")]
    python = str(tmp_path / "venv" / "bin" / "python")
    assert calls[1][:5] == [python, "-I", "-m", "pip", "install"]
    assert str(REQUIREMENTS) in calls[1] and ":all:" in calls[1]


def test_a_failed_tool_is_one_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _script(tmp_path, "uv", "echo 'error: no network' >&2; echo 'second line' >&2; exit 2")
    monkeypatch.setenv("PATH", f"{tmp_path}:/usr/bin:/bin")
    with pytest.raises(RuntimeBuildFailed) as failed:
        ensure_runtime(tmp_path / "home")
    assert "\n" not in str(failed.value) and "second line" in str(failed.value)
    assert not runtime_python(tmp_path / "home").exists()


def test_the_hash_covers_the_house_and_the_requirements(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    house = tmp_path / "plane_house"
    (house / "__pycache__").mkdir(parents=True)
    (house / "tokens.py").write_text("A = 1\n")
    reqs = tmp_path / "reqs.txt"
    reqs.write_text("a==1\n")
    monkeypatch.setattr(runtime, "HOUSE", house)
    monkeypatch.setattr(runtime, "REQUIREMENTS", reqs)
    first = runtime.fingerprint()
    (house / "__pycache__" / "x.pyc").write_text("noise")
    assert runtime.fingerprint() == first, "compiled files are not the source"
    (house / "tokens.py").write_text("A = 2\n")
    second = runtime.fingerprint()
    assert second != first
    reqs.write_text("a==2\n")
    assert runtime.fingerprint() != second


def test_preflight_says_which_lanes_are_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "_found", lambda name: name == "pdftoppm")
    monkeypatch.setattr(runtime, "OFFICE_RUNS_FENCED", True)
    env = runtime.preflight()
    assert env == {"PRISMOID_LOCAL_HAS_OFFICE": "false", "PRISMOID_LOCAL_HAS_POPPLER": "true"}
    note = runtime.lanes_off(env)
    assert note is not None and "LibreOffice" in note and "poppler" not in note
    monkeypatch.setattr(runtime, "_found", lambda name: True)
    assert runtime.lanes_off(runtime.preflight()) is None


def test_the_office_note_never_advises_an_install_that_would_not_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runtime, "_found", lambda name: name == "pdftoppm")
    note = runtime.lanes_off(runtime.preflight())
    assert note is not None and "install" not in note and "brew" not in note


def test_poppler_keeps_its_install_note(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime, "_found", lambda name: False)
    note = runtime.lanes_off(runtime.preflight())
    assert note is not None and "`brew install poppler`" in note


def test_renditions_stay_off_until_libreoffice_runs_in_the_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rendition in the fence hangs for its whole timeout: finding soffice must not turn it on."""
    monkeypatch.setattr(runtime, "_found", lambda name: True)
    assert runtime.preflight()["PRISMOID_LOCAL_HAS_OFFICE"] == "false"
