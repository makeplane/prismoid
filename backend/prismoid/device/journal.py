# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""The helper's exec journal: which command was cut off.

JSONL beside the frame's folders (never in a root, so a command cannot forge it), 0600. Each line
is a `start` (exec id, command head, time), a `finish` (exec id, exit code), an `unseen` (a command
that finished with nobody there to hear it: exec id, head, exit code) or an `ack` (exec id: the
worker has parked or resumed on it). Commands carry secrets in their arguments, so only a head of
the command is kept, and never a byte of its output.

An unfinished, unacknowledged command is the evidence "no blind re-run" rests on, so nothing ever
erases one: not a later command, not a new helper life, not the file's own compaction, not a line
torn by a crash before it. What `interrupted()` names is the last-started of them; it is
`still_running` when it is running in this helper life (only the worker went away), and cut off
otherwise (the helper died under it). With none open it names an `unseen` command, `finished` with
its exit code: "whether it finished", for a worker that left before the result came.

An `unseen` line is news only until it is old. A later one is named first, a `heard` closes it (a
later command's result reached its worker, who has moved on) and so does an `ack`, and a command the
worker has acted on is never told again, whenever it ends. A cut is closed by none of the first two.

Nothing here runs a command it could not write down: `start` raises when the line does not reach the
file whole.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Final

from prismoid.schemas.device import InterruptedOut

HEAD_CHARS: Final = 200
_MAX_BYTES: Final = 1024 * 1024  # past this, a start rewrites the file to the open entries alone


class Journal:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._running: set[str] = set()  # started in this helper life, not finished yet
        self._acked: set[str] = set()  # acted on in this life: compaction may drop the `ack` line

    @property
    def running(self) -> bool:
        """A command of this helper life has not finished."""
        return bool(self._running)

    def start(self, exec_id: str, command: str) -> None:
        entry = {"event": "start", "exec_id": exec_id, "head": command[:HEAD_CHARS], "at": _now()}
        if self._size() > _MAX_BYTES:
            self._rewrite([*self._open().values(), entry])
        else:
            self._append(entry)
        self._running.add(exec_id)

    def finish(self, exec_id: str, exit_code: int) -> None:
        self._running.discard(exec_id)
        self._append({"event": "finish", "exec_id": exec_id, "exit_code": exit_code, "at": _now()})

    def cut(self, exec_id: str) -> None:
        """The helper stopped this command under its worker: it no longer runs and it did not finish.
        Nothing is written: an entry left open IS the record, and only a `finish` would lose it."""
        self._running.discard(exec_id)

    def unseen(self, exec_id: str, command: str, exit_code: int) -> None:
        """This command finished and its worker had gone: its result reached nobody. It is told to
        the resumed attempt like a cut, until the worker has acted on it (`ack`), or has moved on
        (`heard`). One the worker already acted on, while it ran, stays told."""
        if exec_id in self._acked:
            return
        head = command[:HEAD_CHARS]
        self._append(
            {"event": "unseen", "exec_id": exec_id, "head": head, "exit_code": exit_code, "at": _now()}
        )

    def heard(self) -> None:
        """A result reached its worker: an older command nobody heard is no longer its last."""
        if any(entry["event"] == "unseen" for entry in self._open().values()):
            self._append({"event": "heard", "at": _now()})

    def ack(self, exec_id: str) -> None:
        """The worker has acted on this entry (parked the step, or resumed it): it is no longer news,
        and neither is any older entry still open - the record the worker parked on is newer than
        every one of them, so none may come back later to name a command of a step long gone."""
        open_ = list(self._open())
        older = open_[: open_.index(exec_id) + 1] if exec_id in open_ else [exec_id]
        self._acked.update(older)
        for entry in older:
            self._append({"event": "ack", "exec_id": entry, "at": _now()})

    def interrupted(self) -> InterruptedOut | None:
        open_ = self._open()
        if not open_:
            return None
        cut = [entry for entry in open_.values() if entry["event"] == "start"]
        last = (cut or list(open_.values()))[-1]
        return InterruptedOut(
            exec_id=last["exec_id"],
            command_head=last["head"],
            started_at=dt.datetime.fromisoformat(last["at"]),
            still_running=last["exec_id"] in self._running,
            exit_code=last.get("exit_code"),
        )

    def _open(self) -> dict[str, dict[str, Any]]:
        """Every entry with something still to tell, oldest first: a start with neither a finish nor
        an ack, and an unseen finish, unless a result was heard since or it was acked."""
        open_: dict[str, dict[str, Any]] = {}
        for entry in self._entries():
            event, exec_id = entry.get("event"), str(entry.get("exec_id"))
            if event == "heard":
                open_ = {k: v for k, v in open_.items() if v["event"] != "unseen"}
            if event in ("start", "unseen") and isinstance(entry.get("head"), str):
                open_[exec_id] = entry
            elif event in ("finish", "ack"):
                open_.pop(exec_id, None)
        return open_

    def _entries(self) -> list[dict[str, Any]]:
        try:
            lines = self.path.read_text(errors="replace").splitlines()
        except FileNotFoundError:
            return []
        out: list[dict[str, Any]] = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue  # a line torn by the crash this journal exists for
            if isinstance(entry, dict):
                out.append(entry)
        return out

    def _size(self) -> int:
        try:
            return self.path.stat().st_size
        except FileNotFoundError:
            return 0

    def _append(self, entry: dict[str, Any]) -> None:
        line = json.dumps(entry) + "\n"
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":  # a line a crash tore: it stays alone
                line = "\n" + line
            data = line.encode()
            while data:  # a full disk writes short, then raises: never a line taken as written
                data = data[os.write(fd, data) :]
        finally:
            os.close(fd)

    def _rewrite(self, entries: list[dict[str, Any]]) -> None:
        tmp = self.path.with_name(f".{self.path.name}.tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.writelines(json.dumps(e) + "\n" for e in entries)
        os.replace(tmp, self.path)


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat()
