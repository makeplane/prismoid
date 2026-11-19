# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""Planted repositories and outward links are moved out of a person's folder; the person's own
never are. The walk against real folders, without a sandbox:
`tests/integration/device/test_device_services.py` plants through fenced commands. The rig is
`tests/support/rigs/plants_rig.py`."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import sys
import threading
from pathlib import Path
from typing import Any

import httpx

from prismoid.device import plants
from prismoid.device.frames import FrameRegistry
from prismoid.device.helper import KEY_HEADER
from prismoid.device.plants import QUARANTINE, Plants
from prismoid.runner.device_door import create_app
from prismoid.schemas.device import FolderGrant, FrameSpecIn, GrantsIn
from prismoid.schemas.machines import FrameKind
from tests.support.rigs.plants_rig import ctime_ticks as ctime_ticks
from tests.support.rigs.plants_rig import frame, git, is_repository, quarantined
from tests.support.rigs.plants_rig import proj as proj


def test_the_persons_own_repositories_and_links_are_never_moved(tmp_path: Path, proj: Path) -> None:
    state = frame(tmp_path / "home", FolderGrant(path=str(proj), mode="rw"))
    state.sweep_plants()
    state.sweep_plants()
    assert quarantined(tmp_path / "home") == [] and list(state.denials) == []
    assert (proj / ".git").is_dir() and (proj / "vendor" / "lib" / ".git").is_dir()
    assert (proj / "docs").is_symlink() and (proj / "mirror.git" / "HEAD").is_file()


def test_a_git_folder_moved_into_a_subfolder_is_quarantined(tmp_path: Path, proj: Path) -> None:
    """B2's first probe: a repository made in a Prismoid root and moved in whole."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw", name="/work"))
    made = tmp_path / "scratch" / "planted"
    made.mkdir(parents=True)
    git("init", "-q", cwd=made)
    (made / "README").write_text("hi")
    (proj / "src").mkdir()
    shutil.move(str(made), proj / "src" / "planted")
    state.sweep_plants()
    assert not (proj / "src" / "planted" / ".git").exists()
    assert (proj / "src" / "planted" / "README").read_text() == "hi"  # only the repository goes
    [kept] = quarantined(home)
    assert kept["virtual_path"] == "/work/src/planted/.git" and kept["kind"] == "git"
    assert str(proj) not in json.dumps(kept)  # the manifest names the virtual path only
    moved = home / QUARANTINE / "device-t1" / "1" / "item" / ".git"
    assert (moved / "HEAD").is_file()
    assert stat.S_IMODE(moved.parent.parent.stat().st_mode) == 0o700
    assert [d["host"] for d in state.denials] == ["/work/src/planted/.git"]
    assert state.notices[-1].startswith("prismoid: moved /work/src/planted/.git out of the folder")


def test_a_link_out_of_the_root_is_quarantined(tmp_path: Path, proj: Path) -> None:
    """B2's second probe, and a link into Prismoid's own home; a link inside the grants stays."""
    home = tmp_path / "home"
    other = tmp_path / "other"
    other.mkdir()
    state = frame(home, FolderGrant(path=str(proj), mode="rw"), FolderGrant(path=str(other), mode="rw"))
    secret = tmp_path / "secret"
    secret.mkdir()
    (proj / "out").symlink_to(secret)
    (proj / "prismoid").symlink_to(home / "tasks")
    (proj / "dangling").symlink_to(tmp_path / "nothing-yet")
    (proj / "sibling").symlink_to(other)  # another of the frame's own rw grants: inside
    (proj / "here").symlink_to("vendor/lib")
    state.sweep_plants()
    assert sorted(k["virtual_path"] for k in quarantined(home)) == [
        "/work/dangling",
        "/work/out",
        "/work/prismoid",
    ]
    assert secret.is_dir()  # what the link named is never touched, only the link moves
    assert (proj / "sibling").is_symlink() and (proj / "here").is_symlink()
    moved = next((home / QUARANTINE / "device-t1").glob("*/item/out"))
    assert moved.is_symlink() and os.readlink(moved) == str(secret)


def test_a_retargeted_link_is_new(tmp_path: Path, proj: Path) -> None:
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    (proj / "docs").unlink()
    (proj / "docs").symlink_to(Path.home())
    state.sweep_plants()
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/docs"]


def test_a_bare_repository_shape_is_quarantined(tmp_path: Path, proj: Path) -> None:
    """B2's third probe: HEAD, objects/ and refs/ in a folder of any name (even lower case). Its
    shape files go, one by one, and the folder stays: it may have been the person's before."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    git("init", "-q", "--bare", str(proj / "deep" / "store"), cwd=proj)
    hand = proj / "hand"
    (hand / "objects").mkdir(parents=True)
    (hand / "refs").mkdir()
    (hand / "head").write_text("ref: refs/heads/main\n")
    state.sweep_plants()
    assert sorted(k["virtual_path"] for k in quarantined(home)) == ["/work/deep/store", "/work/hand"]
    assert {k["kind"] for k in quarantined(home)} == {"bare"}
    assert not is_repository(proj / "deep" / "store") and not is_repository(hand)
    assert (proj / "mirror.git").is_dir() and is_repository(proj / "mirror.git")  # the person's own


