# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The rig for the planted-repository tests, shared by the
`test_device_plants*` files: a git folder made by hand, a granted project folder, and the walk against
real folders without a sandbox. A plain module, not a test file."""

from __future__ import annotations

import functools
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from prismoid.device.frames import DeviceFrameState, FrameRegistry
from prismoid.device.plants import MANIFEST, QUARANTINE, Plants
from prismoid.schemas.device import FolderGrant, FrameSpecIn
from prismoid.schemas.machines import FrameKind
from tests.support.fs_clock import wait_for_ctime_tick


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _then_tick[**P, R](call: Callable[P, R], folder: Path) -> Callable[P, R]:
    @functools.wraps(call)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return call(*args, **kwargs)
        finally:
            wait_for_ctime_tick(folder)

    return wrapped


@pytest.fixture(autouse=True)
def ctime_ticks(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Every walk returns only once the ctime clock has moved past it, so a folder the test changes
    next carries a ctime the walk never recorded (`tests/support/fs_clock.py` says why Linux needs
    it). Imported by each plants test file, which makes it autouse there."""
    base = tmp_path_factory.getbasetemp()
    monkeypatch.setattr(Plants, "baseline", _then_tick(Plants.baseline, base))
    monkeypatch.setattr(Plants, "sweep", _then_tick(Plants.sweep, base))


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """The person's own folder: a repository with a nested one, a link out, a bare repository."""
    folder = tmp_path / "proj"
    folder.mkdir()
    git("init", "-q", cwd=folder)
    (folder / "vendor" / "lib").mkdir(parents=True)
    git("init", "-q", cwd=folder / "vendor" / "lib")
    (tmp_path / "elsewhere").mkdir()
    (folder / "docs").symlink_to(tmp_path / "elsewhere")
    (folder / "inner").symlink_to("vendor")  # a link inside: never a plant
    git("init", "-q", "--bare", str(folder / "mirror.git"), cwd=folder)
    return folder.resolve()


def frame(home: Path, *grants: FolderGrant) -> DeviceFrameState:
    registry = FrameRegistry(home)
    state = registry.create(FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE, grants=list(grants)))
    state.baseline_plants()
    return state


def quarantined(home: Path) -> list[dict[str, Any]]:
    """Every manifest kept. A numbered slot always holds one (a failed move leaves no slot)."""
    base = home / QUARANTINE / "device-t1"
    if not base.is_dir():
        return []
    slots = sorted((p for p in base.iterdir() if p.name.isdigit()), key=lambda p: int(p.name))
    return [json.loads((d / MANIFEST).read_text()) for d in slots]


def is_repository(folder: Path) -> bool:
    """Does git take `folder` for a repository? The owner's own git, nothing above it searched."""
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(folder.parent), "GIT_CONFIG_GLOBAL": "/dev/null"}
    return (
        subprocess.run(
            ["git", "-C", str(folder), "rev-parse", "--git-dir"], capture_output=True, env=env
        ).returncode
        == 0
    )
