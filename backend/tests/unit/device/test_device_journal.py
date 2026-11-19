# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The helper's exec journal: what was cut off, never what it printed."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import cast

import pytest

from prismoid.device.frames import DeviceFrameState, _left  # pyright: ignore[reportPrivateUsage]
from prismoid.device.journal import Journal


def test_journal_marks_unfinished_as_interrupted(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.start("e1", "ls")
    j.finish("e1", 0)
    j.start("e2", "pytest -x")
    # The helper died here; a new one reads the same file.
    cut = Journal(path).interrupted()
    assert cut is not None
    assert (cut.exec_id, cut.command_head) == ("e2", "pytest -x")
    assert path.stat().st_mode & 0o777 == 0o600


def test_a_finished_journal_is_not_interrupted(tmp_path: Path) -> None:
    j = Journal(tmp_path / "journal.jsonl")
    assert j.interrupted() is None
    j.start("e1", "ls")
    j.finish("e1", 1)
    assert Journal(tmp_path / "journal.jsonl").interrupted() is None


def test_a_cut_command_stays_owed_and_no_longer_runs(tmp_path: Path) -> None:
    """A helper stopping in order leaves what a crash leaves: an open entry, reported as cut off."""
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.start("e1", "npm run test:slow")
    j.cut("e1")
    for life in (j, Journal(path)):
        cut = life.interrupted()
        assert cut is not None and (cut.exec_id, cut.still_running) == ("e1", False)
    assert [json.loads(line)["event"] for line in path.read_text().splitlines()] == ["start"]


def test_a_finish_nobody_heard_is_told_until_acked(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.start("e1", "git push")
    j.finish("e1", 0)
    assert j.interrupted() is None
    j.unseen("e1", "git push", 0)
    for life in (j, Journal(path)):
        told = life.interrupted()
        assert told is not None
        assert (told.exec_id, told.command_head, told.exit_code) == ("e1", "git push", 0)
        assert told.still_running is False
    j.ack("e1")
    assert Journal(path).interrupted() is None


def test_a_cut_is_named_before_a_finish_nobody_heard(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.start("cut", "make deploy")
    j.start("heard-by-nobody", "ls")
    j.finish("heard-by-nobody", 0)
    j.unseen("heard-by-nobody", "ls", 0)
    told = Journal(path).interrupted()
    assert told is not None and (told.exec_id, told.exit_code) == ("cut", None)


def test_compaction_keeps_a_finish_nobody_heard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.start("e1", "git push")
    j.finish("e1", 0)
    j.unseen("e1", "git push", 7)
    monkeypatch.setattr("prismoid.device.journal._MAX_BYTES", 0)
    j.start("e2", "ls")
    j.finish("e2", 0)
    told = Journal(path).interrupted()
    assert told is not None and (told.exec_id, told.exit_code) == ("e1", 7)


def test_a_torn_last_line_never_eats_the_next_entry(tmp_path: Path) -> None:
    """ENOSPC or power loss in mid-line: the next `start` lands on a line of its own, so the cut
    stays reported. Without that newline both lines fail to parse and the evidence is gone."""
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.start("e1", "ls")
    j.finish("e1", 0)
    with path.open("a") as f:
        f.write('{"event": "start", "exec_id": "to')  # torn by the crash
    Journal(path).start("e2", "make deploy")
    cut = Journal(path).interrupted()
    assert cut is not None and (cut.exec_id, cut.command_head) == ("e2", "make deploy")


def _unseen(j: Journal, exec_id: str, exit_code: int = 0) -> None:
    j.start(exec_id, f"make {exec_id}")
    j.finish(exec_id, exit_code)
    j.unseen(exec_id, f"make {exec_id}", exit_code)


def test_a_later_result_somebody_heard_closes_an_unseen_finish(tmp_path: Path) -> None:
    """The worker has moved on: the old command is no longer its last, in any later life."""
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    _unseen(j, "old")
    j.start("newer", "ls")
    j.finish("newer", 0)
    assert j.interrupted() is not None, "not until somebody heard it"
    j.heard()
    assert j.interrupted() is None and Journal(path).interrupted() is None


def test_a_later_unseen_finish_is_the_last_command(tmp_path: Path) -> None:
    j = Journal(tmp_path / "journal.jsonl")
    _unseen(j, "old")
    _unseen(j, "newer", 3)
    told = j.interrupted()
    assert told is not None and (told.exec_id, told.exit_code) == ("newer", 3)
    j.ack("newer")
    assert j.interrupted() is None


def test_a_heard_result_never_closes_a_cut(tmp_path: Path) -> None:
    """The evidence "no blind re-run" rests on: only a finish or an ack ends it."""
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.start("cut", "make deploy")
    _unseen(j, "other")
    j.heard()
    told = Journal(path).interrupted()
    assert told is not None and (told.exec_id, told.exit_code) == ("cut", None)


def test_heard_writes_nothing_with_nothing_unseen(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.start("e1", "ls")
    j.finish("e1", 0)
    before = path.read_text()
    j.heard()
    assert path.read_text() == before


@pytest.mark.parametrize("compacted", [False, True], ids=["plain", "past-a-compaction"])
def test_an_acked_command_never_comes_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compacted: bool
) -> None:
    """Parked on while it still ran, then it ended: the worker already acted on it. Told again it
    would be the wrong "last command" at the next park."""
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.start("ran-on", "make deploy")
    j.ack("ran-on")
    if compacted:
        monkeypatch.setattr("prismoid.device.journal._MAX_BYTES", 0)
        j.start("next", "ls")
        j.finish("next", 0)
    j.finish("ran-on", 0)
    j.unseen("ran-on", "make deploy", 0)
    assert j.interrupted() is None and Journal(path).interrupted() is None


def test_a_short_write_is_finished_not_taken_as_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal.jsonl"
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real(fd, data[:7]))
    Journal(path).start("e1", "make deploy")
    monkeypatch.undo()
    cut = Journal(path).interrupted()
    assert cut is not None and cut.command_head == "make deploy"


def test_a_start_that_cannot_be_written_raises_and_leaves_nothing_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk: a few bytes, then ENOSPC. The caller must not run the command."""
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    real = os.write
    calls = {"n": 0}

    def full(fd: int, data: bytes) -> int:
        calls["n"] += 1
        if calls["n"] > 1:
            raise OSError(errno.ENOSPC, "no space left on device")
        return real(fd, data[:7])

    monkeypatch.setattr(os, "write", full)
    with pytest.raises(OSError, match="no space"):
        j.start("e1", "make deploy")
    monkeypatch.undo()
    assert j.interrupted() is None, "no entry, and nothing `still_running`"
    j.start("e2", "ls")
    cut = j.interrupted()
    assert cut is not None and (cut.exec_id, cut.still_running) == ("e2", True), "the tear stays alone"


def test_concurrent_execs_the_last_started_unfinished_one_is_named(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.start("a", "make build")
    j.start("b", "make test")
    j.finish("b", 0)
    cut = Journal(path).interrupted()
    assert cut is not None and cut.exec_id == "a"


def test_a_second_helper_life_still_reports_the_first_lifes_cut_off_command(tmp_path: Path) -> None:
    """No blind re-run: the evidence outlives later commands and new lives until acked."""
    path = tmp_path / "journal.jsonl"
    Journal(path).start("old", "npm install")
    second = Journal(path)
    second.start("new", "npm test")
    second.finish("new", 0)
    third = Journal(path)
    cut = third.interrupted()
    assert cut is not None and (cut.exec_id, cut.still_running) == ("old", False)


def test_a_running_exec_is_still_running(tmp_path: Path) -> None:
    """Only the worker went away: the command runs on in this helper life."""
    j = Journal(tmp_path / "journal.jsonl")
    j.start("e1", "sleep 100")
    cut = j.interrupted()
    assert cut is not None and cut.still_running
    j.finish("e1", 0)
    assert j.interrupted() is None


def test_ack_clears_the_entry_and_every_older_open_one(tmp_path: Path) -> None:
    """B6 round 1 (M3): the worker parked on the newest cut-off command and holds it in Postgres; an
    older entry still open (a park whose ack was lost) must never come back to name a command of a
    step long gone."""
    path = tmp_path / "journal.jsonl"
    Journal(path).start("a", "make a")
    Journal(path).start("b", "make b")
    j = Journal(path)
    cut = j.interrupted()
    assert cut is not None and cut.exec_id == "b"
    j.ack("b")
    assert Journal(path).interrupted() is None


def test_ack_of_an_older_entry_leaves_the_newer_one(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    Journal(path).start("a", "make a")
    Journal(path).start("b", "make b")
    Journal(path).ack("a")
    cut = Journal(path).interrupted()
    assert cut is not None and cut.exec_id == "b"


def test_the_head_is_capped_and_no_output_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    j = Journal(path)
    j.start("e1", "echo " + "s" * 500)
    j.finish("e1", 0)
    j.start("e2", "x" * 1000)
    cut = Journal(path).interrupted()
    assert cut is not None and cut.command_head == "x" * 200
    for line in path.read_text().splitlines():
        assert set(json.loads(line)) <= {"event", "exec_id", "head", "at", "exit_code"}
    assert "s" * 201 not in path.read_text()


def test_a_torn_last_line_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    Journal(path).start("e1", "ls")
    with path.open("a") as f:
        f.write('{"event": "finish", "exec_')
    cut = Journal(path).interrupted()
    assert cut is not None and cut.exec_id == "e1"


def test_the_journal_stays_small_and_never_drops_an_unfinished_entry(tmp_path: Path) -> None:
    path = tmp_path / "journal.jsonl"
    Journal(path).start("cut", "the one the crash cut off")
    j = Journal(path)
    for n in range(5000):
        j.start(f"e{n}", "y" * 200)
        j.finish(f"e{n}", 0)
    assert path.stat().st_size < 2 * 1024 * 1024
    cut = Journal(path).interrupted()
    assert cut is not None and cut.exec_id == "cut"


def test_a_frame_that_cannot_be_looked_at_counts_as_still_there() -> None:
    """Counted as gone, a stop whose other frames drained would return before its SIGKILL."""

    class Blind:
        ref = "device-blind"

        class groups:
            @staticmethod
            def pids() -> set[int]:
                raise OSError(errno.EIO, "no")

    assert _left(cast("DeviceFrameState", Blind)) == 1