def test_a_protected_repository_is_never_moved(tmp_path: Path, proj: Path) -> None:
    """A protected entry the baseline saw stays, whatever else changes."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    assert str(proj / ".git") in state.protected_git
    (proj / "new.txt").write_text("x")
    state.sweep_plants()
    assert (proj / ".git").is_dir() and quarantined(home) == []


def test_a_venvs_interpreter_link_is_not_a_plant(tmp_path: Path, proj: Path) -> None:
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    venv = proj / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (venv / "bin" / "python").symlink_to(os.path.realpath(sys.executable))
    (venv / "bin" / "python3").symlink_to("python")
    (venv / "bin" / "notes").symlink_to(tmp_path / "secret-notes")  # not an interpreter
    rc = tmp_path / "zshrc"
    rc.write_text("")
    (venv / "bin" / "python3.99").symlink_to(rc)  # an interpreter's name, no executable: a plant
    state.sweep_plants()
    assert sorted(k["virtual_path"] for k in quarantined(home)) == [
        "/work/.venv/bin/notes",
        "/work/.venv/bin/python3.99",
    ]
    assert (venv / "bin" / "python").is_symlink()


def test_a_plant_made_while_the_helper_was_down_is_found_at_its_next_walk(tmp_path: Path, proj: Path) -> None:
    """The inventory persists beside the frame: a new helper life compares with it, never
    re-baselines."""
    home = tmp_path / "home"
    frame(home, FolderGrant(path=str(proj), mode="rw"))
    (proj / "away").symlink_to(Path.home())
    again = FrameRegistry(home).frames["device-t1"]  # the next helper life adopts the frame
    again.sweep_plants()
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/away"]
    assert (proj / "docs").is_symlink()  # still the person's
    assert stat.S_IMODE((again.base / "plants.json").stat().st_mode) == 0o600


def test_a_regrant_baselines_the_new_folder_only(tmp_path: Path, proj: Path) -> None:
    home = tmp_path / "home"
    registry = FrameRegistry(home)
    state = registry.create(FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE))
    state.baseline_plants()
    registry.regrant(state, GrantsIn(grants=[FolderGrant(path=str(proj), mode="rw")]))
    state.sweep_plants()
    assert quarantined(home) == []  # what the folder held when it was granted is the person's


def test_a_git_link_moved_in_is_quarantined(tmp_path: Path, proj: Path) -> None:
    """C1: a folder arrives holding a `.git` that is a link: the entry's own type decides."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    made = tmp_path / "scratch" / "sub"
    made.mkdir(parents=True)
    git("init", "-q", "--bare", str(proj / "w-store"), cwd=proj)
    (made / ".git").symlink_to("../w-store")
    shutil.move(str(made), proj / "sub")
    assert is_repository(proj / "sub")
    state.sweep_plants()
    assert not os.path.lexists(proj / "sub" / ".git")
    assert "/work/sub/.git" in [k["virtual_path"] for k in quarantined(home)]
    assert (home / QUARANTINE / "device-t1" / "1").is_dir()
    assert all("could not be moved" not in d["reason"] for d in state.denials)


