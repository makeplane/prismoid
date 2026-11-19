# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What a command plants in a granted folder: it is moved
out, a git link through the fence never reaches the owner's git, and a container that plants as it
stops is caught. The rig is `tests/support/rigs/device_services_rig.py`."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import pytest

from prismoid.device import docker_proxy, frames
from prismoid.device.plants import MANIFEST, QUARANTINE
from tests.support.markers import darwin_only
from tests.support.rigs.device_services_rig import IMAGE, USER_HOME, Helper, free_port, services_docker, until
from tests.support.rigs.device_services_rig import daemon as daemon
from tests.support.rigs.device_services_rig import folder as folder
from tests.support.rigs.device_services_rig import helper as helper

pytestmark = darwin_only


def _quarantined(home: Path, ref: str) -> list[str]:
    base = home / QUARANTINE / ref
    slots = [d for d in base.iterdir() if d.name.isdigit()] if base.is_dir() else []
    return sorted(json.loads((d / MANIFEST).read_text())["virtual_path"] for d in slots)


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


async def test_plants_from_fenced_commands_are_quarantined(helper: Helper, folder: Path) -> None:
    """B2's three probes, through the real fence: a repository made in $TMPDIR and moved into a
    subfolder, a link out of the root, a bare repository's shape. The person's own nested
    repository and link, there before the grant, stay."""
    _git_init(folder / "theirs")
    (folder / "their-link").symlink_to(USER_HOME / "Documents")
    ref = await helper.frame("tp", grants=[{"path": str(folder), "mode": "rw", "name": "/work"}])

    out = await helper.run(
        ref, 'mkdir "$TMPDIR/p" && git -C "$TMPDIR/p" init -q && mv "$TMPDIR/p" ./sub && echo moved'
    )
    assert "moved" in out["stdout"], out
    assert "prismoid: moved /work/sub/.git out of the folder" in out["stderr"]
    assert not (folder / "sub" / ".git").exists()

    out = await helper.run(ref, 'ln -s "$HOME/.ssh" ./keys && echo linked')
    assert "linked" in out["stdout"], out
    assert "/work/keys" in out["stderr"] and not (folder / "keys").is_symlink()

    out = await helper.run(ref, "git init -q --bare ./store && echo bare")
    assert "bare" in out["stdout"], out
    assert "/work/store" in out["stderr"] and not (folder / "store" / "HEAD").exists()

    assert _quarantined(helper.home, ref) == ["/work/keys", "/work/store", "/work/sub/.git"]
    assert (folder / "theirs" / ".git").is_dir() and (folder / "their-link").is_symlink()
    hosts = [d["host"] for d in helper.state(ref).denials]
    assert hosts == ["/work/sub/.git", "/work/keys", "/work/store"]
    assert str(folder) not in json.dumps(list(helper.state(ref).denials))


def _owner_git_status(folder: Path) -> None:
    """The owner's own unfenced `git status`, as the review's escape needs it."""
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(folder.parent)}
    subprocess.run(["git", "-C", str(folder), "status"], capture_output=True, env=env, timeout=30)


async def test_a_git_link_through_the_fence_never_reaches_the_owners_git(
    helper: Helper, folder: Path
) -> None:
    """C1's probe: a folder holding a `.git` link, moved in from $TMPDIR."""
    ref = await helper.frame("t1", grants=[{"path": str(folder), "mode": "rw", "name": "/work"}])
    out = await helper.run(
        ref,
        'git init -q --bare ./store; mkdir "$TMPDIR/s" && ln -s ../store "$TMPDIR/s/.git" '
        '&& mv "$TMPDIR/s" ./sub && echo moved',
    )
    assert "moved" in out["stdout"], out
    assert not (folder / "sub" / ".git").is_symlink()
    assert "/work/sub/.git" in _quarantined(helper.home, ref)


