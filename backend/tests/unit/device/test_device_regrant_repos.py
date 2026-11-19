# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A regrant keeps the fence on every repository a kept folder holds: the bounded walk does not see
all of them, and the baseline that did walks only folders new here."""

from __future__ import annotations

from pathlib import Path

from prismoid.device.frames import DeviceFrameState, FrameRegistry
from prismoid.device.plants import QUARANTINE
from prismoid.schemas.device import FolderGrant, FrameSpecIn, GrantsIn
from prismoid.schemas.machines import FrameKind
from tests.support.rigs.plants_rig import ctime_ticks as ctime_ticks
from tests.support.rigs.plants_rig import git


def _granted(tmp_path: Path) -> tuple[FrameRegistry, DeviceFrameState, FolderGrant, Path]:
    """A folder with a bare repository only the baseline's walk finds: `find_repos` skips `node_modules`."""
    proj = (tmp_path / "proj").resolve()
    bare = proj / "node_modules" / "pkg" / "mirror.git"
    bare.parent.mkdir(parents=True)
    git("init", "-q", "--bare", str(bare), cwd=proj)
    grant = FolderGrant(path=str(proj), mode="rw")
    registry = FrameRegistry(tmp_path / "home")
    state = registry.create(FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE, grants=[grant]))
    state.baseline_plants()
    assert str(bare) in state.protected_git, "the baseline's own walk found it"
    return registry, state, grant, bare


def test_a_regrant_of_another_folder_keeps_a_repository_only_the_baseline_found(tmp_path: Path) -> None:
    registry, state, grant, bare = _granted(tmp_path)
    more = (tmp_path / "more").resolve()
    more.mkdir()
    registry.regrant(state, GrantsIn(grants=[grant, FolderGrant(path=str(more), mode="rw")]))
    assert str(bare) in state.protected_git, "the folder stayed granted: the fence stays on it"


def test_a_folder_no_longer_read_write_takes_its_repositories_off_the_list(tmp_path: Path) -> None:
    """The fence keeps anything outside the rw grants read-only already."""
    registry, state, grant, bare = _granted(tmp_path)
    registry.regrant(state, GrantsIn(grants=[grant.model_copy(update={"mode": "ro"})]))
    assert str(bare) not in state.protected_git


def _plant(folder: Path) -> Path:
    """What a command left behind: a repository's shape, and a config that would run."""
    plant = folder / "x"
    (plant / "objects").mkdir(parents=True)
    (plant / "refs").mkdir()
    (plant / "HEAD").write_text("ref: refs/heads/m\n")
    (plant / "config").write_text("[core]\n\tfsmonitor = /tmp/evil\n")
    return plant


def test_a_regrant_never_lists_a_plant_as_the_persons_repository(tmp_path: Path) -> None:
    registry, state, grant, _ = _granted(tmp_path)
    plant = _plant(Path(grant.path))
    more = (tmp_path / "more").resolve()
    more.mkdir()
    registry.regrant(state, GrantsIn(grants=[grant, FolderGrant(path=str(more), mode="rw")]))
    assert str(plant) not in state.protected_git
    state.sweep_plants()
    assert not (plant / "config").exists() or not (plant / "HEAD").exists(), "the next walk moves it"


def test_a_re_made_frame_with_new_grants_lists_no_plant_either(tmp_path: Path) -> None:
    registry, _, grant, _ = _granted(tmp_path)
    plant = _plant(Path(grant.path))
    more = (tmp_path / "more").resolve()
    more.mkdir()
    grants = [grant, FolderGrant(path=str(more), mode="rw")]
    again = registry.create(FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE, grants=grants))
    assert str(plant) not in again.protected_git


def test_a_folder_new_in_a_regrant_has_its_repositories_found(tmp_path: Path) -> None:
    registry, state, grant, _ = _granted(tmp_path)
    more = (tmp_path / "more").resolve()
    git("init", "-q", str(more), cwd=tmp_path)
    registry.regrant(state, GrantsIn(grants=[grant, FolderGrant(path=str(more), mode="rw")]))
    assert str(more / ".git") in state.protected_git


def test_what_a_folder_going_away_was_left_is_moved_before_it_goes(tmp_path: Path) -> None:
    """After the revoke no walk reads that folder, so what a command planted there would stand."""
    registry, state, grant, _ = _granted(tmp_path)
    more = (tmp_path / "more").resolve()
    more.mkdir()
    second = FolderGrant(path=str(more), mode="rw")
    registry.regrant(state, GrantsIn(grants=[grant, second]))
    state.baseline_plants()
    plant = _plant(more)
    registry.regrant(state, GrantsIn(grants=[grant]))
    assert not (plant / "HEAD").exists()
    assert list((tmp_path / "home" / QUARANTINE).rglob("HEAD"))