async def test_the_helper_baselines_at_create_and_sweeps_at_the_frames_end(
    tmp_path: Path, proj: Path
) -> None:
    """Through the helper's routes: a frame made over the person's folder keeps it as it is; what
    arrives after is moved when the frame is destroyed, and the quarantine outlives the frame."""
    home = tmp_path / "home"
    app = create_app(home, device_name=lambda: "Test Mac")
    key = (home / "helper.key").read_text().strip()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://helper", headers={KEY_HEADER: key}
    ) as c:
        spec = {
            "task_id": "t1",
            "kind": "machine",
            "grants": [{"path": str(proj), "mode": "rw", "name": "/work"}],
        }
        assert (await c.post("/frames", json=spec)).status_code == 200
        (proj / "away").symlink_to(Path.home())
        assert (await c.delete("/frames/device-t1")).status_code == 204
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/away"]
    assert (proj / "docs").is_symlink() and (proj / ".git").is_dir()


async def test_a_slow_walk_never_holds_a_commands_result(tmp_path: Path, proj: Path) -> None:
    """A walk past its wait carries on in its thread, a second never starts on top of it, and what
    it finds is told with a later result."""
    home = tmp_path / "home"
    state = frame(home, FolderGrant(path=str(proj), mode="rw"))
    (proj / "away").symlink_to(Path.home())
    release, walks = threading.Event(), [0]
    real = state.plants.sweep

    def slow(*args: Any, **kw: Any) -> list[plants.Planted]:
        walks[0] += 1
        release.wait(10)
        return real(*args, **kw)

    state.plants.sweep = slow  # pyright: ignore[reportAttributeAccessIssue]
    await state.sweep_within(0.01)
    await state.sweep_within(0.01)
    assert walks == [1] and quarantined(home) == [] and not state.notices
    release.set()
    assert state.walk is not None
    await state.walk
    assert [k["virtual_path"] for k in quarantined(home)] == ["/work/away"] and len(state.notices) == 1


def _client(app: Any, home: Path) -> httpx.AsyncClient:
    key = (home / "helper.key").read_text().strip()
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://helper", headers={KEY_HEADER: key}
    )


async def test_containers_stop_before_the_last_walk(tmp_path: Path, proj: Path) -> None:
    """I7: a container has no Seatbelt and writes the grant until it stops: the walk comes after,
    at a sleep and at the frame's end."""
    home = tmp_path / "home"
    app = create_app(home, device_name=lambda: "Test Mac")
    order: list[str] = []
    real = app.state.dockers.stop

    async def stop(ref: str) -> None:
        await real(ref)
        (proj / f"late{len(order)}").symlink_to(Path.home())  # what the container did as it stopped
        order.append("stop")

    app.state.dockers.stop = stop
    spec = {
        "task_id": "t1",
        "kind": "machine",
        "grants": [{"path": str(proj), "mode": "rw", "name": "/work"}],
    }
    async with _client(app, home) as c:
        assert (await c.post("/frames", json=spec)).status_code == 200
        assert (await c.post("/frames/device-t1/stop-services")).status_code == 204
        assert [k["virtual_path"] for k in quarantined(home)] == ["/work/late0"]
        assert (await c.delete("/frames/device-t1")).status_code == 204
    assert sorted(k["virtual_path"] for k in quarantined(home)) == ["/work/late0", "/work/late1"]


