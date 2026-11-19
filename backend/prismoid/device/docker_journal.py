# Copyright (c) 2026-present Plane Software, Inc. and contributors
# SPDX-License-Identifier: AGPL-3.0-only
# See the LICENSE file for details.

"""What each frame's Docker proxy made: `run/docker/<ref>.json`, 0600.

A label is not proof of who made an object: an image's `LABEL prismoid.frame=<ref>` puts the label on
every container run from it, by anyone. So the proxy journals the id of every container, network
and volume it makes, before the client hears of it, and the helper acts - at a sleep, at the
frame's end, on a frame's own requests - only on journaled ids that still wear the label.

A frame's end retires its journal to `<ref>~<nonce>.json` (a frame made again under the same ref
starts a fresh one) and removes the retired file only once its cleanup has succeeded: with Docker
away it stays, and is tried again at the helper's next start and whenever it next reaches Docker.
Every write is atomic (a temporary file, then a rename). A torn journal reads as empty, which acts
on nothing: it fails closed. A live journal holds at most `CAP` ids: the proxy prunes the ids of
objects that are gone (`keep`, `forget`) and refuses a create past it.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import Final

KINDS: Final = ("containers", "networks", "volumes")
CAP: Final = 2048  # live ids per frame, every kind together
_REF = re.compile(r"[A-Za-z0-9_-]{1,100}")  # a frame ref: `device-<task id>`, `device-seat-<hex>`

Entry = dict[str, list[str]]


class Journal:
    def __init__(self, run: Path) -> None:
        self.dir = run / "docker"  # beside the frames' sockets, in the helper's 0700 `run/`
        self._cache: dict[Path, Entry] = {}  # live journals as last read or written: one reader

    def _live(self, ref: str) -> Path | None:
        return self.dir / f"{ref}.json" if _REF.fullmatch(ref) else None

    def _read(self, path: Path) -> Entry:
        if (cached := self._cache.get(path)) is not None:
            return {k: list(v) for k, v in cached.items()}
        try:
            raw = json.loads(path.read_text())
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        entry = {k: [str(i) for i in raw.get(k) or [] if isinstance(i, str)] for k in KINDS}  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        if "~" not in path.stem:  # a live journal: this process is its only writer
            self._cache[path] = {k: list(v) for k, v in entry.items()}
        return entry

    def _write(self, path: Path, entry: Entry) -> None:
        entry = {k: list(entry.get(k, [])) for k in KINDS}
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(entry))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        self._cache[path] = {k: list(v) for k, v in entry.items()}

    def record(self, ref: str, kind: str, ident: str) -> None:
        """`ident` is one of the frame's own `kind` ("containers", "networks", "volumes")."""
        if (path := self._live(ref)) is None:
            return
        entry = self._read(path)
        if ident not in entry.setdefault(kind, []):
            entry[kind].append(ident)
            self._write(path, entry)

    def entry(self, ref: str) -> Entry:
        """The frame's live journal: {} when it made nothing."""
        return self._read(path) if (path := self._live(ref)) is not None else {}

    def holds(self, ref: str, kind: str, ident: str) -> bool:
        return ident in self.entry(ref).get(kind, [])

    def count(self, ref: str) -> int:
        return sum(len(ids) for ids in self.entry(ref).values())

    def forget(self, ref: str, kind: str, ident: str) -> None:
        """The object is gone (removed through the proxy)."""
        if (path := self._live(ref)) is not None and ident in (entry := self._read(path)).get(kind, []):
            entry[kind].remove(ident)
            self._write(path, entry)

    def keep(self, ref: str, existing: dict[str, set[str]], before: Entry) -> None:
        """Of the ids journaled `before` a listing, only those whose objects it (`existing`, per
        kind) still lists stay journaled; one journaled since the listing began stays too."""
        if (path := self._live(ref)) is None or not (entry := self._read(path)):
            return
        listed = {k: set(before.get(k, [])) for k in KINDS}
        kept = {
            k: [i for i in entry.get(k, []) if i in existing.get(k, set()) or i not in listed[k]]
            for k in KINDS
        }
        if kept != entry:
            self._write(path, kept)

    def retire(self, ref: str) -> Path | None:
        """The frame has ended: its journal becomes a pending cleanup. None when it made nothing."""
        if (path := self._live(ref)) is None or not path.exists():
            return None
        pending = self.dir / f"{ref}~{secrets.token_hex(4)}.json"
        os.replace(path, pending)
        self._cache.pop(path, None)
        return pending

    def refs(self) -> list[str]:
        """The frames with a live journal."""
        return (
            sorted(p.stem for p in self.dir.glob("*.json") if "~" not in p.stem) if self.dir.is_dir() else []
        )

    def pending(self) -> list[tuple[str, Path]]:
        """The retired journals whose cleanup has not yet succeeded: (ref, file)."""
        if not self.dir.is_dir():
            return []
        found = ((p.stem.partition("~")[0], p) for p in sorted(self.dir.glob("*~*.json")))
        return [(ref, p) for ref, p in found if _REF.fullmatch(ref)]

    def read(self, pending: Path) -> Entry:
        return self._read(pending)  # never cached: only live journals are written

    def done(self, pending: Path) -> None:
        with contextlib.suppress(FileNotFoundError):
            pending.unlink()