async def test_a_repository_of_plain_files_at_the_root_never_runs_for_the_owner(
    helper: Helper, folder: Path, tmp_path: Path
) -> None:
    """C2's probe: at the root of a grant with no `.git`, nothing but plain file names. The owner's
    unsandboxed `git status` must not run the agent's fsmonitor."""
    marker = tmp_path / "ran"
    (folder / "ev.sh").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (folder / "ev.sh").chmod(0o755)
    ref = await helper.frame("t2", grants=[{"path": str(folder), "mode": "rw", "name": "/work"}])
    config = "[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tworktree = .\n\tfsmonitor = ./ev.sh\n"
    out = await helper.run(
        ref, f"mkdir objects refs; echo 'ref: refs/heads/main' > HEAD; printf '{config}' > config; echo made"
    )
    assert "made" in out["stdout"], out
    await asyncio.to_thread(_owner_git_status, folder)
    assert not marker.exists()
    assert not (folder / "HEAD").exists() and (folder / "ev.sh").exists()


async def test_a_repository_of_executable_files_never_runs_for_the_owner(
    helper: Helper, folder: Path, tmp_path: Path
) -> None:
    """The re-review's probe, through the fence: `objects` and `refs` as regular +x files. git
    takes them (`access(X_OK)`); the owner's unsandboxed `git status` must not run the fsmonitor."""
    marker = tmp_path / "ran"
    ref = await helper.frame("t8", grants=[{"path": str(folder), "mode": "rw", "name": "/work"}])
    (folder / "sub").mkdir()
    (folder / "sub" / "ev.sh").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (folder / "sub" / "ev.sh").chmod(0o755)
    config = "[core]\n\trepositoryformatversion = 0\n\tbare = false\n\tworktree = .\n\tfsmonitor = ./ev.sh\n"
    out = await helper.run(
        ref,
        "touch sub/objects sub/refs && chmod +x sub/objects sub/refs "
        f"&& echo 'ref: refs/heads/main' > sub/HEAD && printf '{config}' > sub/config && echo made",
    )
    assert "made" in out["stdout"], out
    assert "/work/sub" in out["stderr"]
    await asyncio.to_thread(_owner_git_status, folder / "sub")
    assert not marker.exists()
    assert _quarantined(helper.home, ref) == ["/work/sub"]


async def test_a_shape_never_moves_the_owners_protected_repository(helper: Helper, folder: Path) -> None:
    """C3's probe: `sub/HEAD`, `sub/objects/`, `sub/refs/` around the owner's `sub/.git`, then a
    hostile repository moved to `sub` (the fence refuses that: `sub` holds a protected path)."""
    (folder / "sub").mkdir()
    _git_init(folder / "sub")
    inode = (folder / "sub" / ".git").stat().st_ino
    ref = await helper.frame("t3", grants=[{"path": str(folder), "mode": "rw", "name": "/work"}])
    await helper.run(ref, "mkdir sub/objects sub/refs; echo 'ref: refs/heads/main' > sub/HEAD")
    assert (folder / "sub" / ".git").stat().st_ino == inode
    assert not (folder / "sub" / "HEAD").exists()
    out = await helper.run(
        ref, 'mkdir "$TMPDIR/e" && git -C "$TMPDIR/e" init -q; mv "$TMPDIR/e" sub; echo $?'
    )
    assert (folder / "sub" / ".git").stat().st_ino == inode, out


async def test_an_unreadable_folder_hides_nothing_through_the_fence(helper: Helper, folder: Path) -> None:
    """C4's probe: a repository moved under a folder, then `chmod 311` on it."""
    ref = await helper.frame("t4", grants=[{"path": str(folder), "mode": "rw", "name": "/work"}])
    await helper.run(
        ref,
        'mkdir -p sub/x "$TMPDIR/r" && git -C "$TMPDIR/r" init -q && mv "$TMPDIR/r" sub/x/y && chmod 311 sub',
    )
    assert _quarantined(helper.home, ref) == ["/work/sub/x/y/.git"]


