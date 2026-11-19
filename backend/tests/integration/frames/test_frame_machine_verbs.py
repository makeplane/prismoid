# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The Machine's Docker-only verbs: reset_mounts, work that survives archive and unpack, bound-frame
widening, hard links. The driver-neutral clauses (labels, bind, freeze, limits, allow, stop) live in tests/conformance."""

import io
import uuid

import pytest

from prismoid.ai.frames import new_frame
from prismoid.ai.frames.driver import Checkpoints
from prismoid.schemas.machines import LABEL_EPOCH, LABEL_KIND, LABEL_MACHINE, FrameKind

pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("frames-a")]  # real Docker frames


async def _machine_frame(epoch: int = 1):
    mid = uuid.uuid4()
    frame = new_frame(str(mid), name=f"machine-{mid}-{epoch}")
    await frame.create(
        allow_domains=[],
        labels={LABEL_KIND: FrameKind.MACHINE, LABEL_MACHINE: str(mid), LABEL_EPOCH: str(epoch)},
    )
    return mid, frame


async def test_reset_mounts_empties_the_last_steps_mounts_and_keeps_the_lock() -> None:
    """A warm Machine's /task and /skills outlive the step: the boundary empties them -
    as the frame's root, which owns /task but holds no capability - and locks /task again, so the
    agent still reads, and never writes, what the system gives it. The step marker stays (the
    boundary rewrites it), and a read-only directory the agent left in /skills is no obstacle."""
    _, frame = await _machine_frame()
    try:
        await frame.write_bytes("/task/.step-start", b"", mode=0o444, owner="system")
        await frame.write_bytes("/task/inputs/01-overview-final.md", b"turn one", mode=0o444, owner="system")
        await frame.write_bytes("/task/context.md", b"a private memory", mode=0o444, owner="system")
        await frame.exec(
            "mkdir -p /skills/draft /skills/locked && touch /skills/locked/x && chmod 0555 /skills/locked"
        )
        await frame.write_bytes("/skills/draft/SKILL.md", b"an author's draft", mode=0o444, owner="system")
        await frame.reset_mounts()
        left = await frame.exec(
            "ls -A /task/inputs /skills && test ! -e /task/context.md && test -e /task/.step-start"
        )
        assert left.ok and left.stdout.split() == ["/skills:", "/task/inputs:"], left
        locks = await frame.exec("stat -c '%a %U' /task /task/inputs")
        assert locks.stdout.split("\n")[:2] == ["555 root", "555 root"]
        assert not (await frame.exec("touch /task/inputs/planted")).ok, (
            "the agent still cannot plant a source"
        )
        await frame.write_bytes("/task/inputs/01-q4.md", b"turn two", mode=0o444, owner="system")
        assert (await frame.exec("cat /task/inputs/01-q4.md")).stdout == "turn two"
    finally:
        await frame.destroy()


async def test_work_survives_archive_and_unpack_into_a_new_frame() -> None:
    _, first = await _machine_frame(1)
    buf = io.BytesIO()
    try:
        await first.exec(
            "python3 -c \"import sqlite3; c=sqlite3.connect('/work/notes.db'); "
            "c.execute('create table f(n)'); c.executemany('insert into f values (?)', [(1,),(2,),(3,)]); c.commit()\""
        )
        await first.exec(
            "pip install --user --quiet --no-index six 2>/dev/null; mkdir -p /work/.local && echo kept > /work/.local/marker"
        )
        await first.freeze()
        assert isinstance(first, Checkpoints)
        await first.archive("/work", buf)
    finally:
        await first.destroy()
    _, second = await _machine_frame(2)
    try:
        buf.seek(0)
        assert isinstance(second, Checkpoints)
        await second.unpack("/work", buf)
        rows = await second.exec(
            "python3 -c \"import sqlite3; print(sqlite3.connect('/work/notes.db').execute('select count(*) from f').fetchone()[0])\""
        )
        assert rows.stdout.strip() == "3"
        assert (await second.exec("cat /work/.local/marker")).stdout.strip() == "kept"
        owner = await second.exec("stat -c %u /work/notes.db")
        assert owner.stdout.strip() == "1000"
    finally:
        await second.destroy()


async def test_a_bound_frames_unpinned_widen_applies_and_leaves_the_rows_version() -> None:
    """A resumed step widens the frame it bound (execution's resume): the bound driver
    has synced no version, so its unpinned push goes unversioned - it applies, and the proxy keeps
    the version the boundary pinned, so a stale pin is still refused and the next one lands."""
    mid, frame = await _machine_frame()
    try:
        assert await frame.allow(["a.example"], 5) is True  # a boundary push, pinned from the row
        again = new_frame(str(mid), name=frame.ref)
        await again.bind()
        assert await again.allow(["b.example"]) is True
        assert again.egress_version == 0 and again.egress_hosts == ["b.example"]
        assert await again.allow(["c.example"], 5) is False  # the real proxy still holds v5
        assert await again.allow(["c.example"], 6) is True
    finally:
        await frame.destroy()


async def test_a_hard_linked_file_in_work_wakes_with_both_names() -> None:
    """Docker's tar makes the second name of a multiply-linked file a hard link member; the
    restore keeps it, so a local clone's objects or a package cache survive a sleep."""
    _, first = await _machine_frame(1)
    buf = io.BytesIO()
    try:
        await first.exec("echo shared > /work/a && ln /work/a /work/b")
        assert isinstance(first, Checkpoints)
        await first.archive("/work", buf)
    finally:
        await first.destroy()
    _, second = await _machine_frame(2)
    try:
        buf.seek(0)
        assert isinstance(second, Checkpoints)
        await second.unpack("/work", buf)
        out = (await second.exec("cat /work/a /work/b && stat -c '%i %u' /work/a /work/b")).stdout.split("\n")
        assert out[:2] == ["shared", "shared"]
        assert out[2] == out[3] and out[2].endswith(" 1000")  # one inode, the agent's
    finally:
        await second.destroy()
