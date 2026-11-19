# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Prismoid's own Python on this computer: what a frame image bakes in.

A task on the device has no image, and the user's `python3` is theirs: Prismoid's system commands (the
preview runner, the QA precheck, the verifier suites) run on a venv under `<home>/runtime/`, pinned
to the image's versions (`runtime_requirements.txt`), with the house style in its site-packages. An
agent reads it too: it is `python3` in a frame's own fresh folder and `$PRISMOID_PYTHON` in a granted
one (`device/exec.py`). These packages live only there: Prismoid itself depends on none of them.

`ensure_runtime` is idempotent by a stamp: a hash of the requirements and the house source, written
last. The venv is built in a temp folder and renamed into place, so a build killed midway leaves
nothing that looks finished, and a rebuild that fails leaves the runtime that worked. Installs take
wheels only: nothing a package ships runs on this computer at install time.

poppler is the user's to install. `preflight` finds it for the worker, and what it serves degrades
without it, as a preview that is not there does. So do renditions, for now, LibreOffice or not:
see `OFFICE_RUNS_FENCED`.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Final

from prismoid.schemas.device import RUNTIME_DIR, RUNTIME_STAMP, TOOL_DIRS, runtime_python

REQUIREMENTS: Final = Path(__file__).with_name("runtime_requirements.txt")
HOUSE: Final = Path(__file__).parents[1] / "ai" / "frames" / "image" / "house" / "plane_house"
PYTHON: Final = "3.12"  # the image's (`FROM python:3.12-slim-bookworm`)
# LibreOffice is an AppKit app even `--headless`: in the fence it hangs until its timeout, because
# it waits on the window server, and it exits 0 having done nothing without a socket in `/tmp`. The
# fence does not open the window server to what parses an agent's file, so a rendition cannot run
# here, and a harvest must not wait 240 s to learn that. Installing LibreOffice changes none of it.
# Flipping this needs the owner's word first, then, for system commands only: mach lookups of the
# window server, the dock, LaunchServices, cfprefsd, fonts and SecurityServer, with
# `user-preference-read`, `iokit-open`, `file-ioctl` and `ipc-posix*`; Unix-socket bind; a short
# folder for `OSL_SOCKET_PATH` (the socket's name is ~60 of sun_path's 104 bytes), which `profile_for`
# refuses today because it lies outside the home; and the preview runner told a profile and a
# rendition folder in `artifacts/`, its `file://` URL quoted (the home has a space).
OFFICE_RUNS_FENCED: Final = False
_BUILD_TIMEOUT_S: Final = 900.0  # one tool: onnxruntime alone is a large download on a slow line
_BUILD_ENV_KEYS: Final = (
    *("PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "SSL_CERT_FILE", "SSL_CERT_DIR"),
    *("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"),
)
_BUILD_ENV_PREFIXES: Final = ("UV_", "PIP_", "LC_")
# A console script finds the venv's Python beside itself, wherever the venv was renamed to, and a
# shebang could not hold a home with a space. uv's `--relocatable` writes this same header.
_RELOCATABLE: Final = b"""#!/bin/sh
'''exec' "$(dirname -- "$(realpath -- "$0")")"/python "$0" "$@"
' '''
"""


class RuntimeBuildFailed(RuntimeError):
    """The runtime could not be built (offline, a tool that failed). One line, for `prismoid up`."""


def fingerprint() -> str:
    """What a built runtime must match: the requirements and the house source, by content."""
    digest = hashlib.sha256(f"{PYTHON}\0".encode())
    digest.update(REQUIREMENTS.read_bytes())
    for path in sorted(HOUSE.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(f"\0{path.relative_to(HOUSE)}\0".encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _build_env() -> dict[str, str]:
    """The little a package tool needs. A model key has no business in an install."""
    return {k: v for k, v in os.environ.items() if k in _BUILD_ENV_KEYS or k.startswith(_BUILD_ENV_PREFIXES)}


def _run(argv: list[str]) -> str:
    """One tool of the build, its failure a single line."""
    try:
        done = subprocess.run(
            argv,
            env=_build_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_BUILD_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeBuildFailed(f"{Path(argv[0]).name} did not run ({e})") from e
    if done.returncode != 0:
        said = " ".join((done.stderr or done.stdout).split())[-300:]
        raise RuntimeBuildFailed(f"{Path(argv[0]).name} failed: {said}")
    return done.stdout


def _install_house(python: Path) -> None:
    """`plane_house` into the venv's site-packages: never a PYTHONPATH, which `-I` ignores. Compiled
    here, because the fence lets no command write a `.pyc` beside it."""
    site = _run([str(python), "-I", "-c", "import site; print(site.getsitepackages()[0])"]).strip()
    target = Path(site) / HOUSE.name
    shutil.copytree(HOUSE, target, ignore=shutil.ignore_patterns("__pycache__"))
    _run([str(python), "-I", "-m", "compileall", "-q", str(target)])


def _relocate_scripts(venv: Path) -> None:
    """What `uv venv --relocatable` does, for a plain venv: its console scripts name the build path
    (a shebang, or pip's `/bin/sh` header when the path has a space), gone after the rename."""
    python = re.escape(str(venv / "bin" / "python").encode())
    header = re.compile(b"#!%b[^\\n]*\\n|#!/bin/sh\\n'''exec' \"%b[^\"]*\" .*\\n' '''\\n" % (python, python))
    for path in (venv / "bin").iterdir():
        if path.is_file() and not path.is_symlink():
            raw = path.read_bytes()
            if found := header.match(raw):
                path.write_bytes(_RELOCATABLE + raw[found.end() :])


def _build(venv: Path) -> None:
    """A whole venv at `venv`. With uv when it is on PATH (relocatable, because it is renamed into
    place afterwards), else with the Python Prismoid runs under, made relocatable here."""
    python = venv / "bin" / "python"
    wheels = ["--only-binary", ":all:"]
    if uv := shutil.which("uv"):
        _run([uv, "venv", "--no-project", "--relocatable", "--python", PYTHON, str(venv)])
        _run(
            [
                uv,
                "pip",
                "install",
                "--python",
                str(python),
                "--compile-bytecode",
                *wheels,
                "-r",
                str(REQUIREMENTS),
            ]
        )
    else:
        _run([sys.executable, "-m", "venv", str(venv)])
        _run([str(python), "-I", "-m", "pip", "install", "--quiet", *wheels, "-r", str(REQUIREMENTS)])
        _relocate_scripts(venv)
    _install_house(python)


def _write_stamp(root: Path, value: str) -> None:
    tmp = root / f"{RUNTIME_STAMP}.tmp"
    tmp.write_text(value)
    tmp.replace(root / RUNTIME_STAMP)


def _current(root: Path, python: Path, wanted: str) -> bool:
    try:
        if (root / RUNTIME_STAMP).read_text() != wanted:
            return False
        # The interpreter a venv links to can go (`brew upgrade`) with the stamp still right.
        return subprocess.run([python, "-I", "-c", ""], capture_output=True, check=False).returncode == 0
    except OSError:
        return False


def _restore(root: Path, aside: Path, stamp_aside: Path) -> None:
    """Put back what stood there, moved and never rewritten: a full disk cannot refuse it."""
    if aside.exists():
        shutil.rmtree(root / "venv", ignore_errors=True)
        aside.rename(root / "venv")
    if stamp_aside.exists():
        stamp_aside.rename(root / RUNTIME_STAMP)


def _replace(root: Path, wanted: str) -> None:
    """Build beside the runtime, then swap it in, old stamp out first and new stamp in last. The old
    venv and stamp wait in the build folder until the new stamp is written: any failure, or a
    Ctrl-C, on the way puts them back, and what stood there stands."""
    build = Path(tempfile.mkdtemp(prefix=".build-", dir=root))
    aside, stamp_aside = build / "old", build / "old-stamp"
    try:
        _build(build / "venv")
        try:
            if (root / RUNTIME_STAMP).exists():
                (root / RUNTIME_STAMP).rename(stamp_aside)
            if (root / "venv").exists():
                (root / "venv").rename(aside)
            (build / "venv").rename(root / "venv")
            _write_stamp(root, wanted)
        except BaseException:
            _restore(root, aside, stamp_aside)
            raise
    finally:
        shutil.rmtree(build, ignore_errors=True)


def ensure_runtime(home: Path, *, building: Callable[[], None] | None = None) -> Path:
    """The venv's python under `home`, built if it is not there or not this Prismoid's. `building` is
    told when a build starts: it can take minutes. Raises `RuntimeBuildFailed`; what stood there
    before stands. The caller holds the home's lock."""
    root, python = home / RUNTIME_DIR, runtime_python(home)
    wanted = fingerprint()
    if _current(root, python, wanted):
        return python
    if building is not None:
        building()
    try:
        root.mkdir(parents=True, exist_ok=True)
        for leftover in root.glob(".build-*"):  # a build killed midway
            shutil.rmtree(leftover, ignore_errors=True)
        _replace(root, wanted)
    except OSError as e:  # a full disk, a folder that went away
        raise RuntimeBuildFailed(f"could not write the runtime ({e})") from e
    return python


def _found(tool: str) -> bool:
    return shutil.which(tool, path=":".join(TOOL_DIRS)) is not None


def preflight() -> dict[str, str]:
    """What the worker is told about the optional tools (`settings.local.has_*`)."""
    return {
        "PRISMOID_LOCAL_HAS_OFFICE": str(OFFICE_RUNS_FENCED and _found("soffice")).lower(),
        "PRISMOID_LOCAL_HAS_POPPLER": str(_found("pdftoppm")).lower(),
    }


def lanes_off(env: dict[str, str]) -> str | None:
    """The sentence `prismoid up` says when a tool is missing; None when nothing is off."""
    off = []
    if env["PRISMOID_LOCAL_HAS_OFFICE"] != "true":
        off.append("office files show as text, not as rendered pages (LibreOffice cannot run in the fence)")
    if env["PRISMOID_LOCAL_HAS_POPPLER"] != "true":
        off.append("without poppler page images for review are off (`brew install poppler`)")
    return "; ".join(off) or None
