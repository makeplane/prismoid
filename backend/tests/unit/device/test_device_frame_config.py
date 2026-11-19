# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What the helper writes in a folder a command can reach: it is unsandboxed, so a link of a command's
there must never carry the write away."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from prismoid.device.frames import FrameRegistry
from prismoid.schemas.device import FolderGrant, FrameSpecIn
from prismoid.schemas.machines import FrameKind


def _spec(proj: Path) -> FrameSpecIn:
    grant = FolderGrant(path=str(proj), mode="rw")
    return FrameSpecIn(task_id="t2", kind=FrameKind.MACHINE, grants=[grant], docker=True)


def _proj(tmp_path: Path) -> Path:
    proj = (tmp_path / "proj").resolve()
    proj.mkdir()
    return proj


def test_the_docker_config_is_written_private_and_kept(tmp_path: Path) -> None:
    registry = FrameRegistry(tmp_path / "home")
    state = registry.create(_spec(_proj(tmp_path)))
    config = state.docker_config / "config.json"
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert "auths" in json.loads(config.read_text())
    config.write_text("{}")
    registry.create(state.spec)
    assert config.read_text() == "{}", "what the frame keeps there stays"


def test_a_dangling_link_is_not_written_through(tmp_path: Path) -> None:
    registry = FrameRegistry(tmp_path / "home")
    state = registry.create(_spec(_proj(tmp_path)))
    config = state.docker_config / "config.json"
    config.unlink()
    victim = tmp_path / "outside-the-fence"
    config.symlink_to(victim)
    registry.create(state.spec)
    assert not victim.exists()
    assert not config.is_symlink() and "auths" in json.loads(config.read_text())


def test_a_link_to_a_file_is_taken_away_and_the_file_untouched(tmp_path: Path) -> None:
    registry = FrameRegistry(tmp_path / "home")
    state = registry.create(_spec(_proj(tmp_path)))
    config = state.docker_config / "config.json"
    config.unlink()
    victim = tmp_path / "theirs"
    victim.write_text("theirs")
    os.symlink(victim, config)
    registry.create(state.spec)
    assert victim.read_text() == "theirs"
    assert not config.is_symlink()