@pytest.mark.parametrize("full", [16, 0], ids=["full-rules", "pinned"])
async def test_a_nested_repository_cannot_be_carried_off_and_brought_back(
    helper: Helper, folder: Path, full: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I1's probe: under `node_modules`, where the repository walk never looks; on either tier."""
    monkeypatch.setattr(frames, "FULL_GIT", full)
    pkg = folder / "node_modules" / "pkg"
    pkg.mkdir(parents=True)
    _git_init(pkg)
    ref = await helper.frame("t5", grants=[{"path": str(folder), "mode": "rw", "name": "/work"}])
    assert (str(pkg / ".git") in helper.state(ref).pinned_git) == (full == 0)
    out = await helper.run(ref, 'mv node_modules/pkg "$TMPDIR/pk"; echo "rc=$?"')
    assert "rc=0" not in out["stdout"], out
    assert (pkg / ".git").is_dir()


async def test_a_container_that_plants_as_it_stops_is_caught(
    helper: Helper, folder: Path, daemon: None
) -> None:
    """I7: no Seatbelt in a container, and it writes the grant up to its last moment."""
    ref = await helper.frame("t6", grants=[{"path": str(folder), "mode": "rw", "name": "/work"}], docker=True)
    script = "trap 'mkdir -p /w/late/.git; touch /w/late/.git/HEAD; exit 0' TERM; sleep 300 & wait"
    try:
        out = await helper.run(ref, f'docker run -d -v "$PWD:/w" {IMAGE} sh -c "{script}"', limit=240)
        assert out["exit_code"] == 0, out
        async with helper.client() as c:
            assert (await c.post(f"/frames/{ref}/stop-services")).status_code == 204
        assert not (folder / "late" / ".git").exists()
        assert "/work/late/.git" in _quarantined(helper.home, ref)
    finally:
        ids = services_docker(
            "ps", "-aq", "--filter", f"label={docker_proxy.FRAME_LABEL}={ref}", check=False
        ).stdout.split()
        if ids:
            services_docker("rm", "-f", *ids, check=False)


async def test_a_container_published_on_every_address_stays_on_loopback(
    helper: Helper, folder: Path, daemon: None
) -> None:
    """Queued (a): `-p Y:80` (the wildcard) lands on 127.0.0.1."""
    ref = await helper.frame("t7", grants=[{"path": str(folder), "mode": "rw", "name": "/work"}], docker=True)
    port = free_port()
    try:
        out = await helper.run(ref, f"docker run -d -p {port}:80 {IMAGE} sleep 300", limit=240)
        assert out["exit_code"] == 0, out
        bound = json.loads(services_docker("inspect", out["stdout"].strip().splitlines()[-1]).stdout)[0]
        assert [b["HostIp"] for b in bound["HostConfig"]["PortBindings"]["80/tcp"]] == ["127.0.0.1"]
        assert await helper.app.state.dockers.published(ref) == {("127.0.0.1", port)}
        out = await helper.run(ref, f"docker run -d -p 192.168.1.5:{free_port()}:80 {IMAGE} sleep 300")
        assert out["exit_code"] != 0 and "loopback" in out["stderr"]
    finally:
        ids = services_docker(
            "ps", "-aq", "--filter", f"label={docker_proxy.FRAME_LABEL}={ref}", check=False
        ).stdout.split()
        if ids:
            services_docker("rm", "-f", *ids, check=False)


async def test_a_plant_left_running_is_moved_when_services_stop(helper: Helper, folder: Path) -> None:
    ref = await helper.frame("ts", grants=[{"path": str(folder), "mode": "rw", "name": "/work"}])
    await helper.spawn(ref, '(sleep 2; ln -s "$HOME" ./later; sleep 300)')
    await until(lambda: (folder / "later").is_symlink(), "the leftover's link", 10)
    async with helper.client() as c:
        assert (await c.post(f"/frames/{ref}/stop-services")).status_code == 204
    assert not (folder / "later").is_symlink()
    assert _quarantined(helper.home, ref) == ["/work/later"]