async def test_destroy_waits_for_a_running_walk_before_its_last_one(tmp_path: Path, proj: Path) -> None:
    """Queued (b): the walk past its wait must not stand in for the final one."""
    home = tmp_path / "home"
    app = create_app(home, device_name=lambda: "Test Mac")
    spec = {
        "task_id": "t1",
        "kind": "machine",
        "grants": [{"path": str(proj), "mode": "rw", "name": "/work"}],
    }
    async with _client(app, home) as c:
        assert (await c.post("/frames", json=spec)).status_code == 200
        state = app.state.registry.frames["device-t1"]
        release, walks = threading.Event(), [0]
        real = state.plants.sweep

        def slow(*args: Any, **kw: Any) -> list[plants.Planted]:
            walks[0] += 1
            if walks[0] == 1:
                release.wait(10)
            return real(*args, **kw)

        state.plants.sweep = slow
        await state.sweep_within(0.01)  # still walking, and past the folder it will never see again
        (proj / "away").symlink_to(Path.home())
        asyncio.get_running_loop().call_later(2.3, release.set)  # past the 2 s a walk may hold anyone
        assert (await c.delete("/frames/device-t1")).status_code == 204
    assert walks == [2] and [k["virtual_path"] for k in quarantined(home)] == ["/work/away"]


async def test_the_quarantine_goes_with_the_task_only(tmp_path: Path, proj: Path) -> None:
    """Queued (c): a frame's end keeps it; a deleted task's `forget` removes it."""
    home = tmp_path / "home"
    app = create_app(home, device_name=lambda: "Test Mac")
    spec = {
        "task_id": "t1",
        "kind": "machine",
        "grants": [{"path": str(proj), "mode": "rw", "name": "/work"}],
    }
    async with _client(app, home) as c:
        assert (await c.post("/frames", json=spec)).status_code == 200
        (proj / "away").symlink_to(Path.home())
        assert (await c.delete("/frames/device-t1")).status_code == 204
        assert (home / QUARANTINE / "device-t1").is_dir()
        (home / QUARANTINE / "keep-me").mkdir()
        assert (await c.delete("/frames/device-t1/quarantine")).status_code == 204
        assert not (home / QUARANTINE / "device-t1").exists()
        assert (await c.delete("/frames/device-t1/quarantine")).status_code == 204  # idempotent
        assert (await c.delete("/frames/../quarantine")).status_code in (404, 422)
        assert (await c.delete("/frames/%2E%2E/quarantine")).status_code in (404, 422)
    assert (home / QUARANTINE / "keep-me").is_dir() and (home / "helper.key").exists()


def test_plants_knows_nothing_of_a_root_it_was_never_given(tmp_path: Path) -> None:
    store = Plants(tmp_path / "plants.json", tmp_path / "home", "device-t1")
    root = tmp_path / "r"
    root.mkdir()
    (root / "l").symlink_to(Path.home())
    assert store.sweep([("/work", str(root))], ()) == []  # a root never baselined is baselined
    assert (root / "l").is_symlink()


def test_a_grant_stays_the_folder_it_was_through_a_swap_and_a_new_helper(tmp_path: Path, proj: Path) -> None:
    """A link in the folder's place moves no write access: the frame holds the real path it took,
    through a regrant of another folder and into the next helper life."""
    home, documents, more = tmp_path / "home", tmp_path / "Documents", tmp_path / "more"
    documents.mkdir()
    more.mkdir()
    real = os.path.realpath(proj)
    grant = FolderGrant(path=str(proj), mode="rw", name="/work")
    registry = FrameRegistry(home)
    state = registry.create(FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE, grants=[grant]))
    shutil.rmtree(proj)
    proj.symlink_to(documents)
    assert state.granted("rw") == [real] and state.paths.work == real
    other = FolderGrant(path=str(more), mode="rw", name="/grants/more")
    registry.regrant(state, GrantsIn(grants=[grant, other]))
    assert state.granted("rw") == [real, os.path.realpath(more)]
    again = FrameRegistry(home).frames["device-t1"]
    assert again.granted("rw")[0] == real and again.paths.work == real
    recreated = registry.create(FrameSpecIn(task_id="t1", kind=FrameKind.MACHINE, grants=[grant]))
    assert recreated.granted("rw") == [real]
