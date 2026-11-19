# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a person does in a granted folder while a task runs: a walk cannot
tell who wrote what, so a link they remade the same stays, a folder they closed stays closed, and a
repository they started goes to quarantine and outlives the task. The rig is `tests/support/rigs/plants_rig.py`."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

from prismoid.device.frames import FrameRegistry
from prismoid.device.plants import QUARANTINE
from prismoid.schemas.device import FolderGrant
from tests.support.rigs.plants_rig import ctime_ticks as ctime_ticks
from tests.support.rigs.plants_rig import frame, git, quarantined
from tests.support.rigs.plants_rig import proj as proj


def _grant(proj: Path) -> FolderGrant:
    return FolderGrant(path=str(proj), mode="rw", name="/work")


def test_a_link_remade_with_the_same_target_is_the_persons(tmp_path: Path, proj: Path) -> None:
    home = tmp_path / "home"
    state = frame(home, _grant(proj))
    subprocess.run(["ln", "-sfn", str(tmp_path / "elsewhere"), str(proj / "docs")], check=True)
    state.sweep_plants()
    assert quarantined(home) == [] and (proj / "docs").is_symlink()


def test_a_link_remade_with_another_target_is_a_plant(tmp_path: Path, proj: Path) -> None:
    home = tmp_path / "home"
    state = frame(home, _grant(proj))
    (tmp_path / "other").mkdir()
    subprocess.run(["ln", "-sfn", str(tmp_path / "other"), str(proj / "docs")], check=True)
    state.sweep_plants()
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/docs"]


def test_a_new_link_at_a_new_path_to_a_known_target_is_a_plant(tmp_path: Path, proj: Path) -> None:
    home = tmp_path / "home"
    state = frame(home, _grant(proj))
    (proj / "again").symlink_to(tmp_path / "elsewhere")
    state.sweep_plants()
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/again"]


def test_a_person_s_link_survives_a_rename_of_its_folder(tmp_path: Path, proj: Path) -> None:
    home = tmp_path / "home"
    (proj / "sub").mkdir()
    (proj / "sub" / "out").symlink_to(tmp_path / "elsewhere")
    state = frame(home, _grant(proj))
    (proj / "sub").rename(proj / "renamed")
    state.sweep_plants()
    assert quarantined(home) == [] and (proj / "renamed" / "out").is_symlink()
    subprocess.run(["ln", "-sfn", str(tmp_path / "elsewhere"), str(proj / "renamed" / "out")], check=True)
    state.sweep_plants()
    assert quarantined(home) == [], "and is known by its new path from then on"


def test_a_folder_the_person_closed_stays_closed(tmp_path: Path, proj: Path) -> None:
    home = tmp_path / "home"
    (proj / "private").mkdir()
    (proj / "private" / "secret").write_text("theirs")
    (proj / "private").chmod(0)
    try:
        state = frame(home, _grant(proj))
        assert stat.S_IMODE((proj / "private").stat().st_mode) == 0, "the baseline leaves it"
        state.sweep_plants()
        state.sweep_plants()
        assert stat.S_IMODE((proj / "private").stat().st_mode) == 0, "and so does every walk"
        assert quarantined(home) == []
    finally:
        (proj / "private").chmod(0o755)
    assert (proj / "private" / "secret").read_text() == "theirs"


def test_a_folder_an_agent_closes_is_still_opened_by_the_walk(tmp_path: Path, proj: Path) -> None:
    home = tmp_path / "home"
    state = frame(home, _grant(proj))
    (proj / "hidden").mkdir()
    (proj / "hidden" / "x").mkdir()
    (proj / "hidden" / "x" / "HEAD").write_text("ref")
    (proj / "hidden" / "x" / "objects").mkdir()
    (proj / "hidden" / "x" / "refs").mkdir()
    (proj / "hidden").chmod(0)
    try:
        state.sweep_plants()
        assert [k["kind"] for k in quarantined(home)] == ["bare"]
    finally:
        (proj / "hidden").chmod(0o755)


def _forgotten(home: Path, state_ref: str = "device-t1") -> Path:
    assert FrameRegistry(home).forget_quarantine(state_ref)
    return home / QUARANTINE / state_ref


def test_a_repository_in_quarantine_outlives_its_task(tmp_path: Path, proj: Path) -> None:
    """The person's `git init` during a task is moved like a plant, commits and all."""
    home = tmp_path / "home"
    state = frame(home, _grant(proj))
    (proj / "mine").mkdir()
    git("init", "-q", cwd=proj / "mine")
    (proj / "out").symlink_to(tmp_path / "elsewhere")
    state.sweep_plants()
    assert sorted(k["kind"] for k in quarantined(home)) == ["git", "link"]
    kept = _forgotten(home)
    assert [k["kind"] for k in quarantined(home)] == ["git"]
    assert list(kept.rglob(".git"))


def test_a_bare_repository_in_quarantine_outlives_it_too(tmp_path: Path, proj: Path) -> None:
    home = tmp_path / "home"
    state = frame(home, _grant(proj))
    git("init", "-q", "--bare", str(proj / "new.git"), cwd=proj)
    state.sweep_plants()
    assert [k["kind"] for k in quarantined(home)] == ["bare"]
    _forgotten(home)
    assert [k["kind"] for k in quarantined(home)] == ["bare"]


def test_a_folder_holding_a_repository_deep_inside_is_kept(tmp_path: Path, proj: Path) -> None:
    """A folder moved whole (an unlistable one) can hold one at any depth."""
    home = tmp_path / "home"
    slot = home / QUARANTINE / "device-t1" / "1"
    (slot / "item" / "sealed" / "deep").mkdir(parents=True)
    (slot / "manifest.json").write_text('{"kind": "opaque"}')
    git("init", "-q", cwd=slot / "item" / "sealed" / "deep")
    _forgotten(home)
    assert (slot / "item" / "sealed" / "deep" / ".git").is_dir()


def test_a_folder_that_cannot_be_read_is_kept_rather_than_guessed_at(tmp_path: Path) -> None:
    home = tmp_path / "home"
    slot = home / QUARANTINE / "device-t1" / "1"
    (slot / "item" / "sealed").mkdir(parents=True)
    (slot / "manifest.json").write_text('{"kind": "opaque"}')
    (slot / "item" / "sealed").chmod(0)
    try:
        _forgotten(home)
        assert (slot / "item" / "sealed").is_dir()
    finally:
        (slot / "item" / "sealed").chmod(0o755)


def test_what_is_no_repository_goes_with_the_task_and_nothing_stays_behind(
    tmp_path: Path, proj: Path
) -> None:
    home = tmp_path / "home"
    state = frame(home, _grant(proj))
    (proj / "out").symlink_to(tmp_path / "elsewhere")
    state.sweep_plants()
    assert quarantined(home)
    assert not _forgotten(home).exists()


def test_a_folder_the_person_can_list_but_not_enter_stays_as_it_was(tmp_path: Path, proj: Path) -> None:
    home = tmp_path / "home"
    (proj / "listed").mkdir()
    (proj / "listed").chmod(0o400)
    try:
        state = frame(home, _grant(proj))
        state.sweep_plants()
        state.sweep_plants()
        assert stat.S_IMODE((proj / "listed").stat().st_mode) == 0o400
        assert quarantined(home) == []
    finally:
        (proj / "listed").chmod(0o755)
